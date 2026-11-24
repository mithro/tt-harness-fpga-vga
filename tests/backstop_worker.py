# SPDX-License-Identifier: Apache-2.0
"""A worker for tests/test_nothing_outlives.py: backstop_worker.py <dir> <token> <marker>. It writes <marker> right
before it starts a tool through proc.run_logged in a real limited jail (the tool is two `sleep <token>` processes,
one of them after setsid), and is SIGKILLed from outside a few milliseconds later."""

import sys
from pathlib import Path

from tthfv import proc, sandbox

work, token, marker = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
rw, ro = work / "rw", work / "ro"
rw.mkdir(parents=True, exist_ok=True)
ro.mkdir(parents=True, exist_ok=True)
box = sandbox.for_stage(rw=rw, ro=[ro])
marker.write_text("go")
proc.run_logged(["sh", "-c", f"setsid sleep {token} & exec sleep {token}"], 400, rw / "x.log", cwd=rw, sandbox=box)
