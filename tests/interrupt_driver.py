# SPDX-License-Identifier: Apache-2.0
"""Run `tthfv run build` with slow fake operations, for the Ctrl-C test: interrupt_driver.py <state dir>.

TTHFV_WORK must be set. Exits with the CLI's exit code."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ops import SlowOps

from tthfv import runner
from tthfv.cli import main

IDS = ["tt99/tt_um_slow1", "tt99/tt_um_slow2", "tt99/tt_um_late1", "tt99/tt_um_late2"]

if __name__ == "__main__":  # the forkserver imports this file as __main__ in every worker
    runner.REAL_OPS = SlowOps(Path(sys.argv[1]))
    runner.manifest.load = lambda **kw: []
    runner.select = lambda designs, only, candidates: IDS
    sys.exit(main(["run", "build", "--jobs", "2"]))
