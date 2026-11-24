# SPDX-License-Identifier: Apache-2.0
"""Make one tool scope in a limited sandbox and exit; prints this process's pid: slice_driver.py <work dir>."""

import os
import sys
from pathlib import Path

from tthfv import proc, sandbox

work = Path(sys.argv[1])
(work / "rw").mkdir(parents=True, exist_ok=True)
(work / "ro").mkdir(parents=True, exist_ok=True)
box = sandbox.for_stage(rw=work / "rw", ro=[work / "ro"])
done = proc.run_logged(["true"], 60, work / "rw" / "x.log", cwd=work / "rw", sandbox=box)
print(os.getpid(), done.rc, flush=True)
