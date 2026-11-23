# SPDX-License-Identifier: Apache-2.0
"""Design manifest: the list of designs, read from the videos repo's data files.

This reads JSON and text files only. It imports nothing from the videos repo.
"""

import argparse
import difflib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from tthfv import paths, pins
from tthfv.stimulus import first_event_inputs, fixed_inputs

DEFAULT_CLOCK_HZ = 25_175_000
EXTRAS_DEFAULT = paths.REPO_ROOT / "designs" / "extra.json"
_PORTS = ("ui", "uo", "uio")
_TINY_VGA_UO = ["R1", "G1", "B1", "vsync", "R0", "G0", "B0", "hsync"]


@dataclass(frozen=True)
class Design:
    id: str  # "<shuttle>/<macro>"
    shuttle: str
    macro: str
    title: str
    author: str
    repo: str
    commit: str
    top_module: str
    source_files: tuple[str, ...]
    clock_hz: int  # never 0: 25_175_000 substituted
    clock_declared: bool  # False when the manifest said 0
    pinout: dict  # {"ui": [...8], "uo": [...8], "uio": [...8]}
    pmods: tuple[str, ...]
    tiles: str
    render_verdict: str | None  # videos repo verdict; None for extras
    override: dict  # compiled override json, {} if none
    patch: Path | None  # override patch file, if any
    qspi: str | None  # pin map string from the render result, if any
    ui_in: int  # fixed value by the stimulus rule
    uio_in: int
    scripted: bool  # True if an .inputs or .gamepad script exists
    candidate: bool  # render_verdict == "ok"
    subdir: str = ""  # directory of the sources inside the repo ("" for the repo root)

    def to_json(self) -> dict:
        d = asdict(self)
        d["source_files"] = list(self.source_files)
        d["pmods"] = list(self.pmods)
        d["patch"] = str(self.patch) if self.patch else None
        return d


def sync_videos_repo() -> Path:
    """Clone the videos repo to WORK/videos-repo and check out exactly the pinned commit."""
    dest = paths.WORK / "videos-repo"
    if not (dest / ".git").exists():
        paths.WORK.mkdir(parents=True, exist_ok=True)
        _git("clone", "--filter=blob:none", "--no-checkout", pins.VIDEOS_REPO_URL, str(dest))
    elif _head(dest) == pins.VIDEOS_REPO_COMMIT:
        return dest
    else:
        _git("fetch", "origin", cwd=dest)
    _git("checkout", "--detach", pins.VIDEOS_REPO_COMMIT, cwd=dest)
    if _head(dest) != pins.VIDEOS_REPO_COMMIT:
        raise RuntimeError(f"{dest} is not at {pins.VIDEOS_REPO_COMMIT}")
    return dest


def _git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).strip()


def _group_pinout(flat: dict) -> dict:
    """Turn {"ui[0]": "x", ...} into {"ui": [8 names], "uo": [...], "uio": [...]}."""
    grouped = {port: [""] * 8 for port in _PORTS}
    for key, name in flat.items():
        port, _, rest = key.partition("[")
        if port in grouped and rest.endswith("]"):
            grouped[port][int(rest[:-1])] = name
    return grouped


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _from_target(data: Path, t: dict) -> Design:
    shuttle, macro = t["shuttle"], t["macro"]
    comp = data / "overrides-compiled" / shuttle
    src_dir = data.parent / "overrides" / shuttle
    res_path = data / "results" / shuttle / macro / "result.json"
    result = _read_json(res_path) if res_path.exists() else {}
    verdict = result.get("verdict")

    # The compiled override directory is gitignored in the videos repo, so a clone at the pinned
    # commit lacks it. The render result records the compiled override it ran with (committed),
    # so use that when the compiled file is absent.
    ov_path = comp / f"{macro}.json"
    override = _read_json(ov_path) if ov_path.exists() else (result.get("override") or {})

    script_path = comp / override.get("inputs", f"{macro}.inputs")
    script = script_path.read_text() if script_path.exists() else None
    if script is None and override.get("inputs"):
        # No compiled script (pinned clone): take the first event of the tracked source YAML.
        yaml_path = src_dir / f"{macro}.yaml"
        if not yaml_path.exists():
            raise FileNotFoundError(f"{t['id']}: override has inputs but {yaml_path} is missing")
        first = first_event_inputs((yaml.safe_load(yaml_path.read_text()) or {}).get("inputs") or [])
        if first is not None:
            script = f"0 {first[0]} {first[1]}\n"
    gamepad_path = comp / override.get("gamepad", f"{macro}.gamepad")
    ui_in, uio_in = fixed_inputs(override, script)

    patch = None
    if override.get("patch"):
        patch = next((p for p in (comp / override["patch"], src_dir / override["patch"]) if p.exists()), None)
        if patch is None:
            raise FileNotFoundError(f"{t['id']}: override names patch {override['patch']!r}, not found")

    declared_clock = int(override.get("clock_hz", t["clock_hz"]) or 0)
    return Design(
        id=t["id"],
        shuttle=shuttle,
        macro=macro,
        title=t["title"],
        author=t["author"],
        repo=t["repo"],
        commit=t["commit"],
        top_module=override.get("top_module", t["top_module"]),
        source_files=tuple(override.get("sources", t["source_files"])),
        clock_hz=declared_clock or DEFAULT_CLOCK_HZ,
        clock_declared=declared_clock != 0,
        pinout=_group_pinout(t["pinout"]),
        pmods=tuple(t["pmods"]),
        tiles=t["tiles"],
        render_verdict=verdict,
        override=override,
        patch=patch,
        qspi=result.get("qspi") or None,
        ui_in=ui_in,
        uio_in=uio_in,
        scripted=bool(script is not None or gamepad_path.exists() or "inputs" in override or "gamepad" in override),
        candidate=verdict == "ok",
    )


