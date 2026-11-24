# SPDX-License-Identifier: Apache-2.0
"""Take a claim in a separate process, pausing at the point where an expired claim is about to be replaced
until the parent says go. Used by test_board_claims_race.py. Args: <sync dir> <name>."""

import sys
import time
from pathlib import Path

from tthfv.board import ClaimHeld, claims

sync, name = Path(sys.argv[1]), sys.argv[2]


def pause() -> None:
    (sync / f"{name}-paused").touch()
    deadline = time.monotonic() + 10
    while not (sync / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.01)


claims._pause = pause
try:
    claims.take("fpga-4", "a2961e5cac65b25f", name, 10)
except ClaimHeld:
    print("HELD", flush=True)
else:
    print("TOOK", flush=True)
