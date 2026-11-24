# SPDX-License-Identifier: Apache-2.0
"""A standalone `tthfv reference` of a design whose simulation is a quiet `sleep`, run the way the command runs it
(sandbox.command around reference.simulate): standalone_driver.py <seconds>. TTHFV_WORK, TTHFV_SANDBOX=bwrap and the
real toolchain must be set; the limits are on (TTHFV_LIMITS unset)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_sandbox_stages import PORTS, make

from tthfv import reference, sandbox

SOURCE = """module tt_um_quiet %(ports)s
  initial $system("sleep %(seconds)s");
  reg [7:0] count;
  always @(posedge clk) count <= rst_n ? count + 8'd1 : 8'd0;
  assign uo_out = count;
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""


def go() -> int:
    work = Path(os.environ["TTHFV_WORK"])
    d, fetched = make(work.parent, "tt_um_quiet", SOURCE % {"ports": PORTS, "seconds": sys.argv[1]})
    sim = reference.simulate(d, fetched, clocks=100)
    print(sim.status, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(sandbox.command(go))
