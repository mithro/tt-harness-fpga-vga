# SPDX-License-Identifier: Apache-2.0
"""Parse yosys and nextpnr-ice40 logs into the figures recorded in build.json."""

import re
from dataclasses import dataclass, field
from typing import NamedTuple

_FMAX = re.compile(
    r"Max frequency for clock\s+'([^']+)':\s+([0-9.]+) MHz \((PASS|FAIL) at", re.IGNORECASE
)  # nextpnr pads the name column with spaces when it reports several clocks
_UTIL = re.compile(r"^\w+:\s+(ICESTORM_\w+|SB_\w+|IO_\w+):\s+(\d+)\s*/\s*(\d+)\s+\d+%", re.MULTILINE)
# nextpnr's capacity failures, as printed by the pinned version: "ERROR: Unable to place cell 'x', no BELs
# remaining to implement cell type 'ICESTORM_LC'". Pcf and constraint errors ("package does not have a pin
# named ...", "Loading PCF failed") are other failures and are not matched.
_NOFIT = re.compile(
    r"^ERROR:.*(no BELs remaining|more \S+(?: \S+){0,4} than (?:are )?available)", re.MULTILINE | re.IGNORECASE
)
_CELL = re.compile(r"^\s+(\d+)\s+(SB_\w+)\s*$", re.MULTILINE)
_TOP_CLK = re.compile(r"^clk(\$|$)")
CELL_KEYS = ("SB_LUT4", "SB_DFF", "SB_RAM40_4K", "SB_SPRAM256KA", "SB_MAC16")


class Clock(NamedTuple):
    fmax_hz: int
    passed: bool  # nextpnr's verdict against the constraint on the clock's final line


@dataclass(frozen=True)
class NextpnrReport:
    fmax_hz: int | None  # the main clock's Fmax; None if the log has no timing line
    utilisation: dict[str, tuple[int, int]]  # resource -> (used, available)
    fit: bool
    clocks: dict[str, Clock] = field(default_factory=dict)  # every clock's final Fmax and verdict

    @property
    def met(self) -> bool:
        """Timing is met only if nextpnr reported at least one clock and every clock passed."""
        return bool(self.clocks) and all(c.passed for c in self.clocks.values())

    @property
    def failing(self) -> list[str]:
        return [name for name, c in self.clocks.items() if not c.passed]


def _pick_fmax(clocks: dict[str, Clock]) -> int | None:
    """The main clock is the one named clk or clk$... (clk$SB_IO_IN_$glb_clk). If no clock has
    that name, take the slowest one, which is the conservative answer."""
    for name, c in clocks.items():
        if _TOP_CLK.match(name):
            return c.fmax_hz
    return min(c.fmax_hz for c in clocks.values()) if clocks else None


def parse_nextpnr(log_text: str) -> NextpnrReport:
    """nextpnr prints a Max frequency line per clock after placement and again after routing: the
    last line for each clock is the final figure and verdict. Utilisation is read from the last
    report block."""
    clocks: dict[str, Clock] = {}
    for name, mhz, verdict in _FMAX.findall(log_text):
        clocks[name] = Clock(round(float(mhz) * 1e6), verdict.upper() == "PASS")
    utilisation: dict[str, tuple[int, int]] = {}
    for name, used, total in _UTIL.findall(log_text):
        utilisation[name] = (int(used), int(total))
    over = any(used > total for used, total in utilisation.values())
    fit = not over and not _NOFIT.search(log_text)
    return NextpnrReport(_pick_fmax(clocks), utilisation, fit, clocks)


def parse_yosys(log_text: str) -> dict[str, int]:
    """Cell counts from the final statistics block. SB_DFF sums every SB_DFF* flip-flop variant."""
    marker = log_text.rfind("=== design hierarchy ===")
    block = log_text[marker:] if marker >= 0 else log_text
    counts = dict.fromkeys(CELL_KEYS, 0)
    for n, cell in _CELL.findall(block):
        key = "SB_DFF" if cell.startswith("SB_DFF") else cell
        if key in counts:
            counts[key] += int(n)
    return counts
