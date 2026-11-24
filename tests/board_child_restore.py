# SPDX-License-Identifier: Apache-2.0
"""A read-only session whose body returns at once, so the run is mostly the restore check; used by
test_board_round4.py, which signals it at many offsets. Prints BODY when the body runs, then either
SESSION-END (the session returned normally) or SIGNALLED <signum> <outcome> (the session raised a signal)."""

import faulthandler
import signal
import sys
import time

faulthandler.register(signal.SIGUSR1, all_threads=True)

from tthfv.board import SessionTerminated, session, status
from tthfv.board.session import SessionReport, board_session

status.MIN_INTERVAL_S = 0.1
session.CONFIRM_AFTER_S = 0.0
session.RESTORE_VISITOR_AFTER_S = 0.5
FAST = {"poll_s": 0.1, "restore_timeout_s": 10.0, "confirm_timeout_s": 2.0, "attach_timeout_s": 2.0}

report = SessionReport()
try:
    with board_session("fpga-4", "child", report=report, **FAST) as link:
        link.exec_raw("print(1)", 5, changes_state=False)
        print("BODY", flush=True)
except KeyboardInterrupt:
    print(f"SIGNALLED {int(signal.SIGINT)} {report.outcome}", flush=True)
    sys.exit(128 + signal.SIGINT)
except SessionTerminated as e:
    print(f"SIGNALLED {e.signum} {report.outcome}", flush=True)
    sys.exit(128 + e.signum)
print(f"SESSION-END {report.outcome}", flush=True)
time.sleep(3)  # a signal arriving now is outside the session: the default action applies (not exit 0)
