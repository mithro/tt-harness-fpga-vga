# SPDX-License-Identifier: Apache-2.0
"""Run `tthfv run build` over designs whose build is a long `sleep` inside a real sandbox and cgroup scope, for
the test that kills this process with SIGKILL: orphan_driver.py <state dir>. TTHFV_WORK must be set; the
environment turns the sandbox and the limits on. Each worker writes its pid to <state>/worker-<pid>."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ops import FakeOps

from tthfv import proc, runner, sandbox
from tthfv.cli import main
from tthfv.runner import Outcome

SECONDS = sys.argv[2] if len(sys.argv) > 2 else "300"  # how long each design's tool sleeps
IDS = ["tt99/tt_um_sleep1", "tt99/tt_um_sleep2", "tt99/tt_um_sleep3", "tt99/tt_um_sleep4"]


class SleepOps(FakeOps):
    """Build = `sleep 300` in a limited jail, as a tool of a real build would run."""

    def build(self, design_id: str, force: bool) -> Outcome:
        (self.state / f"worker-{os.getpid()}").write_text(design_id)
        work = self.state / "jail" / design_id.replace("/", "-")
        (work / "rw").mkdir(parents=True, exist_ok=True)
        (work / "ro").mkdir(parents=True, exist_ok=True)
        box = sandbox.for_stage(rw=work / "rw", ro=[work / "ro"])
        proc.run_logged(["sleep", SECONDS], 400, work / "rw" / "x.log", cwd=work / "rw", sandbox=box)
        return Outcome("ok")


if __name__ == "__main__":  # the forkserver imports this file as __main__ in every worker
    runner.REAL_OPS = SleepOps(Path(sys.argv[1]))
    runner.manifest.load = lambda **kw: []
    runner.select = lambda designs, only, candidates: IDS
    sys.exit(main(["run", "build", "--jobs", "2"]))
