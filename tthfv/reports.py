# SPDX-License-Identifier: Apache-2.0
"""Parse yosys and nextpnr-ice40 logs into the figures recorded in build.json."""

import re
from dataclasses import dataclass, field

_FMAX = re.compile(r"Max frequency for clock '([^']+)': ([0-9.]+) MHz")
_UTIL = re.compile(r"^\w+:\s+(ICESTORM_\w+|SB_\w+|IO_\w+):\s+(\d+)\s*/\s*(\d+)\s+\d+%", re.MULTILINE)
_NOFIT = re.compile(r"^ERROR:.*(Unable to|exceed|cannot be|Insufficient|capacity)", re.MULTILINE | re.IGNORECASE)
_CELL = re.compile(r"^\s+(\d+)\s+(SB_\w+)\s*$", re.MULTILINE)
_TOP_CLK = re.compile(r"^clk(\$|$)")
CELL_KEYS = ("SB_LUT4", "SB_DFF", "SB_RAM40_4K", "SB_SPRAM256KA", "SB_MAC16")


@dataclass(frozen=True)
class NextpnrReport:
    fmax_hz: int | None  # the top-level clk net's Fmax; None if the log has no timing line
    utilisation: dict[str, tuple[int, int]]  # resource -> (used, available)
    fit: bool
    clocks: dict[str, int] = field(default_factory=dict)  # every clock's final Fmax, in Hz


def _pick_fmax(clocks: dict[str, int]) -> int | None:
    """The top-level clk net is named clk$SB_IO_IN_$glb_clk (or clk). If no clock matches that
    name, take the slowest one, which is the conservative answer."""
    for name, hz in clocks.items():
        if _TOP_CLK.match(name):
            return hz
    return min(clocks.values()) if clocks else None


def parse_nextpnr(log_text: str) -> NextpnrReport:
    """nextpnr prints a Max frequency line after placement and again after routing: the last line
    for each clock is the final figure. Utilisation is read from the last report block."""
    clocks: dict[str, int] = {}
    for name, mhz in _FMAX.findall(log_text):
        clocks[name] = round(float(mhz) * 1e6)
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
