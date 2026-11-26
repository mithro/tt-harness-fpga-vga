# SPDX-License-Identifier: Apache-2.0
"""Behavioural models of PDK standard cells that some designs instantiate directly in their RTL.

The models are in tthfv/harness/cells/<pdk>.v, one `// @cell <name> <kind>` block per cell (kind: logic, delay,
latch). They are used only for a design that needs them, by one rule shared by the build and the reference:

    A tool run (yosys, Verilator) failed because a module whose name is a PDK cell name is missing. Then the
    stage runs again with the models of every PDK cell the design's compilation units reference (comments
    stripped) and do not declare themselves. The chosen models are written as one file (cells_text), read after
    the design's own sources; their names and the file's digest are part of that stage's key (key_fields).

A design that never fails that way never reads a model, so its scripts, command lines and keys are those of the
code before this module existed. Latch cells (and clock gates, which hold a latch) synthesise to LUT loops on the
iCE40; delay cells become wires. The build records both, and the publisher lists a design with latch cells with `pdk_latches: true`.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tthfv import fetch

CELLS_DIR = Path(__file__).resolve().parent / "harness" / "cells"
FILE_NAME = "pdk_cells.v"  # the file a stage writes the chosen models to
KINDS = ("logic", "delay", "latch")

# A PDK standard cell name: sky130 / gf180 libraries (<library>__<cell>), IHP SG13G2 and SG13CMOS5L (<lib>_<cell>).
_NAME = r"(?:sky130_fd_sc_[a-z]+|gf180mcu_fd_sc_[a-z0-9]+)__\w+|sg13g2_\w+|sg13cmos5l_\w+"
_PDK_NAME = re.compile(rf"^(?:{_NAME})$")
_REFERENCE = re.compile(rf"(?<![\w$])({_NAME})")
_BLOCK = re.compile(r"^// @cell (\S+) (\S+)\n(.*?^endmodule\n)", re.MULTILINE | re.DOTALL)
# What yosys and Verilator say about a module that no source declares. Verilator spells `__` as `___05F`.
_YOSYS_MISSING = re.compile(r"Module `\\([\w$]+)' referenced in module .* is not part of the design")
_VERILATOR_MISSING = re.compile(r"Cannot find file containing module: '([\w$]+)'")


class LibraryError(Exception):
    """A model file breaks the format; a bug in tthfv, not a design's fault."""


@dataclass(frozen=True)
class Cell:
    name: str
    kind: str  # logic, delay or latch
    text: str  # the `// @cell` line through `endmodule`


@cache
def library() -> dict[str, Cell]:
    """Every modelled cell, by name, from every file in CELLS_DIR."""
    cells: dict[str, Cell] = {}
    for path in sorted(CELLS_DIR.glob("*.v")):
        text = path.read_text()
        here = []
        for m in _BLOCK.finditer(text):
            name, kind, body = m.group(1), m.group(2), m.group(3)
            if kind not in KINDS or not _PDK_NAME.match(name) or name in cells:
                raise LibraryError(f"{path.name}: bad or repeated cell {name!r} of kind {kind!r}")
            if fetch.declared_modules_in(body) != {name}:
                raise LibraryError(f"{path.name}: the block of {name} must declare exactly module {name}")
            cells[name] = Cell(name, kind, m.group(0))
            here.append(name)
        if fetch.declared_modules_in(text) != set(here):
            raise LibraryError(f"{path.name}: a module outside a `// @cell` block")
    return cells


def is_pdk_name(name: str) -> bool:
    return bool(_PDK_NAME.match(name))


def missing_cell(output: str) -> str:
    """The first PDK cell name that yosys or Verilator reported as a missing module in output, or ''."""
    for m in [*_YOSYS_MISSING.finditer(output), *_VERILATOR_MISSING.finditer(output)]:
        name = m.group(1).replace("__05F", "_")
        if is_pdk_name(name):
            return name
    return ""


