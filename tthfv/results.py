# SPDX-License-Identifier: Apache-2.0
"""Join the manifest with what each stage wrote, as work/results/index.json and docs/results.md.

Sources, per design, under WORK: src/<id>/fetch.json, build/<id>/build.json,
reference/<id>/reference.json and reference-fail.json, and run/<id>/outcome.json (what the runner saw
last, which is the only record of a failed fetch). A stage with no record is `not-run`.

A record is `stale` when it was made from something other than the current manifest, fetch and tools:
its commit differs from the manifest's, its sources_sha256 from the current fetch.json's (where a fetch
exists), or a tool version from the installed one. The reason names the field. The runner's outcome.json
wins over an older build.json or reference.json when the latest attempt errored or did not run, so an
old `ok` is never shown for a design whose latest attempt failed.

index.json has a generation time. docs/results.md has none, so it only changes when a result does.
"""

import argparse
import collections
import json
from datetime import UTC, datetime
from pathlib import Path

from tthfv import manifest, paths, tools
from tthfv.errors import UserError
from tthfv.manifest import Design

DEFAULT_MD = paths.REPO_ROOT / "docs" / "results.md"
_STAGES = ("fetch", "build", "reference")


class Invalid(Exception):
    """A per-design record is unreadable or has the wrong shape; the message names the file."""


def _read(path: Path, work: Path) -> dict | None:
    """The JSON object in path, None if there is no such file, Invalid if it is damaged."""
    name = path.relative_to(work) if path.is_relative_to(work) else path
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Invalid(f"{name}: unreadable: {e}") from e
    try:
        data = json.loads(text)
    except ValueError as e:
        raise Invalid(f"{name}: not valid JSON (truncated?): {e}") from e
    if not isinstance(data, dict):
        raise Invalid(f"{name}: expected a JSON object")
    return data


def _need(rec: dict, key: str, name: str) -> object:
    if key not in rec:
        raise Invalid(f"{name}: missing {key!r}")
    return rec[key]


def _from_outcome(outcome: dict, stage: str, name: str) -> dict | None:
    o = outcome.get(stage)
    if o is None:
        return None
    if not isinstance(o, dict):
        raise Invalid(f"{name}: {stage} is not an object")
    return {"status": _need(o, "status", f"{name} {stage}"), "reason": o.get("reason")}


def _invalid(why: str, nulls: dict) -> dict:
    return {"status": "invalid", "reason": why, **nulls}


def _stale(why: str, nulls: dict) -> dict:
    return {"status": "stale", "reason": why, **nulls}


def _short(value: object) -> str:
    text = str(value)
    return text[:12] if len(text) > 40 else text


def _differences(rec: dict, name: str, commit: str, fetch: dict | None) -> list[str]:
    """Which of commit, sources_sha256 and the tool versions in a stage record differ from the current
    ones. `rec` has commit and sources_sha256 (a build record or a reference key). A missing field is
    itself a difference (a record from an older version)."""
    out = []
    if "commit" not in rec:
        out.append(f"{name} has no commit (written by an older version)")
    elif rec["commit"] != commit:
        out.append(f"commit differs (record {_short(rec['commit'])}, manifest {_short(commit)})")
    if fetch is not None:
        if "sources_sha256" not in rec:
            out.append(f"{name} has no sources_sha256 (written by an older version)")
        elif rec["sources_sha256"] != fetch.get("sources_sha256"):
            out.append(
                f"sources_sha256 differs (record {_short(rec['sources_sha256'])},"
                f" fetch {_short(fetch.get('sources_sha256'))})"
            )
    return out


def _tool_differences(recorded: dict, current: dict | None, names: tuple[str, ...]) -> list[str]:
    out = []
    for tool in names:
        if not current or tool not in current:
            continue
        if recorded.get(tool) != current[tool]:
            out.append(f"{tool} version differs (record {recorded.get(tool)!r}, installed {current[tool]!r})")
    return out


def _later_failure(outcome: dict, outcome_ns: int | None, stage: str, record: Path) -> dict | None:
    """The runner's error / not-run outcome of a stage, if it is newer than the stage's record file (or the
    record does not exist). That attempt is the latest word: an older record must not be shown."""
    o = outcome.get(stage)
    if not isinstance(o, dict) or o.get("status") not in ("error", "not-run") or outcome_ns is None:
        return None
    try:
        record_ns = record.stat().st_mtime_ns
    except FileNotFoundError:
        return o
    return o if outcome_ns >= record_ns else None


