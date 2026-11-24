# SPDX-License-Identifier: Apache-2.0
"""Run one board session against the fake board named by the environment, until a signal ends it.
Used by test_board_signals.py as a subprocess: prints BODY once the body is running."""

import faulthandler
import signal
import sys
import time

faulthandler.register(signal.SIGUSR1, all_threads=True)  # the test asks for a stack dump if we hang

from tthfv.board import SessionTerminated, session, status
from tthfv.board.session import board_session

status.MIN_INTERVAL_S = 0.0
session.CONFIRM_AFTER_S = 0.0
FAST = {"poll_s": 0.05, "restore_timeout_s": 3.0, "confirm_timeout_s": 2.0, "attach_timeout_s": 2.0}

try:
    with board_session("fpga-4", "child", **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=True)
        print("BODY", flush=True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20:
            link.exec_raw("print(2)", 5, changes_state=False)
            time.sleep(0.05)
except SessionTerminated as e:
    print(f"TERMINATED {e.signum}", flush=True)
    sys.exit(128 + e.signum)