def referenced(units: Iterable[Path]) -> list[str]:
    """The PDK cell names the units mention outside comments and do not declare as a module, sorted."""
    texts = [fetch.strip_comments(fetch.read_source_text(p)) for p in units]
    names = {n for t in texts for n in _REFERENCE.findall(t)}
    return sorted(names - fetch.declared_modules(units))


@dataclass(frozen=True)
class Plan:
    """The models one design gets: the referenced cells that have one (cells) and those that do not."""

    cells: tuple[str, ...]
    unmodelled: tuple[str, ...]

    @property
    def text(self) -> str:
        return cells_text(self.cells)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    def of_kind(self, kind: str) -> list[str]:
        lib = library()
        return [c for c in self.cells if lib[c].kind == kind]

    @property
    def latches(self) -> bool:
        return bool(self.of_kind("latch"))

    def key_fields(self) -> dict:
        """What goes into the stage's key: the cells and the digest of the model file."""
        return {"pdk_cells": list(self.cells), "pdk_cells_sha256": self.sha256}

    def note(self) -> str:
        """The warning for build.json: what the FPGA cannot reproduce."""
        parts = []
        if latches := self.of_kind("latch"):
            parts.append(f"latch cells ({', '.join(latches)}) are LUT loops on the iCE40")
        if delays := self.of_kind("delay"):
            parts.append(f"delay cells ({', '.join(delays)}) are wires on the iCE40 and in the simulation")
        if not parts:
            return ""
        return (
            "PDK cells modelled behaviourally: "
            + "; ".join(parts)
            + ": the bitstream may not behave like the simulation"
        )


def plan_for(units: Iterable[Path]) -> Plan:
    lib = library()
    names = referenced(units)
    return Plan(tuple(n for n in names if n in lib), tuple(n for n in names if n not in lib))


def plan_named(cells: Iterable[str]) -> Plan | None:
    """The plan a record names (its pdk_cells), or None if a name is not modelled (any more)."""
    lib = library()
    cells = tuple(cells)
    if not all(isinstance(c, str) and c in lib for c in cells) or list(cells) != sorted(set(cells)):
        return None
    return Plan(cells, ())


def cells_text(cells: Iterable[str]) -> str:
    lib = library()
    body = "\n".join(lib[c].text for c in cells)
    return (
        "// SPDX-License-Identifier: Apache-2.0\n"
        "// PDK cell models chosen by tthfv for this design; see tthfv/harness/cells/ for their sources.\n\n"
        + body
    )


LOOP_SET = "tthfv_pdk_loops"
DELAY_AS_FUNCTION = "uses PDK delay as function"


def loop_check(top: str, cells_file: Path) -> list[str]:
    """yosys commands that fail, naming the selection LOOP_SET, if a combinational loop runs through a cell of
    cells_file (a ring oscillator or a delay line: the cell's analog delay is the design's function). They work on
    a copy of the design (design -push-copy ... -pop), so what follows them is not affected."""
    return [
        "design -push-copy",
        f"hierarchy -top {top}",
        "proc",
        "setattr -mod -unset keep_hierarchy",
        "setattr -unset keep_hierarchy",
        "flatten",
        "scc -select",
        f"select -set {LOOP_SET} %",
        f"select -assert-none @{LOOP_SET} a:src=*{cells_file.name}* %i",
        "design -pop",
    ]


def loop_reason(plan: Plan) -> str:
    return f"{DELAY_AS_FUNCTION}: a combinational loop runs through the PDK cell models ({', '.join(plan.cells)})"


def retry_reason(output: str, plan: Plan) -> str:
    """Why a stage that failed with output cannot be retried with PDK models, or '' if it can."""
    missing = missing_cell(output)
    if not missing:
        return "no missing PDK cell"
    if missing not in plan.cells:
        return f"needs PDK cell model {missing}"
    return ""
