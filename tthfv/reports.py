# SPDX-License-Identifier: Apache-2.0
"""The figures recorded in build.json, taken from the tools' machine-readable output and never from their
log text, which a design can write into (a `(* src = "...\\nInfo: ..." *)` attribute or an elaboration-time
$display puts whole lines of its choice in a tool's log).

- nextpnr-ice40 writes `--report report.json`: per clock `fmax.<name>.achieved` and `.constraint` in MHz and
  a `utilization` object. parse_report() reads it and decides pass or fail itself (achieved >= constraint);
  JSON keys are data, so a clock name with quotes or newlines is just a string. Anything missing, malformed,
  not a finite positive number, or not matching the clock the design was built for is a ReportError: the
  attempt is a failure, never an ok.
- Paths between two different clock domains are in `critical_paths`, not in `fmax`: each entry has `from` and
  `to` ("posedge NAME", "negedge NAME" or "<async>") and a list of segments with a `delay` in ns; the sum of
  all the delays is what nextpnr prints as "Max delay A -> B" (checked on two real designs, to the 10 ps).
  A path between two clock edges of different clocks that takes longer than one period of the design's clock
  is a timing failure, and so is a path between the opposite edges of one clock that takes longer than half
  of it. This is a conservative rule of thumb, not a proof: nextpnr reports only the worst path per pair of
  domains, two unrelated clocks have no defined phase relation, and pin-to-register and register-to-pin
  (<async>) paths are not judged.
- yosys writes `stat -json` to a file: cell counts by type (parse_stat).
- The nextpnr log is consulted only by capacity_failure(), after nextpnr has exited non-zero, to tell a design
  that does not fit from any other failure. A forged line there can change which failure is named, never
  turn a failure into an ok or an ok into a failure.
"""

import json
import math
import re
from dataclasses import dataclass, field
from typing import NamedTuple

from tthfv.text import strip_control

MAX_CLOCKS = 64  # more clocks than this in a report is a failure
MAX_CROSS = 64  # more judged clock-to-clock paths than this in a report is a failure
MAX_NAME = 200  # characters of a clock or resource name that are recorded
CONSTRAINT_TOLERANCE = 0.005  # the report's constraint may differ from the design's clock by this fraction
LC_KEY = "ICESTORM_LC"
CELL_KEYS = ("SB_LUT4", "SB_DFF", "SB_RAM40_4K", "SB_SPRAM256KA", "SB_MAC16")
# Cell types that hold state and so are timed by a clock: a design with any of them must have a clock in the report.
CLOCKED_KEYS = ("SB_DFF", "SB_RAM40_4K", "SB_SPRAM256KA", "SB_MAC16")
_TOP_CLK = re.compile(r"^clk(\$|$)")

# nextpnr's capacity failures, as printed by the pinned version: "ERROR: Unable to place cell 'x', no BELs
# remaining to implement cell type 'ICESTORM_LC'". Pcf and constraint errors ("package does not have a pin
# named ...", "Loading PCF failed") are other failures and are not matched.
_NOFIT = re.compile(
    r"^ERROR:.*(no BELs remaining|more \S+(?: \S+){0,4} than (?:are )?available)",
    re.MULTILINE | re.IGNORECASE,
)
_UTIL_LOG = re.compile(r"^\w+:\s+(ICESTORM_\w+|SB_\w+|IO_\w+):\s+(\d+)\s*/\s*(\d+)\s+\d+%", re.MULTILINE)


class ReportError(Exception):
    """The tool's report cannot be trusted; str() says what was wrong. The attempt fails."""


class Clock(NamedTuple):
    fmax_hz: int
    constraint_hz: int
    passed: bool  # achieved >= constraint, decided here


class CrossPath(NamedTuple):
    """The worst path between two clock edges that fmax does not judge: two different clocks, or the two edges
    of one clock."""

    from_edge: str  # "posedge NAME", cleaned and capped like a clock name
    to_edge: str
    delay_ns: float
    limit_ns: float
    passed: bool  # delay_ns <= limit_ns

    def record(self) -> dict:
        return {
            "from": self.from_edge, "to": self.to_edge, "delay_ns": round(self.delay_ns, 3),
            "limit_ns": round(self.limit_ns, 3), "pass": self.passed,
        }  # fmt: skip


