# SPDX-License-Identifier: Apache-2.0
"""Join the manifest with what each stage wrote, as work/results/index.json and docs/results.md.

Sources, per design, under WORK: src/<id>/fetch.json, build/<id>/build.json,
reference/<id>/reference.json and reference-fail.json, and run/<id>/outcome.json (what the runner saw
last, which is the only record of a failed fetch). A stage with no record is `not-run`.

index.json has a generation time. docs/results.md has none, so it only changes when a result does.
"""

import argparse
import collections
import json
from datetime import UTC, datetime
from pathlib import Path

from tthfv import manifest, paths
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


_BUILD_NULLS = {
    "lut_percent": None, "fmax_hz": None, "recipe": None, "attempts": None, "bitstream_sha256": None,
}  # fmt: skip
_REF_NULLS = {"mode": None, "width": None, "height": None, "frames_complete": None, "hash": None}


def _fetch(work: Path, design: Design, outcome: dict) -> dict:
    name = f"run/{design.id}/outcome.json"
    try:
        if (o := _from_outcome(outcome, "fetch", name)) is not None:
            return o
        if (work / "src" / design.id / "fetch.json").exists():
            return {"status": "ok", "reason": ""}
        return {"status": "not-run", "reason": None}
    except Invalid as e:
        return _invalid(str(e), {})


def _build(work: Path, design: Design, outcome: dict) -> dict:
    name = f"build/{design.id}/build.json"
    try:
        b = _read(work / name, work)
        if b is None:
            o = _from_outcome(outcome, "build", f"run/{design.id}/outcome.json")
            return {**(o or {"status": "not-run", "reason": None}), **_BUILD_NULLS}
        return {
            "status": _need(b, "status", name),
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


def _reference(work: Path, design: Design, outcome: dict) -> dict:
    name = f"reference/{design.id}/reference.json"
    try:
        ref = _read(work / name, work)
        if ref is not None:
            complete = _need(ref, "frames_complete", name)
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


def collect(designs: list[Design], work: Path | None = None) -> list[dict]:
    """One record per design, sorted by id."""
    work = paths.WORK if work is None else work
    records = []
    for d in sorted(designs, key=lambda d: d.id):
        try:
            outcome = _read(work / "run" / d.id / "outcome.json", work) or {}
        except Invalid:
            outcome = {}  # supplementary: a damaged one is as good as none; the stage files decide
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
                "fetch": _fetch(work, d, outcome),
                "build": _build(work, d, outcome),
                "reference": _reference(work, d, outcome),
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
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.rename(path)


def _cmd_results(args: argparse.Namespace) -> int:
    records = collect(manifest.load(extras=manifest.EXTRAS_DEFAULT))
    index = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(records),
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
