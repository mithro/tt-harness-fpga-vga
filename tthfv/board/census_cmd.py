# SPDX-License-Identifier: Apache-2.0
"""`tthfv board census <slug>`: the pin census session, and `--info`, its read-only look at the board first."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from tthfv import paths
from tthfv.board import SessionTerminated, census, probe
from tthfv.board import command as base
from tthfv.board.session import SessionReport, board_session

CENSUS_TIMEOUT_S = 30.0


def _record_path(slug: str, kind: str) -> Path:
    work = Path(os.environ.get("TTHFV_WORK") or paths.REPO_ROOT / "work")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return work / "board" / f"{slug}-{kind}-{stamp}.json"


def _session(
    args: argparse.Namespace, purpose: str, body
) -> tuple[SessionReport, dict, SessionTerminated | None]:
    """Run `body(link, record)` in a board session; always return the report and the record (the caller writes
    it), and re-raise anything but a termination signal after filling `record['error']`."""
    report = SessionReport()
    record: dict = {"slug": args.slug, "purpose": purpose}
    held: dict = {}
    terminated: SessionTerminated | None = None
    base._say(f"{args.slug}: checking status, then a session ({purpose})")
    try:
        with board_session(
            args.slug, purpose, report=report, max_connected_s=args.max_connected_s, **base.SESSION_ARGS
        ) as link:
            held["link"] = link
            base._say(f"{args.slug}: connected in {link.connect_s:.2f} s, clients verified 1")
            body(link, record)
            base._say(f"{args.slug}: leaving (clean-up, disconnect, release, wait for 'loaded')")
    except SessionTerminated as e:
        record["error"] = f"SessionTerminated: {e}"
        terminated = e
    except BaseException as e:
        record["error"] = f"{type(e).__name__}: {e}"
        record["session"] = report.as_dict()
        _finish(record, held)
        raise _Failed(record, e) from e
    record["session"] = report.as_dict()
    _finish(record, held)
    return report, record, terminated


def _finish(record: dict, held: dict) -> None:
    if "link" in held:
        record["transcript"] = held["link"].transcript
        record["notices"] = held["link"].notices


class _Failed(Exception):
    """The session failed; the record is still to be written before the original error is raised."""

    def __init__(self, record: dict, original: BaseException):
        super().__init__(str(original))
        self.record = record
        self.original = original


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    base._say(f"record written to {path}")


def _timing_line(report: SessionReport) -> str:
    return (
        f"pre-connect wait {report.pre_connect_wait_s} s, connect {report.connect_s} s, verified clients "
        f"{report.verified_clients} after {report.confirm_s} s, connected {report.connected_s} s, time to loaded "
        f"after disconnect {report.time_to_loaded_s} s, outcome {report.outcome}"
    )


def _run(args: argparse.Namespace) -> int:
    if args.info:
        return _run_info(args)
    purpose = "pin census"
    record_path = _record_path(args.slug, "census")
    raw_text: dict = {}

    def body(link, record: dict) -> None:
        link.enter_raw()
        out = link.exec_raw(census.census_source(), CENSUS_TIMEOUT_S, changes_state=True, label="pin census")
        raw_text["stdout"] = out.decode("utf-8", "replace")
        record["stdout"] = raw_text["stdout"]

    try:
        report, record, terminated = _session(args, purpose, body)
    except _Failed as f:
        _write(record_path, f.record)
        raise f.original from None
    if terminated is not None:
        _write(record_path, record)
        return 128 + terminated.signum
    try:
        raw = census.parse_output(raw_text["stdout"])
        profile = census.build_profile(raw, usb_serial=_serial(args.slug), date=census.today())
        census.validate_profile(profile)
    except census.CensusError as e:
        record["error"] = f"CensusError: {e}"
        _write(record_path, record)
        raise
    record["raw"] = raw
    out = Path(args.out) if args.out else paths.REPO_ROOT / "boards" / f"{args.slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, indent=2) + "\n")
    _write(record_path, record)
    print(json.dumps(profile, indent=2))
    print(_timing_line(report))
    if args.check_against:
        other = json.loads(Path(args.check_against).read_text())
        census.validate_profile(other)
        diff = census.differences(other, profile)
        if diff:
            print("tthfv: the profiles DISAGREE:\n  " + "\n  ".join(diff), file=sys.stderr)
            return 1
        print(f"the two profiles agree (ignoring the date): {args.check_against} and {out}")
    return 0


def _serial(slug: str) -> str:
    from tthfv.board import status

    serial = status.expected_serial(slug)
    if serial is None:
        raise census.CensusError(f"{slug} has no known USB serial")
    return serial


def _run_info(args: argparse.Namespace) -> int:
    record_path = Path(args.out) if args.out else _record_path(args.slug, "info")

    def body(link, record: dict) -> None:
        link.enter_raw()
        text = link.exec_raw(probe.PROBE_SOURCE, base.PROBE_TIMEOUT_S, changes_state=False).decode(
            "utf-8", "replace"
        )
        record["probe"] = json.loads(text.strip().splitlines()[-1])
        record["registers"] = probe.decode_registers(record["probe"].get("regs") or {})
        text = link.exec_raw(census.info_source(), base.PROBE_TIMEOUT_S, changes_state=False).decode(
            "utf-8", "replace"
        )
        record["info"] = json.loads(text.strip().splitlines()[-1])

    try:
        report, record, terminated = _session(args, "read-only look before the pin census", body)
    except _Failed as f:
        _write(record_path, f.record)
        raise f.original from None
    _write(record_path, record)
    if terminated is not None:
        return 128 + terminated.signum
    print(json.dumps({k: record[k] for k in ("probe", "registers", "info")}, indent=2))
    print(_timing_line(report))
    return 0


def add_parser(bsub) -> None:
    c = bsub.add_parser(
        "census", help="pin census of a board (changes pin pulls and restores them), or --info"
    )
    c.add_argument("slug")
    c.add_argument(
        "--out", default=None, help="profile to write (default boards/<slug>.json); with --info, the record"
    )
    c.add_argument(
        "--check-against", default=None, help="a profile from an earlier run: exit 1 if this one differs"
    )
    c.add_argument(
        "--info", action="store_true", help="only a read-only look (runtime, SDK, chip, each pin as found)"
    )
    c.add_argument(
        "--max-connected-s", type=float, default=120.0, help="cap on connected time (default 120 s)"
    )
    c.set_defaults(func=_run)
