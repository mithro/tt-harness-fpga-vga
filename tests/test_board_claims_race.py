# SPDX-License-Identifier: Apache-2.0
"""Review fix I7: replacing an expired claim must not let two processes both hold it."""

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CHILD = Path(__file__).with_name("claims_child.py")
SERIAL = "a2961e5cac65b25f"


def _wait_for(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def test_two_processes_replacing_an_expired_claim_one_wins(tmp_path):
    cdir, sync = tmp_path / "claims", tmp_path / "sync"
    cdir.mkdir()
    sync.mkdir()
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    (cdir / f"fpga-4-{SERIAL}.json").write_text(
        json.dumps({"owner": "old", "purpose": "old", "expires": past})
    )
    env = {**os.environ, "TTHFV_BOARD_CLAIMS": str(cdir), "PYTHONPATH": str(CHILD.parent)}

    def start(name):
        return subprocess.Popen(
            [sys.executable, str(CHILD), str(sync), name], stdout=subprocess.PIPE, text=True, env=env
        )

    a = start("a")
    try:
        assert _wait_for(sync / "a-paused", 10)  # A decided the claim is expired and is about to replace it
        b = start("b")
        try:
            _wait_for(sync / "b-paused", 2)  # without a lock B gets here too; with one it waits for A
            (sync / "go").touch()
            out_a, _ = a.communicate(timeout=20)
            out_b, _ = b.communicate(timeout=20)
        finally:
            b.kill()
    finally:
        a.kill()
    results = sorted([out_a.strip(), out_b.strip()])
    assert results == ["HELD", "TOOK"]
    holder = "a" if out_a.strip() == "TOOK" else "b"
    assert json.loads((cdir / f"fpga-4-{SERIAL}.json").read_text())["purpose"] == holder
