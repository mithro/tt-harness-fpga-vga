# SPDX-License-Identifier: Apache-2.0
"""Fake stage operations for the runner tests.

This is a module of its own, not part of a test file, so that a worker process started with the
forkserver method can import it. Each call is appended to <state>/calls.log; a finished stage leaves
<state>/<stage>-<shuttle>-<macro>.done and is not run again without force, like the real stages.

Design ids steer the behaviour: "boom" in the id raises in build, "fetchfail" fails the fetch,
"nosync" gives reference status no-sync.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tthfv.runner import Outcome


class FakeOps:
    def __init__(self, state: Path):
        self.state = Path(state)

    def _go(self, stage: str, design_id: str, force: bool, status: str, reason: str = "") -> Outcome:
        marker = self.state / f"{stage}-{design_id.replace('/', '-')}.done"
        if marker.exists() and not force:
            return Outcome(status, reason)
        with (self.state / "calls.log").open("a") as f:
            f.write(f"{stage} {design_id} force={force} pid={os.getpid()}\n")
        marker.write_text("")
        return Outcome(status, reason)

    def fetch(self, design_id: str, force: bool) -> Outcome:
        if "fetchfail" in design_id:
            return self._go("fetch", design_id, force, "fetch-fail", "no network")
        return self._go("fetch", design_id, force, "ok")

    def build(self, design_id: str, force: bool) -> Outcome:
        if "boom" in design_id:
            raise RuntimeError("synthetic failure")
        return self._go("build", design_id, force, "ok")

    def reference(self, design_id: str, force: bool, clocks: int | None) -> Outcome:
        return self._go("reference", design_id, force, "no-sync" if "nosync" in design_id else "ok")


class KillOps(FakeOps):
    """A design with "killme" in its id kills its own worker process during build, as the OOM killer would."""

    def build(self, design_id: str, force: bool) -> Outcome:
        if "killme" in design_id:
            os.kill(os.getpid(), signal.SIGKILL)
        return super().build(design_id, force)


class SlowOps(FakeOps):
    """A design with "slow" in its id spawns a grandchild in its own session and sleeps. If either is
    still alive after SECONDS it writes a marker file, which the interrupt test checks is absent."""

    SECONDS = 5

    def build(self, design_id: str, force: bool) -> Outcome:
        if "slow" in design_id:
            marker = self.state / "grandchild"
            code = f"import time, pathlib; time.sleep({self.SECONDS}); pathlib.Path({str(marker)!r}).write_text('x')"
            subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
            time.sleep(self.SECONDS)
            (self.state / "worker-survived").write_text("x")
        return super().build(design_id, force)
