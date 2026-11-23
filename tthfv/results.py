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


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fetch(work: Path, design: Design, outcome: dict) -> dict:
    if (o := outcome.get("fetch")) is not None:
        return {"status": o["status"], "reason": o["reason"]}
    if (work / "src" / design.id / "fetch.json").exists():
        return {"status": "ok", "reason": ""}
    return {"status": "not-run", "reason": None}


def _build(work: Path, design: Design, outcome: dict) -> dict:
    b = _read(work / "build" / design.id / "build.json")
    if b is None:
        o = outcome.get("build") or {"status": "not-run", "reason": None}
        status, reason = o["status"], o["reason"]
        b = {}
    else:
        status, reason = b["status"], b["reason"]
    return {
        "status": status,
        "reason": reason,
        "lut_percent": b.get("lut_percent"),
        "fmax_hz": b.get("fmax_hz"),
        "recipe": b.get("recipe"),
        "attempts": len(b["attempts"]) if "attempts" in b else None,
        "bitstream_sha256": b.get("bitstream_sha256"),
    }


def _reference(work: Path, design: Design, outcome: dict) -> dict:
    folder = work / "reference" / design.id
    none = {"mode": None, "width": None, "height": None, "frames_complete": None, "hash": None}
    ref = _read(folder / "reference.json")
    if ref is not None:
        first = ref["frames"][0]["hash"] if ref.get("frames") else None
        return {
            "status": "ok" if ref["frames_complete"] else "no-sync",
            "reason": "",
            "mode": ref["mode"],
            "width": ref["width"],
            "height": ref["height"],
            "frames_complete": ref["frames_complete"],
            "hash": first,
        }
    fail = _read(folder / "reference-fail.json") or outcome.get("reference")
    if fail is not None:
        return {"status": fail["status"], "reason": fail["reason"], **none}
    return {"status": "not-run", "reason": None, **none}


def collect(designs: list[Design], work: Path | None = None) -> list[dict]:
    """One record per design, sorted by id."""
    work = paths.WORK if work is None else work
    records = []
    for d in sorted(designs, key=lambda d: d.id):
        outcome = _read(work / "run" / d.id / "outcome.json") or {}
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
