# SPDX-License-Identifier: Apache-2.0
"""`tthfv board status [<slug>...]` and `tthfv board probe <slug>`."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from tthfv import paths
from tthfv.board import BoardError, probe, status
from tthfv.board.session import SessionReport, board_session

SESSION_ARGS: dict = {}  # extra board_session arguments (tests shorten the polls)
PROBE_TIMEOUT_S = 15.0
ALL_BOARDS = ("fpga-1", "fpga-3", "fpga-4", "tt-fd1a167bd863a198")  # status reads only, for fpga-1


def _say(msg: str) -> None:
    t = dt.datetime.now(dt.UTC).strftime("%H:%M:%S")
    print(f"[{t}Z] {msg}", file=sys.stderr, flush=True)


def _cmd_status(args: argparse.Namespace) -> int:
    for slug in args.slugs or ALL_BOARDS:
        try:
            s = status.read(slug)
        except BoardError as e:
            print(f"{slug}: {e}")
            continue
        ok, why = status.usable(s)
        verdict = "usable" if ok else f"not usable: {why}"
        print(f"{slug}  serial {s.usb_serial}  clients {s.clients}  idle {s.idle_state!r}  {verdict}")
        if args.json:
            print(json.dumps(s.raw, sort_keys=True))
    return 0


def _default_out(slug: str) -> Path:
    work = Path(os.environ.get("TTHFV_WORK") or paths.REPO_ROOT / "work")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return work / "board" / f"{slug}-{stamp}.json"


def _cmd_probe(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else _default_out(args.slug)
    report = SessionReport()
    record: dict = {"slug": args.slug, "prompt_only": args.prompt_only}
    held: dict = {}
    purpose = "read-only connect check" if args.prompt_only else "read-only probe"
    _say(f"{args.slug}: checking status, then a session ({purpose})")
    try:
        kwargs = {"max_connected_s": args.max_connected_s, **SESSION_ARGS}
        with board_session(args.slug, purpose, report=report, **kwargs) as link:
            held["link"] = link
            _say(f"{args.slug}: connected in {link.connect_s:.2f} s, clients verified 1")
            if args.prompt_only:
                record["prompt"] = link.friendly_prompt().decode("utf-8", "replace")
                _say(f"{args.slug}: prompt reply {record['prompt']!r}")
            else:
                link.enter_raw()
                t = time.monotonic()
                stdout = link.exec_raw(probe.PROBE_SOURCE, PROBE_TIMEOUT_S)
                record["probe_round_trip_s"] = round(time.monotonic() - t, 3)
                text = stdout.decode("utf-8", "replace").strip()
                record["probe_stdout"] = text
                result = json.loads(text.splitlines()[-1])
                record["probe"] = result
                record["registers"] = probe.decode_registers(result.get("regs") or {})
                _say(f"{args.slug}: probe answered in {record['probe_round_trip_s']} s")
            _say(f"{args.slug}: leaving (clean-up, disconnect, release, wait for 'loaded')")
    except BaseException as e:
        record["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        record["session"] = report.as_dict()
        if "link" in held:
            record["transcript"] = held["link"].transcript
            record["notices"] = held["link"].notices
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=2) + "\n")
        _say(f"{args.slug}: record written to {out}")
    print(json.dumps({k: v for k, v in record.items() if k in ("probe", "registers", "prompt")}, indent=2))
    print(
        f"connect {report.connect_s} s, connected {report.connected_s} s, "
        f"time to loaded after disconnect {report.time_to_loaded_s} s, outcome {report.outcome}"
    )
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("board", help="the public Tiny Tapeout FPGA boards: status and a read-only probe")
    bsub = p.add_subparsers(dest="board_command", metavar="<board command>")
    bsub.required = True
    s = bsub.add_parser("status", help="read the public status of boards (no connection)")
    s.add_argument("slugs", nargs="*", help=f"board slugs (default: {' '.join(ALL_BOARDS)})")
    s.add_argument("--json", action="store_true", help="also print each raw status JSON")
    s.set_defaults(func=_cmd_status)
    pr = bsub.add_parser("probe", help="one read-only session on a board, following the sharing rules")
    pr.add_argument("slug")
    pr.add_argument("--prompt-only", action="store_true", help="only confirm the friendly prompt (send a CR)")
    pr.add_argument(
        "--out", default=None, help="session record to write (default: work/board/<slug>-<time>.json)"
    )
    pr.add_argument(
        "--max-connected-s",
        type=float,
        default=120.0,
        help="cap on connected time (default 120 s; a probe needs ~15)",
    )
    pr.set_defaults(func=_cmd_probe)