_BUILD_NULLS = {
    "lut_percent": None, "fmax_hz": None, "recipe": None, "attempts": None, "bitstream_sha256": None,
}  # fmt: skip
_REF_NULLS = {"mode": None, "width": None, "height": None, "frames_complete": None, "hash": None}


def _fetch_record(work: Path, design: Design) -> dict | None:
    """fetch.json, or None if there is none or it is unreadable."""
    try:
        return _read(work / "src" / design.id / "fetch.json", work)
    except Invalid:
        return None


def _fetch(work: Path, design: Design, outcome: dict) -> dict:
    name = f"run/{design.id}/outcome.json"
    try:
        if (o := _from_outcome(outcome, "fetch", name)) is not None:
            return o
        rec = _fetch_record(work, design)
        if (work / "src" / design.id / "fetch.json").exists():
            if rec is not None and rec.get("commit") != design.commit:
                why = f"commit differs (record {_short(rec.get('commit'))}, manifest {_short(design.commit)})"
                return {"status": "stale", "reason": why}
            return {"status": "ok", "reason": ""}
        return {"status": "not-run", "reason": None}
    except Invalid as e:
        return _invalid(str(e), {})


def _build(work: Path, design: Design, outcome: dict, outcome_ns: int | None, now: dict | None) -> dict:
    name = f"build/{design.id}/build.json"
    try:
        b = _read(work / name, work)
        later = _later_failure(outcome, outcome_ns, "build", work / name)
        if later is not None:
            return {**_from_outcome(outcome, "build", f"run/{design.id}/outcome.json"), **_BUILD_NULLS}
        if b is None:
            o = _from_outcome(outcome, "build", f"run/{design.id}/outcome.json")
            return {**(o or {"status": "not-run", "reason": None}), **_BUILD_NULLS}
        status = _need(b, "status", name)
        differs = _differences(b, "build.json", design.commit, _fetch_record(work, design))
        differs += _tool_differences(b.get("tools") or {}, now, ("yosys", "nextpnr-ice40", "icepack"))
        if differs:
            return _stale("; ".join(differs), _BUILD_NULLS)
        return {
            "status": status,
            "reason": b.get("reason"),
            "lut_percent": b.get("lut_percent"),
            "fmax_hz": b.get("fmax_hz"),
            "recipe": b.get("recipe"),
            "attempts": len(b["attempts"]) if b.get("attempts") is not None else None,
            "bitstream_sha256": b.get("bitstream_sha256"),
        }
    except Invalid as e:
        return _invalid(str(e), _BUILD_NULLS)
    except (TypeError, KeyError, IndexError) as e:
        return _invalid(f"{name}: {type(e).__name__}: {e}", _BUILD_NULLS)


def _reference(work: Path, design: Design, outcome: dict, outcome_ns: int | None, now: dict | None) -> dict:
    name = f"reference/{design.id}/reference.json"
    try:
        ref = _read(work / name, work)
        fail_path = work / "reference" / design.id / "reference-fail.json"
        record = work / name if ref is not None else fail_path
        if _later_failure(outcome, outcome_ns, "reference", record) is not None:
            o = _from_outcome(outcome, "reference", f"run/{design.id}/outcome.json")
            return {**o, **_REF_NULLS}
        if ref is not None:
            complete = _need(ref, "frames_complete", name)
            key = ref.get("key")
            if not isinstance(key, dict):
                return _stale("reference.json has no key (written by an older version)", _REF_NULLS)
            differs = _differences(key, "reference.json key", design.commit, _fetch_record(work, design))
            differs += _tool_differences(ref.get("tools") or {}, now, ("verilator",))
            if differs:
                return _stale("; ".join(differs), _REF_NULLS)
            first = ref["frames"][0]["hash"] if ref.get("frames") else None
            return {
                "status": "ok" if complete else "no-sync",
                "reason": "",
                "mode": ref.get("mode"),
                "width": ref.get("width"),
                "height": ref.get("height"),
                "frames_complete": complete,
                "hash": first,
            }
        name = f"reference/{design.id}/reference-fail.json"
        fail = _read(work / name, work)
        if fail is not None:
            return {
                "status": _need(fail, "status", name), "reason": fail.get("reason"), **_REF_NULLS,
            }  # fmt: skip
        o = _from_outcome(outcome, "reference", f"run/{design.id}/outcome.json")
        return {**(o or {"status": "not-run", "reason": None}), **_REF_NULLS}
    except Invalid as e:
        return _invalid(str(e), _REF_NULLS)
    except (TypeError, KeyError, IndexError) as e:
        return _invalid(f"{name}: {type(e).__name__}: {e}", _REF_NULLS)


