# SPDX-License-Identifier: Apache-2.0
"""`tthfv board compare-capture`: compare a capture recorded on a board by vgacap's `ttcap capture` with the
design's reference for that board's profile. Offline: no board is involved.

The capture is read by tthfv.board.capture_file (any recorded gap refuses it), placed in the reference by
compare.locate_capture (it starts an unknown number of clocks after reset release) and compared over the
overlap. The record goes to WORK/validate/<shuttle>/<macro>/<profile>/result.json (or --out), with the
difference images on a mismatch, or of the first captured frame when the capture could not be placed.
"""

import argparse
import dataclasses
import json
from pathlib import Path

import numpy as np

from tthfv import compare, paths, reference, safefile
from tthfv.board.capture_file import CaptureData, load_capture
from tthfv.errors import UserError

RESULT_SCHEMA = 1
DEFAULT_PROFILE = paths.REPO_ROOT / "boards" / "fpga-4.json"


def _capture_meta(cap: CaptureData) -> dict:
    meta = dataclasses.asdict(cap)
    del meta["uo_out"]
    meta["time_messages"] = list(cap.time_messages)
    return meta


def _start_disagreement(cap: np.ndarray, ref: np.ndarray, anchor: int) -> dict:
    """Where the capture's first `anchor` samples first differ from the reference's first clocks."""
    n = min(anchor, len(cap), len(ref))
    xor = cap[:n] ^ ref[:n]
    differing = np.flatnonzero(xor)
    return {
        "compared_clocks": n,
        "first_diff_clock": int(differing[0]) if len(differing) else None,
        "diff_clocks": len(differing),
        "first_diffs": [
            {"clock": int(i), "reference": int(ref[i]), "capture": int(cap[i])} for i in differing[:16]
        ],
    }