@dataclass(frozen=True)
class NextpnrReport:
    fmax_hz: int | None  # the main clock's Fmax; None if the design has no clock
    utilisation: dict[str, tuple[int, int]]  # resource -> (used, available)
    clocks: dict[str, Clock] = field(default_factory=dict)
    cross_paths: list[CrossPath] = field(default_factory=list)
    note: str = ""  # something worth saying about how the figures were judged

    @property
    def met(self) -> bool:
        """Every clock in the report meets its constraint and so does every judged clock-to-clock path (a design
        with no clock has nothing to miss)."""
        return all(c.passed for c in self.clocks.values()) and all(c.passed for c in self.cross_paths)

    @property
    def failing_cross(self) -> list[CrossPath]:
        return [c for c in self.cross_paths if not c.passed]

    @property
    def failing(self) -> list[str]:
        return [name for name, c in self.clocks.items() if not c.passed]


def _pick_fmax(clocks: dict[str, Clock]) -> int | None:
    """The main clock is the one named clk or clk$... (clk$SB_IO_IN_$glb_clk). If no clock has that name,
    take the slowest one, which is the conservative answer."""
    for name, c in clocks.items():
        if _TOP_CLK.match(name):
            return c.fmax_hz
    return min(c.fmax_hz for c in clocks.values()) if clocks else None


def _reject_constant(name: str):
    raise ValueError(f"{name} is not a number")


def _load(data: bytes, what: str):
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        raise ReportError(f"{what} is not valid JSON ({type(e).__name__}: {str(e)[:100]})") from e


def _number(value, what: str) -> float:
    """A finite positive number (not a bool, not a string)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReportError(f"{what} is not a number: {str(value)[:40]!r}")
    if not math.isfinite(value) or value <= 0:
        raise ReportError(f"{what} is not a finite positive number: {value!r}")
    return float(value)


def _count(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportError(f"{what} is not a count: {str(value)[:40]!r}")
    return value


def _edge(endpoint) -> tuple[str, str] | None:
    """("posedge" | "negedge", clock name) of a critical path's from or to, None for "<async>" (a pin)."""
    if not isinstance(endpoint, str):
        raise ReportError("a critical path's endpoint is not a string")
    for kind in ("posedge ", "negedge "):
        if endpoint.startswith(kind):
            return kind.strip(), endpoint[len(kind) :]
    return None


def _cross_paths(doc: dict, clock_hz: int) -> list[CrossPath]:
    """The judged clock-to-clock paths of the report's critical_paths; see the module docstring."""
    paths = doc.get("critical_paths")
    if not isinstance(paths, list):
        raise ReportError("the nextpnr report has no 'critical_paths' list")
    period_ns = 1e9 / clock_hz
    found: list[CrossPath] = []
    for cp in paths:
        if not isinstance(cp, dict) or not isinstance(cp.get("path"), list):
            raise ReportError("a critical path in the nextpnr report is malformed")
        src, dst = _edge(cp.get("from")), _edge(cp.get("to"))
        if src is None or dst is None:
            continue  # a pin on one side: not judged
        if src == dst:
            continue  # the same edge of the same clock: judged by fmax
        delay = 0.0
        for seg in cp["path"]:
            if not isinstance(seg, dict) or isinstance(seg.get("delay"), bool):
                raise ReportError("a critical path segment in the nextpnr report is malformed")
            d = seg.get("delay")
            if not isinstance(d, int | float) or not math.isfinite(d) or d < 0:
                raise ReportError(f"a critical path segment has a delay that is not a finite number: {d!r}")
            delay += d
        # one clock's two edges share a half-period; two different clocks have no known relation, so one period
        limit = period_ns / 2 if src[1] == dst[1] else period_ns
        found.append(CrossPath(clean_name(cp["from"]), clean_name(cp["to"]), delay, limit, delay <= limit))
        if len(found) > MAX_CROSS:
            raise ReportError(f"the nextpnr report has more than {MAX_CROSS} clock-to-clock paths")
    return found


def clean_name(name: str) -> str:
    """A design-controlled name as recorded: control characters removed, at most MAX_NAME characters."""
    return strip_control(name)[:MAX_NAME]