def _from_extra(e: dict) -> Design:
    top = e["top_module"]
    pinout = {"ui": [""] * 8, "uo": list(_TINY_VGA_UO), "uio": [""] * 8}
    return Design(
        id=e["id"],
        shuttle=e["id"].split("/", 1)[0],
        macro=top,
        title=e.get("title", top),
        author=e.get("author", ""),
        repo=e["repo"],
        commit=e["commit"],
        top_module=top,
        source_files=tuple(e["source_files"]),
        clock_hz=int(e["clock_hz"]) or DEFAULT_CLOCK_HZ,
        clock_declared=int(e["clock_hz"]) != 0,
        pinout=pinout,
        pmods=("tiny-vga",),
        tiles=e.get("tiles", "1x1"),
        render_verdict=None,
        override={},
        patch=None,
        qspi=None,
        ui_in=int(e.get("ui_in", 0)),
        uio_in=int(e.get("uio_in", 0)),
        scripted=False,
        candidate=False,
        subdir=e.get("subdir", ""),
    )


def load(videos_repo: Path | None = None, extras: Path | None = None) -> list[Design]:
    """Load all designs, sorted by id. videos_repo defaults to the synced clone in WORK."""
    repo = videos_repo if videos_repo is not None else paths.WORK / "videos-repo"
    data = repo / "data"
    targets = _read_json(data / "targets.json")["targets"]
    designs = [_from_target(data, t) for t in targets]
    if extras is not None:
        designs += [_from_extra(e) for e in json.loads(extras.read_text())]
    ids = [d.id for d in designs]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"duplicate design ids: {sorted(dup)}")
    return sorted(designs, key=lambda d: d.id)


def by_id(designs: list[Design], id: str) -> Design:
    for d in designs:
        if d.id == id:
            return d
    close = difflib.get_close_matches(id, [d.id for d in designs], n=5, cutoff=0.6)
    hint = f"; close matches: {', '.join(close)}" if close else ""
    raise KeyError(f"no design {id!r}{hint}")


def _cmd_sync(args: argparse.Namespace) -> int:
    print(sync_videos_repo())
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    designs = load(extras=EXTRAS_DEFAULT)
    if args.candidates:
        designs = [d for d in designs if d.candidate]
    if args.json:
        json.dump([d.to_json() for d in designs], sys.stdout, indent=2)
        print()
    else:
        for d in designs:
            print(f"{d.id}\t{d.render_verdict or '-'}\t{d.clock_hz}\t{d.title}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    design = by_id(load(extras=EXTRAS_DEFAULT), args.id)
    json.dump(design.to_json(), sys.stdout, indent=2)
    print()
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("manifest", help="list the designs and their fixed inputs")
    msub = p.add_subparsers(dest="manifest_command", metavar="<subcommand>")
    msub.required = True

    s = msub.add_parser("sync", help="clone the videos repo at the pinned commit")
    s.set_defaults(func=_cmd_sync)

    ls = msub.add_parser("list", help="list designs")
    ls.add_argument("--candidates", action="store_true", help="only designs whose render verdict is ok")
    ls.add_argument("--json", action="store_true", help="full records as JSON")
    ls.set_defaults(func=_cmd_list)

    sh = msub.add_parser("show", help="show one design as JSON")
    sh.add_argument("id", help="<shuttle>/<macro>")
    sh.set_defaults(func=_cmd_show)
