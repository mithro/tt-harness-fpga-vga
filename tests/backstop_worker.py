# SPDX-License-Identifier: Apache-2.0
"""A worker for tests/test_nothing_outlives.py: backstop_worker.py <dir> <token> <marker> [no-cgroup-kill]. It writes <marker> right
before it starts a tool through proc.run_logged in a real limited jail (the tool is two `sleep <token>` processes,
one of them after setsid), and is SIGKILLed from outside a few milliseconds later."""

import sys
from pathlib import Path

from tthfv import proc, sandbox

work, token, marker = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
if sys.argv[4:] == ["no-cgroup-kill"]:  # test of the guard's fallback: its cgroup.kill path does not exist
    from tthfv import limits

    # The tool is the payload itself, not a jail: bubblewrap's --die-with-parent then cannot end it when the wrapper
    # dies, so only the guard can, and only through its fallback.
    _scope_argv = limits.scope_argv
    _payload = ["sh", "-c", f"setsid sleep {token} & exec sleep {token}"]
    limits.scope_argv = lambda argv, *a, **k: _scope_argv(
        _payload, *a, guard_cgroup="/nonexistent/cgroup", **k
    )
rw, ro = work / "rw", work / "ro"
rw.mkdir(parents=True, exist_ok=True)
ro.mkdir(parents=True, exist_ok=True)
box = sandbox.for_stage(rw=rw, ro=[ro])
marker.write_text("go")
proc.run_logged(
    ["sh", "-c", f"setsid sleep {token} & exec sleep {token}"], 400, rw / "x.log", cwd=rw, sandbox=box
)
