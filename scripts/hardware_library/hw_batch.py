"""Validate published library bitstreams on the fpga-4 Tiny Tapeout FPGA board, one after another.

usage: hw_batch.py --alias fpgas-welland-pi-sw2-p35 --out DIR [--minutes 45] [--max N]
                   [--clock-hz 400000] [--only shuttle/macro ...] [--dry-run]

PRECONDITION: the caller holds a broker claim on fpga-4 (fpgas-online MCP claim_board) for at least
--minutes. This script does not claim. Only fpga-4 is supported: the references are made with its measured
pin profile (boards/fpga-4.json).

Visitors: "visitor" means a client of the daemon's serial bridge (/health `clients`). Someone only watching
the board's camera is not seen by this check.

Per design: check the Pi's /health for visitors; copy the library .bin to the Pi; POST it to the daemon as
the single upload `tthfv_batch` (re-uploading the same name replaces it, so the daemon's 16-upload store loses
at most one slot) and enable it; on the Pi, stop fpgas-tt only if clients stays 0 (pi_helper stop-if-idle);
run `ttcap capture` on the local serial port under `timeout -s INT`; stop any leftover ttcap and check the
port is free; start fpgas-tt; copy the capture back; run `tthfv board compare-capture`.

End of batch (also on an error, Ctrl-C, SIGTERM or SIGHUP): if a visitor is connected, leave the daemon
running and do not reboot (exit 2, "restore deferred"); otherwise stop fpgas-tt, stop leftover ttcap,
`ttcap reboot --to flash`, start fpgas-tt, wait for a fresh idle_display 'loaded'. Every step is
best-effort, and the last step always attempts `systemctl start fpgas-tt`. Exit 3 with "BOARD NOT
RESTORED" if 'loaded' is not confirmed. Signals are
ignored during the restore (and by its children), so a hung restore needs SIGKILL; it is bounded by
RESTORE_RESERVE_S. Exit 1 if any design ended in an error. Exit 0 otherwise.
"""

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]  # the harness checkout
LIVE = Path(
    os.environ.get("TTHFV_LIVE_DIR", "/srv/data.wafer.space/tinytapeout-vga-videos")
)  # published library
SSH_CONFIG = Path.home() / ".config/fpgas-online/ssh_config"
HELPER = Path(__file__).with_name("pi_helper.py")
BOARD, PROFILE = "fpga-4", "boards/fpga-4.json"
PI_DIR = "tthfv-batch"  # under the Pi user's home
PI_UPLOAD_FILE = "/var/lib/fpgas-tt/uploads/tthfv_batch.bin"  # the daemon's StateDirectory, owned by pi
UPLOAD = "tthfv_batch"
RESTORE_RESERVE_S = (
    600  # kept free before --minutes ends; restore() is bounded by this (about 90 s when it goes well)
)
FIRST_DESIGN_BUDGET_S = 120  # until a design has been timed
MAX_CONSECUTIVE_ERRORS = 3
SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=20",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
]


class Stop(Exception):
    """SIGTERM or SIGHUP: unwind through the finally blocks like Ctrl-C does."""


def on_signal(signum, _frame):
    raise Stop(f"signal {signum}")