def collect(
    designs: list[Design], work: Path | None = None, current_tools: dict[str, str] | None = None
) -> list[dict]:
    """One record per design, sorted by id. current_tools ({tool: version}, from the installed toolchain)
    is what stage records are compared with; None or {} skips that comparison."""
    work = paths.WORK if work is None else work
    records = []
    for d in sorted(designs, key=lambda d: d.id):
        outcome_path = work / "run" / d.id / "outcome.json"
        try:
            outcome = _read(outcome_path, work) or {}
            outcome_ns = outcome_path.stat().st_mtime_ns if outcome else None
        except Invalid:
            outcome, outcome_ns = {}, None  # supplementary: a damaged one is as good as none
        fetch_record = _fetch_record(work, d) or {}
        records.append(
            {
                "id": d.id,
                "title": d.title,
                "author": d.author,
                "shuttle": d.shuttle,
                "macro": d.macro,
                "tiles": d.tiles,
                "clock_hz": d.clock_hz,
                "clock_declared": d.clock_declared,
                "candidate": d.candidate,
                "render_verdict": d.render_verdict,
                "qspi": d.qspi is not None,
                "scripted": d.scripted,
                "ui_in": d.ui_in,
                "commit": d.commit,
                "sources_sha256": fetch_record.get("sources_sha256"),
                "top_module_used": fetch_record.get("top_module_used"),
                "fetch": _fetch(work, d, outcome),
                "build": _build(work, d, outcome, outcome_ns, current_tools),
                "reference": _reference(work, d, outcome, outcome_ns, current_tools),
            }
        )
    return records


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", r"\|")


def _counts(records: list[dict], stage: str) -> str:
    c = collections.Counter(r[stage]["status"] for r in records)
    order = sorted(c, key=lambda s: (s != "ok", s))  # ok first, then by name
    return ", ".join(f"{c[s]} {s}" for s in order)


def render_markdown(records: list[dict]) -> str:
    lines = ["# Results", "", f"{len(records)} designs.", ""]
    lines += [f"- {stage}: {_counts(records, stage)}" for stage in _STAGES]
    lines += [
        "",
        "| id | title | tiles | build | LUT % | fmax / clock (MHz) | reference | mode | frames |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(records, key=lambda r: r["id"]):
        b, ref = r["build"], r["reference"]
        lut = "-" if b["lut_percent"] is None else f"{b['lut_percent']:.1f}"
        fmax = "-" if b["fmax_hz"] is None else f"{b['fmax_hz'] / 1e6:.2f}"
        frames = "-" if ref["frames_complete"] is None else str(ref["frames_complete"])
        cells = [
            r["id"], _cell(r["title"]), r["tiles"], b["status"], lut,
            f"{fmax} / {r['clock_hz'] / 1e6:g}", ref["status"], ref["mode"] or "-", frames,
        ]  # fmt: skip
        lines.append("| " + " | ".join(cells) + " |")
    stale = [
        f"- {r['id']} {stage}: {_cell(r[stage]['reason'] or '')}"
        for r in sorted(records, key=lambda r: r["id"])
        for stage in _STAGES
        if r[stage]["status"] == "stale"
    ]
    if stale:
        lines += ["", "Stale records (rerun the stage):", "", *stale]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.rename(path)


def _videos_repo_commit() -> str | None:
    """HEAD of the videos repo the manifest was read from, or None if it is not a git checkout."""
    try:
        return manifest._head(manifest.default_videos_repo())
    except (OSError, UserError):
        return None


def _installed_tools() -> dict[str, str]:
    try:
        return tools.versions()
    except (UserError, OSError):
        return {}  # no toolchain: records cannot be compared with it


def _cmd_results(args: argparse.Namespace) -> int:
    current = _installed_tools()
    records = collect(manifest.load(extras=manifest.EXTRAS_DEFAULT), current_tools=current)
    index = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(records),
        "tools": current,
        "videos_repo_commit": _videos_repo_commit(),
        "designs": records,
    }
    _write(paths.WORK / "results" / "index.json", json.dumps(index, indent=2) + "\n")
    md = Path(args.out_md)
    _write(md, render_markdown(records))
    print(f"{len(records)} designs: {paths.WORK / 'results' / 'index.json'}, {md}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("results", help="write the results index and the Markdown table")
    p.add_argument(
        "--out-md", default=str(DEFAULT_MD), help=f"Markdown table to write (default {DEFAULT_MD})"
    )
    p.set_defaults(func=_cmd_results)