def _cmd(args: argparse.Namespace) -> int:
    cap = load_capture(args.capture)
    if cap.edge == "rising" and not args.allow_rising_edge:
        raise UserError(
            f"{args.capture}: the header says edge=rising. A rising-edge sample is read within a few ns of the "
            "edge, before uo_out has settled; capture with ttcap's default (falling: the end of the high phase, "
            "which is the reference's uo_out.bin byte), or pass --allow-rising-edge to compare it anyway"
        )
    prof = reference.load_profile(str(args.profile))
    try:
        ref_dir = reference.reference_dir(args.design, prof.name)
        out = (
            Path(args.out) if args.out is not None else paths.design_dir("validate", args.design) / prof.name
        )
    except ValueError as e:
        raise UserError(str(e)) from e
    print(
        f"capture: {cap.samples} samples at {cap.clock_hz} Hz, design {cap.design}, board profile "
        f"{cap.board_profile}, edge {cap.edge}; closing summary {cap.summary!r}"
    )
    if cap.edge is None:
        print(
            "note: the capture's header does not record the sampling edge (no edge= in its desc); "
            "compared as if sampled on the falling edge, ttcap's default"
        )
    record: dict = {
        "schema": RESULT_SCHEMA, "capture": str(args.capture), "design": args.design, "profile": prof.name,
        "reference": str(ref_dir), "capture_meta": _capture_meta(cap), "verdict": None,
        "anchor_clocks": compare.ANCHOR_CLOCKS, "max_start_clocks": args.max_start_clocks,
        "capture_start_clock": None, "offset": None, "candidates": None, "earliest_candidate_clock": None,
        "comparison": None, "report": None, "images": None, "reason": "",
    }  # fmt: skip
    expect = {"profile": {"name": prof.name, "data": prof.data}}
    code = 1
    try:
        ref = compare.load_reference(ref_dir)
        key = ref.meta.get("key")
        if not isinstance(key, dict):
            raise compare.Refused("reference.json has no key")
        compare._check_key(expect, key)
        record["reference_phase_sensitive"] = ref.meta.get("phase_sensitive")
        if ref.meta.get("phase_sensitive"):
            print(
                "note: this reference is phase_sensitive (uo_out changes between the high and the low phase of "
                "a clock: negative-edge registers or a combinational path from the clock), so a correct capture "
                "may legitimately differ from its high-phase dump, which is what the capture is compared with"
            )
        max_start = (
            args.max_start_clocks if args.max_start_clocks is not None else compare.start_window(ref.meta)
        )
        record["max_start_clocks"] = max_start
        loc = compare.locate_capture(cap.uo_out, ref.samples, max_start_clocks=max_start)
        record.update(candidates=loc.candidates, earliest_candidate_clock=loc.earliest_clock)
        if loc.start_clock is None:
            record["verdict"] = "no-match"
            dis = _start_disagreement(cap.uo_out, ref.samples, loc.anchor_clocks)
            record["start_disagreement"] = dis
            print(
                f"no match: the capture's first {loc.anchor_clocks} samples are nowhere in the reference's "
                f"first {max_start} clocks"
            )
            print(
                f"against reference clock 0 they first differ at clock {dis['first_diff_clock']} "
                f"({dis['diff_clocks']} of {dis['compared_clocks']} differ)"
            )
            for d in dis["first_diffs"]:
                print(f"  clock {d['clock']}: reference {d['reference']:#04x} capture {d['capture']:#04x}")
            record["images"] = compare.write_diff_images(cap.uo_out, ref_dir, out, first_capture_frame=True)
        else:
            offset = -loc.start_clock
            record.update(capture_start_clock=loc.start_clock, offset=offset)
            print(
                f"first sample is reference clock {loc.start_clock} ({loc.candidates} candidate place(s) for "
                f"the first {loc.anchor_clocks} samples in the first {max_start} clocks, earliest "
                f"{loc.earliest_clock})"
            )
            c = compare.compare(
                cap.uo_out, ref_dir, build_json=None, offset=offset, expect_key=expect, overlap_only=True
            )
            record["comparison"] = dataclasses.asdict(c)
            record["verdict"] = c.verdict
            record["reason"] = c.reason
            sizes = f"{c.compared_clocks} clocks compared (capture {c.capture_clocks}, reference {c.reference_clocks})"
            if c.verdict == "refused":
                raise compare.Refused(c.reason)
            print(f"{c.verdict}: {sizes}, offset {c.offset}")
            if c.reason:
                print(c.reason)
            if c.verdict == "exact":
                code = 0
            else:
                rep = compare.diff_report(cap.uo_out, ref.samples, c, reference_meta=ref.meta)
                record["report"] = rep
                compare.print_details(c, rep)
                record["images"] = compare.write_diff_images(
                    cap.uo_out, ref_dir, out, offset=offset, overlap_only=True
                )
    except compare.Refused as e:
        record.update(verdict="refused", reason=str(e))
        print(f"refused: {e}")
    if record["images"]:
        for name, path in record["images"]["files"].items():
            print(f"{name}: {path}")
        if record["images"]["note"]:
            print(record["images"]["note"])
    out.mkdir(parents=True, exist_ok=True)
    safefile.write_file(out / "result.json", json.dumps(record, indent=2) + "\n")
    print(f"result: {out / 'result.json'}")
    return code


def add_parser(bsub) -> None:
    p = bsub.add_parser(
        "compare-capture",
        help="compare a vgacap capture file (ttcap capture) with the design's board-profile reference (offline)",
    )
    p.add_argument("capture", type=Path, help="the .vgacap stream ttcap capture wrote")
    p.add_argument("design", help="design id, <shuttle>/<macro>")
    p.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="board profile the reference was made under (default boards/fpga-4.json)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="where result.json and images go (default work/validate/<shuttle>/<macro>/<profile>)",
    )
    p.add_argument(
        "--max-start-clocks",
        type=int,
        default=None,
        help="search for the capture's start in this many reference clocks (default two frames of the reference)",
    )
    p.add_argument(
        "--allow-rising-edge",
        action="store_true",
        help="compare a capture sampled on the rising edge (refused by default: uo_out has not settled there)",
    )
    p.set_defaults(func=_cmd)
