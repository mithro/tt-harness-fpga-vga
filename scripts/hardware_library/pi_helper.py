"""Pi-side helper for hw_batch.py (copied to the Pi, run with the system python3; stdlib only).

usage: python3 pi_helper.py health | stop-if-idle | kill-ttcap | wait-loaded SECONDS

health        print the daemon's /health JSON (exit 1 if the daemon does not answer)
stop-if-idle  read /health until clients is 0 on 3 reads 2 s apart (the daemon's own idle task can show as
              a client for a moment), then `sudo systemctl stop fpgas-tt`; exit 3 if a client stays connected,
              7 if the daemon does not answer
kill-ttcap    stop any ttcap process of this user started from ~/tthfv-batch (SIGINT, then SIGKILL after 10 s)
              and check /dev/ttboard is not held open by any of this user's processes other than the daemon's
              MainPID (the daemon runs as pi too); exit 4 if it still is
wait-loaded   poll /health until idle_display.state is 'loaded' (exit 0), 'in use' (exit 6: a visitor has
              the board) or SECONDS pass (exit 5); a daemon restart resets the state to 'waiting', so a
              'loaded' seen after a restart is fresh
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

HEALTH = "http://127.0.0.1:8765/health"
BATCH_DIR = os.path.expanduser("~/tthfv-batch")


def health() -> dict:
    with urllib.request.urlopen(HEALTH, timeout=10) as r:
        return json.loads(r.read())


def ttcap_pids() -> list[int]:
    me, pids = os.getuid(), []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != me:
                continue
            with open(f"/proc/{d}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        try:
            cwd = os.readlink(f"/proc/{d}/cwd")
        except OSError:
            cwd = ""
        ours = cwd.startswith(BATCH_DIR) or any(BATCH_DIR.encode() in a for a in argv)
        if ours and any(a.endswith(b"/ttcap") or a == b"ttcap" for a in argv):
            pids.append(int(d))
    return pids


def port_holders() -> list[int]:
    target, me, held = os.path.realpath("/dev/ttboard"), os.getuid(), []
    out = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", "fpgas-tt"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    daemon = int(out) if out.isdigit() else 0
    for d in os.listdir("/proc"):
        if not d.isdigit() or (daemon and int(d) == daemon):
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != me:
                continue
            for fd in os.listdir(f"/proc/{d}/fd"):
                if os.path.realpath(f"/proc/{d}/fd/{fd}") == target:
                    held.append(int(d))
                    break
        except OSError:
            continue
    return held


def stop_if_idle() -> int:
    for i in range(3):
        c = health().get("clients")
        print(f"clients {c}", flush=True)
        if c != 0:
            time.sleep(5)
            c = health().get("clients")
            print(f"clients {c} (re-read)", flush=True)
            if c != 0:
                return 3
        if i < 2:
            time.sleep(2)
    return subprocess.run(["sudo", "systemctl", "stop", "fpgas-tt"], check=False).returncode


def main() -> int:
    cmd = sys.argv[1]
    if cmd == "health":
        try:
            print(json.dumps(health()))
        except OSError as exc:
            print(f"no answer: {exc}")
            return 1
        return 0
    if cmd == "stop-if-idle":
        try:
            return stop_if_idle()
        except OSError as exc:
            print(f"no answer from the daemon: {exc}", flush=True)
            return 7
    if cmd == "kill-ttcap":
        pids = ttcap_pids()
        print(f"ttcap pids {pids}", flush=True)
        for p in pids:
            os.kill(p, signal.SIGINT)
        for _ in range(10):
            if not ttcap_pids():
                break
            time.sleep(1)
        for p in ttcap_pids():
            print(f"SIGKILL {p}", flush=True)
            os.kill(p, signal.SIGKILL)
        time.sleep(1)
        held = port_holders()
        print(f"/dev/ttboard held by {held}", flush=True)
        return 4 if held else 0
    if cmd == "wait-loaded":
        end = time.time() + float(sys.argv[2])
        state = None
        while time.time() < end:
            try:
                state = (health().get("idle_display") or {}).get("state")
            except OSError as exc:
                state = f"no answer: {exc}"
            if state == "loaded":
                print("loaded", flush=True)
                return 0
            if state == "in use":
                print("in use: a visitor has the board", flush=True)
                return 6
            time.sleep(5)
        print(f"not loaded: {state}", flush=True)
        return 5
    print(f"unknown command {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
