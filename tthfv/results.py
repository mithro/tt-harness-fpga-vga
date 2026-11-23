# SPDX-License-Identifier: Apache-2.0
"""Join the manifest with what each stage wrote, as work/results/index.json and work/results/results.md.

Sources, per design, under WORK: src/<id>/fetch.json, build/<id>/build.json,
reference/<id>/reference.json and reference-fail.json, and run/<id>/outcome.json (what the runner saw
last, which is the only record of a failed fetch). A stage with no record is `not-run`; if the outcome
says `ok` for it the reason is "record missing (interrupted run?)", since the record was lost.

A record is `stale` when it was made from something other than the current manifest, fetch and tools:
its commit differs from the manifest's, its sources_sha256 from the current fetch.json's (where a fetch
exists), or a tool version from the installed one. The reason names the field. The runner's outcome.json
wins over an older build.json or reference.json when the latest attempt errored or did not run, so an
old `ok` is never shown for a design whose latest attempt failed. Each outcome entry carries the time the
runner finished it; it is compared with the record file's mtime, per stage.

index.json has a generation time. The Markdown table (work/results/results.md by default) has none, so it only changes when a result does.
"""

import argparse
import collections
import json
from datetime import UTC, datetime
from pathlib import Path

from tthfv import manifest, paths, tools
from tthfv.errors import UserError
from tthfv.manifest import Design
from tthfv.text import strip_control

_STAGES = ("fetch", "build", "reference")
RECORD_MISSING = "record missing (interrupted run?)"


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


def _finished_at(entry: dict) -> float | None:
    """When the runner finished this outcome entry (epoch seconds), None if it has no usable time."""
    try:
        when = datetime.fromisoformat(entry["finished"])
    except (KeyError, TypeError, ValueError):
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()


def _later_failure(outcome: dict, stage: str, record: Path, any_failure: bool = False) -> bool:
    """Whether the runner's outcome for a stage is a failure that is newer than the stage's record file.
    That attempt is the latest word: an older record must not be shown. The outcome entry's own time is
    compared with the record's mtime; an entry without a time (an old file) is older than any record.
    Only `error` and `not-run` count, since another status (pnr-fail, ...) is also in the record itself;
    any_failure (fetch) counts every status but `ok`. A missing record is the caller's business."""
    o = outcome.get(stage)
    if not isinstance(o, dict):
        return False
    status = o.get("status")
    if not (status != "ok" if any_failure else status in ("error", "not-run")):
        return False
    finished = _finished_at(o)
    if finished is None:
        return False
    try:
        return finished >= record.stat().st_mtime
    except FileNotFoundError:
        return True


def _record_missing(outcome: dict, stage: str, name: str) -> dict:
    """The status of a stage whose record file is absent. The outcome may say why (an error, a failure);
    an `ok` there proves nothing, since the record is what was lost, so it is `not-run`."""
    o = _from_outcome(outcome, stage, name)
    if o is None:
        return {"status": "not-run", "reason": None}
    if o["status"] == "ok":
        return {"status": "not-run", "reason": RECORD_MISSING}
    return o


_BUILD_NULLS = {
    "lut_percent": None, "fmax_hz": None, "recipe": None, "attempts": None, "bitstream_sha256": None,
    "timed_out": False,
}  # fmt: skip
_REF_NULLS = {
    "mode": None, "width": None, "height": None, "frames_complete": None, "hash": None, "timed_out": False,
}  # fmt: skip


def _fetch_record(work: Path, design: Design) -> dict | None:
    """fetch.json, or None if there is none or it is unreadable."""
    try:
        return _read(work / "src" / design.id / "fetch.json", work)
    except Invalid:
        return None


def _fetch(work: Path, design: Design, outcome: dict) -> dict:
    name = f"run/{design.id}/outcome.json"
    try:
        path = work / "src" / design.id / "fetch.json"
        if not path.exists():
            return _record_missing(outcome, "fetch", name)
        if _later_failure(outcome, "fetch", path, any_failure=True):
            return _from_outcome(outcome, "fetch", name)
        rec = _fetch_record(work, design)
        if rec is not None and rec.get("commit") != design.commit:
            why = f"commit differs (record {_short(rec.get('commit'))}, manifest {_short(design.commit)})"
            return {"status": "stale", "reason": why}
        return {"status": "ok", "reason": ""}
    except Invalid as e:
        return _invalid(str(e), {})


def _build(work: Path, design: Design, outcome: dict, now: dict | None) -> dict:
    name = f"build/{design.id}/build.json"
    try:
        oname = f"run/{design.id}/outcome.json"
        b = _read(work / name, work)
        if b is None:
            return {**_record_missing(outcome, "build", oname), **_BUILD_NULLS}
        if _later_failure(outcome, "build", work / name):
            return {**_from_outcome(outcome, "build", oname), **_BUILD_NULLS}
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
            "timed_out": b.get("timed_out") is True,
        }
    except Invalid as e:
        return _invalid(str(e), _BUILD_NULLS)
    except (TypeError, KeyError, IndexError) as e:
        return _invalid(f"{name}: {type(e).__name__}: {e}", _BUILD_NULLS)


def _reference(work: Path, design: Design, outcome: dict, now: dict | None) -> dict:
    name = f"reference/{design.id}/reference.json"
    try:
        ref = _read(work / name, work)
        fail_path = work / "reference" / design.id / "reference-fail.json"
        record = work / name if ref is not None else fail_path
        if record.exists() and _later_failure(outcome, "reference", record):
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
                "timed_out": False,
            }
        name = f"reference/{design.id}/reference-fail.json"
        fail = _read(work / name, work)
        if fail is not None:
            return {
                "status": _need(fail, "status", name), "reason": fail.get("reason"),
                **_REF_NULLS, "timed_out": fail.get("timed_out") is True,
            }  # fmt: skip
        return {**_record_missing(outcome, "reference", f"run/{design.id}/outcome.json"), **_REF_NULLS}
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
        except Invalid:
            outcome = {}  # supplementary: a damaged one is as good as none
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
                "build": _build(work, d, outcome, current_tools),
                "reference": _reference(work, d, outcome, current_tools),
            }
        )
    return records


def _cell(text: object) -> str:
    return " ".join(strip_control(str(text)).split()).replace("|", r"\|")


def _counts(records: list[dict], stage: str) -> str:
    c = collections.Counter(r[stage]["status"] for r in records)
    order = sorted(c, key=lambda s: (s != "ok", s))  # ok first, then by name
    return ", ".join(f"{c[s]} {s}" for s in order)


def _status_cell(stage: dict) -> str:
    """The status, marked when a timeout caused it, so a real pnr-fail differs from a starved one."""
    return stage["status"] + (" (timed out)" if stage.get("timed_out") else "")


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
            _cell(c)
            for c in (
                r["id"], r["title"], r["tiles"], _status_cell(b), lut,
                f"{fmax} / {r['clock_hz'] / 1e6:g}", _status_cell(ref), ref["mode"] or "-", frames,
            )
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
    md = Path(args.out_md) if args.out_md else paths.WORK / "results" / "results.md"
    _write(md, render_markdown(records))
    print(f"{len(records)} designs: {paths.WORK / 'results' / 'index.json'}, {md}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("results", help="write the results index and the Markdown table")
    p.add_argument(
        "--out-md",
        default=None,
        help="Markdown table to write (default: results/results.md in the work directory)",
    )
    p.set_defaults(func=_cmd_results)