def parse_report(data: bytes, *, clock_hz: int, has_registers: bool) -> NextpnrReport:
    """Read nextpnr's --report JSON. clock_hz is the clock the design was built for: every clock's constraint
    must be within CONSTRAINT_TOLERANCE of it. has_registers: the design has clocked cells, so a report
    without a single clock is not believable. Raises ReportError."""
    doc = _load(data, "the nextpnr report")
    if not isinstance(doc, dict) or not isinstance(doc.get("fmax"), dict):
        raise ReportError("the nextpnr report has no 'fmax' object")
    if not isinstance(doc.get("utilization"), dict):
        raise ReportError("the nextpnr report has no 'utilization' object")
    if len(doc["fmax"]) > MAX_CLOCKS:
        raise ReportError(
            f"the nextpnr report lists {len(doc['fmax'])} clocks; at most {MAX_CLOCKS} are accepted"
        )
    clocks: dict[str, Clock] = {}
    want_mhz = clock_hz / 1e6
    for raw, entry in doc["fmax"].items():
        if not isinstance(entry, dict):
            raise ReportError("a clock's entry in the nextpnr report is not an object")
        name = clean_name(raw)
        base, n = name, 1
        while name in clocks:  # two names that differ only in what was removed or cut stay two clocks
            n += 1
            name = f"{base[: MAX_NAME - 6]}~{n}"
        achieved = _number(entry.get("achieved"), f"fmax of clock {name!r}")
        constraint = _number(entry.get("constraint"), f"constraint of clock {name!r}")
        if abs(constraint - want_mhz) > CONSTRAINT_TOLERANCE * want_mhz:
            raise ReportError(
                f"clock {name!r} was constrained to {constraint:.3f} MHz but the design is built for {want_mhz:.3f} MHz"
            )
        clocks[name] = Clock(round(achieved * 1e6), round(constraint * 1e6), achieved >= constraint)
    cross = _cross_paths(doc, clock_hz)
    note = ""
    if not clocks and has_registers:
        # Registers that only touch pins (I/O registers) have no clock-to-clock path, and nextpnr then lists no
        # clock. If it listed any path between two clock edges, a missing clock would be wrong.
        if cross or any(
            _edge(cp.get("from")) and _edge(cp.get("to"))
            for cp in doc["critical_paths"]
            if isinstance(cp, dict)
        ):
            raise ReportError("the nextpnr report lists no clock, but the design has clock-to-clock paths")
        note = "no clock-to-clock path: the registers only touch pins, so no clock frequency was judged"
    utilisation: dict[str, tuple[int, int]] = {}
    for raw, entry in doc["utilization"].items():
        if not isinstance(entry, dict):
            raise ReportError("a utilisation entry in the nextpnr report is not an object")
        utilisation[clean_name(raw)] = (
            _count(entry.get("used"), f"used {raw[:40]!r}"),
            _count(entry.get("available"), f"available {raw[:40]!r}"),
        )
    if LC_KEY not in utilisation or utilisation[LC_KEY][1] == 0:
        raise ReportError(f"the nextpnr report has no {LC_KEY} utilisation")
    if any(used > total for used, total in utilisation.values()):
        raise ReportError("the nextpnr report shows a resource used beyond its capacity")
    return NextpnrReport(_pick_fmax(clocks), utilisation, clocks, cross, note)


def capacity_failure(log_text: str) -> bool:
    """For a nextpnr run that exited non-zero: True if its log says the design does not fit the device. A
    heuristic on text the design can influence; the only consequence is the name of a failure."""
    over = any(int(used) > int(total) for _n, used, total in _UTIL_LOG.findall(log_text))
    return over or bool(_NOFIT.search(log_text))


def log_lc_percent(log_text: str) -> float | None:
    """The logic-cell utilisation printed in the log of a run that did not fit, for display only."""
    found = [(int(u), int(t)) for n, u, t in _UTIL_LOG.findall(log_text) if n == LC_KEY]
    return round(100 * found[-1][0] / found[-1][1], 1) if found and found[-1][1] else None


def parse_stat(data: bytes) -> dict[str, int]:
    """Cell counts from yosys `stat -json`: the design's totals by type. SB_DFF sums every SB_DFF* variant.
    Raises ReportError."""
    doc = _load(data, "the yosys statistics")
    if not isinstance(doc, dict):
        raise ReportError("the yosys statistics are not a JSON object")
    section = doc.get("design")
    if not isinstance(section, dict) or not isinstance(section.get("num_cells_by_type"), dict):
        modules = doc.get("modules")
        if not isinstance(modules, dict) or len(modules) != 1:
            raise ReportError("the yosys statistics have no design totals")
        section = next(iter(modules.values()))
        if not isinstance(section, dict):
            raise ReportError("the yosys statistics have no design totals")
    by_type = section.get("num_cells_by_type")
    if not isinstance(by_type, dict):
        raise ReportError("the yosys statistics have no cell counts")
    counts = dict.fromkeys(CELL_KEYS, 0)
    for cell, n in by_type.items():
        key = "SB_DFF" if cell.startswith("SB_DFF") else cell
        if key in counts:
            counts[key] += _count(n, f"count of {cell[:40]!r}")
    return counts


def has_registers(cells: dict[str, int]) -> bool:
    return any(cells.get(k, 0) > 0 for k in CLOCKED_KEYS)