def log_line(out: Path, text: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {text}"
    print(line, flush=True)
    with (out / "batch.log").open("a") as f:
        f.write(line + "\n")


class Board:
    def __init__(self, alias: str, out: Path, deadline: float):
        self.alias, self.out, self.deadline = alias, out, deadline
        self.daemon_stopped = False
        self.touched = False  # an upload or enable reached the daemon, or the daemon was stopped

    def remaining(self) -> float:
        return self.deadline - time.time()

    def run(self, log: Path, argv: list[str], timeout: float) -> int:
        timeout = max(10.0, timeout)
        with log.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S', time.gmtime())} $ {shlex.join(argv)}\n")
            f.flush()
            p = subprocess.Popen(
                argv, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
            )  # Ctrl-C reaches only this script, which then cleans up
            try:
                rc = p.wait(timeout=timeout)
            except BaseException as exc:
                p.kill()
                p.wait()
                f.write(
                    f"[{f'timeout after {timeout:.0f} s' if isinstance(exc, subprocess.TimeoutExpired) else repr(exc)}"
                    f"; local process killed]\n"
                )
                raise
            f.write(f"[exit {rc}]\n")
        return rc

    def ssh(self, log: Path, command: str, timeout: float = 120) -> int:
        return self.run(log, ["ssh", "-F", str(SSH_CONFIG), *SSH_OPTS, self.alias, command], timeout)

    def scp_up(self, log: Path, src: Path, dst: str) -> int:
        return self.run(
            log, ["scp", "-q", "-F", str(SSH_CONFIG), *SSH_OPTS, str(src), f"{self.alias}:{dst}"], 120
        )

    def scp_down(self, log: Path, src: str, dst: Path) -> int:
        return self.run(
            log, ["scp", "-q", "-F", str(SSH_CONFIG), *SSH_OPTS, f"{self.alias}:{src}", str(dst)], 120
        )

    def helper(self, log: Path, args: str, timeout: float = 60) -> int:
        return self.ssh(log, f"python3 {PI_DIR}/pi_helper.py {args}", timeout)

    def clients(self, log: Path) -> int | None:
        """The Pi's own /health clients count (the public status.json can lag); None if no answer."""
        tmp = log.with_suffix(".health")
        tmp.unlink(missing_ok=True)
        rc = self.run(
            tmp,
            ["ssh", "-F", str(SSH_CONFIG), *SSH_OPTS, self.alias, f"python3 {PI_DIR}/pi_helper.py health"],
            60,
        )
        text = tmp.read_text()
        with log.open("a") as f:
            f.write(text)
        if rc != 0:
            return None
        for line in reversed(text.splitlines()):
            if line.startswith("{"):
                try:
                    return json.loads(line).get("clients")
                except ValueError:
                    return None
        return None

    def daemon_active(self, log: Path) -> bool | None:
        """systemctl's view on the Pi: True active, False not active, None no answer."""
        rc = self.ssh(log, "systemctl is-active --quiet fpgas-tt", 30)
        return True if rc == 0 else (None if rc == 255 else False)

    def stop_if_idle(self, log: Path) -> bool:
        rc = self.helper(log, "stop-if-idle", 60)
        if rc == 0:
            self.daemon_stopped = True
            return True
        if rc == 3:
            return False
        raise RuntimeError(f"stop-if-idle exit {rc}")

    def start_daemon(self, log: Path, strict: bool = True) -> None:
        """Free the port of any ttcap of ours, then start fpgas-tt and wait for /health. With strict False a
        port still held is logged and the daemon is started anyway (the last step of a restore)."""
        try:
            freed = self.helper(log, "kill-ttcap", 60) == 0
        except Exception as exc:
            if strict:
                raise
            freed = False
            with log.open("a") as f:
                f.write(f"kill-ttcap did not run: {exc!r}\n")
        if not freed:
            if strict:
                raise RuntimeError("a process of ours still holds /dev/ttboard")
            with log.open("a") as f:
                f.write("kill-ttcap failed; starting the daemon anyway\n")
        if self.ssh(log, "sudo systemctl start fpgas-tt", 60) != 0:
            raise RuntimeError("could not start fpgas-tt")
        self.daemon_stopped = False
        for _ in range(20):
            if self.helper(log, "health", 20) == 0:
                return
            time.sleep(2)
        raise RuntimeError("fpgas-tt did not answer /health within about 60 s of start")


def pick_designs(only: list[str]) -> list[dict]:
    lib = json.loads((LIVE / "fpga-library.json").read_text())
    ref = HARNESS / "work/reference"
    if not ref.is_dir():
        sys.exit(f"no references at {ref}: run from the harness checkout that built them")
    picks = [
        e
        for e in lib["designs"]
        if e["build_status"] == "ok"
        and e["ui_in"] in (None, 0)
        and not e["pdk_latches"]
        and (ref / e["shuttle"] / e["macro"] / BOARD).exists()
    ]
    if only:
        want = set(only)
        picks = [e for e in picks if f"{e['shuttle']}/{e['macro']}" in want]
        missing = want - {f"{e['shuttle']}/{e['macro']}" for e in picks}
        if missing:
            sys.exit(f"not eligible or unknown: {sorted(missing)}")
    return picks


def design_dir(out: Path, e: dict) -> Path:
    return out / f"{e['shuttle']}__{e['macro']}"


def verdict_of(d: Path) -> str | None:
    res = d / "result" / "result.json"
    if not res.exists():
        return None
    try:
        return json.loads(res.read_text()).get("verdict")
    except ValueError:
        return "unreadable result.json"


def validate_one(b: Board, e: dict, args) -> str:
    key = f"{e['shuttle']}/{e['macro']}"
    d = design_dir(args.out, e)
    if d.exists():  # an earlier attempt that did not end in 'exact': keep it beside the new one
        d.rename(d.with_name(d.name + time.strftime(".attempt-%Y%m%dT%H%M%SZ", time.gmtime())))
    d.mkdir(parents=True)
    log = d / "commands.log"
    binp = LIVE / e["bin"]
    data = binp.read_bytes()
    (d / "design.json").write_text(
        json.dumps(
            {
                "key": key,
                "upload_name": UPLOAD,
                "bin": e["bin"],
                "bin_sha256": hashlib.sha256(data).hexdigest(),
                "bin_bytes": len(data),
                "clock_hz_capture": args.clock_hz,
                "board": BOARD,
                "ui_in_driven": args.ui_in,
                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        )
    )
    c = b.clients(log)
    if c is None:
        return "error: no /health answer"
    if c != 0:
        return f"visitor: clients {c} before upload"
    pi_bin, pi_cap = f"{PI_DIR}/{UPLOAD}.bin", f"{PI_DIR}/{UPLOAD}.vgacap"
    if b.scp_up(log, binp, pi_bin) != 0:
        return "error: scp up"
    b.touched = True
    if (
        b.ssh(
            log,
            f"curl -sS --fail-with-body -F name={UPLOAD} -F file=@{pi_bin} http://127.0.0.1:8765/bitstream",
        )
        != 0
    ):
        return "error: upload"
    if (
        b.ssh(log, f"curl -sS --fail-with-body -X POST http://127.0.0.1:8765/designs/{UPLOAD}/enable", 120)
        != 0
    ):
        return "error: enable"
    if b.ssh(log, f"rm -f {pi_cap}") != 0:
        return "error: rm old capture"
    ui_in_arg = "" if args.ui_in is None else f" --ui-in {args.ui_in}"
    if not b.stop_if_idle(log):
        return "visitor: clients > 0 before the daemon stop"
    try:
        rc = b.ssh(
            log,
            f"cd {PI_DIR}/vgacap && timeout -s INT -k 15 180 uv run --no-sync ttcap capture serial:/dev/ttboard"
            f" --profile rp2350 --design {UPLOAD} --clock-hz {args.clock_hz} --mode stream --seconds 0"
            f" --frames 8 --desc board={BOARD}{ui_in_arg} --out ~/{pi_cap}",
            240,
        )
    finally:
        b.start_daemon(log)
    if rc != 0:
        return f"error: capture exit {rc}"
    if b.ssh(log, f"test -s {pi_cap}") != 0:
        return "error: no capture file"
    cap = d / "capture.vgacap"
    if b.scp_down(log, pi_cap, cap) != 0:
        return "error: scp down"
    b.ssh(log, f"rm -f {pi_bin} {pi_cap}")
    with (d / "compare.log").open("w") as f:
        rc = subprocess.run(
            [
                "uv",
                "run",
                "tthfv",
                "board",
                "compare-capture",
                str(cap),
                key,
                "--profile",
                PROFILE,
                "--out",
                str(d / "result"),
            ],
            cwd=HARNESS,
            stdout=f,
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
        ).returncode
    v = verdict_of(d)
    if v is None:
        return f"error: compare wrote no verdict (exit {rc})"
    return f"{v} (compare exit {rc})"


def restore(b: Board) -> int:
    """0 restored, 2 deferred for a visitor, 3 not restored. Re-derives the daemon's state from the Pi (a
    killed local ssh does not stop its remote command, so local flags can be stale). Bounded by
    RESTORE_RESERVE_S; the last step always attempts a daemon start."""
    log = b.out / "restore.log"
    end = (
        time.time() + RESTORE_RESERVE_S - 260
    )  # leave time for the final start (~60 s) and wait-loaded (170 s)
    problems: list[str] = []

    def left() -> float:
        return end - time.time()

    rebooted = False
    try:
        active = None
        for _ in range(3):
            active = b.daemon_active(log)
            if active is not None:
                break
            time.sleep(5)
        if active is None:
            problems.append("no answer from the Pi")
        else:
            if not active:
                b.start_daemon(log, strict=False)
            c = None
            for _ in range(3):
                c = b.clients(log)
                if c is not None or left() < 30:
                    break
                time.sleep(5)
            if c is None:
                problems.append("daemon gave no clients count")
            elif c != 0:
                log_line(
                    b.out,
                    f"restore deferred: clients {c}; daemon left running, board not rebooted"
                    f" (the board keeps {UPLOAD} until its next user loads something)",
                )
                return 2
            elif not b.stop_if_idle(log):
                log_line(
                    b.out, "restore deferred: a client connected; daemon left running, board not rebooted"
                )
                return 2
            else:
                b.ssh(log, f"rm -f {PI_UPLOAD_FILE}", 30)
                if b.helper(log, "kill-ttcap", 60) != 0:
                    problems.append("port held before reboot")
                for attempt in range(2):
                    if left() < 100:
                        problems.append("no time left for a reboot")
                        break
                    rc = b.ssh(
                        log,
                        f"cd {PI_DIR}/vgacap && timeout -s INT -k 15 60 uv run --no-sync ttcap reboot"
                        " --to flash serial:/dev/ttboard",
                        90,
                    )
                    if rc == 0:
                        rebooted = True
                        break
                    problems.append(f"reboot attempt {attempt + 1} exit {rc}")
                    time.sleep(5)
    except BaseException as exc:  # noqa: BLE001 - keep going to the daemon start below
        problems.append(f"restore step failed: {exc!r}")
    started = False
    for attempt in range(2):
        try:
            b.start_daemon(log, strict=False)
            started = True
            break
        except BaseException as exc:  # noqa: BLE001
            problems.append(f"daemon start attempt {attempt + 1}: {exc!r}")
            time.sleep(5)
    rc_wait = None
    if started:
        try:
            rc_wait = b.helper(log, "wait-loaded 170", 200)
        except BaseException as exc:  # noqa: BLE001
            problems.append(f"wait-loaded: {exc!r}")
    if started and rebooted and rc_wait in (0, 6):
        b.ssh(log, f"rm -rf {PI_DIR}", 30)
        how = (
            "idle_display loaded"
            if rc_wait == 0
            else (f"idle_display 'in use' after the reboot (clients {b.clients(log)})")
        )
        log_line(b.out, f"restored: {how}; problems along the way: {problems or 'none'}")
        return 0
    log_line(
        b.out,
        f"BOARD NOT RESTORED: daemon started {started}, rebooted {rebooted}, wait-loaded exit"
        f" {rc_wait}; problems {problems}; see {log}",
    )
    return 3


def summary(out: Path, picks: list[dict]) -> dict:
    tally: dict[str, int] = {}
    for e in picks:
        v = verdict_of(design_dir(out, e)) if design_dir(out, e).exists() else "not run"
        tally[str(v)] = tally.get(str(v), 0) + 1
    return tally


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument(
        "--vgacap-tar",
        type=Path,
        required=True,
        help="tar of a vgacap checkout (top directory vgacap/) to run ttcap from on the Pi",
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--minutes", type=float, default=45)
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--clock-hz", type=int, default=400000)
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-tried", action="store_true", help="skip designs that already have any verdict")
    ap.add_argument(
        "--ui-in",
        type=int,
        default=None,
        help="drive ui_in to this value during the capture (ttcap --ui-in: RP-control mode, restored after)",
    )
    args = ap.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    picks = pick_designs(args.only)
    todo = [e for e in picks if verdict_of(design_dir(args.out, e)) != "exact"]
    if args.skip_tried:  # only designs with no result yet
        todo = [e for e in todo if verdict_of(design_dir(args.out, e)) is None]
    if args.max:
        todo = todo[: args.max]
    log_line(args.out, f"eligible {len(picks)}, to do {len(todo)}, budget {args.minutes} min")
    if args.dry_run:
        for e in todo:
            print(f"{e['shuttle']}/{e['macro']}")
        return 0
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    b = Board(args.alias, args.out, time.time() + args.minutes * 60)
    setup = args.out / "setup.log"
    if b.ssh(setup, f"mkdir -p {PI_DIR}") != 0 or b.scp_up(setup, HELPER, f"{PI_DIR}/pi_helper.py") != 0:
        log_line(args.out, "could not reach the Pi; nothing was changed")
        return 3
    c = b.clients(setup)
    if c != 0:
        log_line(args.out, f"board not idle (clients {c}); nothing was changed")
        b.ssh(setup, f"rm -rf {PI_DIR}")
        return 2
    errors = consecutive = 0
    longest = FIRST_DESIGN_BUDGET_S
    rc_restore = 3
    try:
        if (
            b.scp_up(setup, args.vgacap_tar, f"{PI_DIR}/vgacap.tar") != 0
            or b.ssh(
                setup,
                f"tar -C {PI_DIR} -xf {PI_DIR}/vgacap.tar && cd {PI_DIR}/vgacap && UV_NO_DEV=1 uv sync",
                600,
            )
            != 0
        ):
            raise RuntimeError("Pi setup failed; see setup.log")
        for i, e in enumerate(todo, 1):
            if b.remaining() < longest + RESTORE_RESERVE_S:
                log_line(args.out, f"time budget reached; stopping before {i}/{len(todo)}")
                break
            t0 = time.time()
            try:
                outcome = validate_one(b, e, args)
            except (KeyboardInterrupt, Stop):
                raise
            except Exception as exc:  # noqa: BLE001 - recorded per design; repeated failures end the batch
                outcome = f"error: {exc!r}"
            took = time.time() - t0
            longest = max(longest, took)
            log_line(args.out, f"[{i}/{len(todo)}] {e['shuttle']}/{e['macro']}: {outcome} ({took:.0f} s)")
            if outcome.startswith("visitor"):
                log_line(args.out, "visitor seen: ending the batch")
                break
            if outcome.startswith("error"):
                errors += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    log_line(args.out, f"{consecutive} errors in a row: ending the batch")
                    break
            else:
                consecutive = 0
    except BaseException as exc:  # noqa: BLE001 - logged; the finally block restores the board
        log_line(args.out, f"batch interrupted: {exc!r}")
        errors += 1
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # the restore must not be cut short by a second Ctrl-C
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if b.touched:
            rc_restore = restore(b)
        else:
            b.ssh(setup, f"rm -rf {PI_DIR}", 30)
            log_line(args.out, "nothing reached the daemon: no restore needed")
            rc_restore = 0
        log_line(args.out, f"summary over {args.out.name}: {summary(args.out, picks)}")
    if rc_restore:
        return rc_restore
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
