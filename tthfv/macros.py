# SPDX-License-Identifier: Apache-2.0
"""Name the analog blocks and hard macros a design instantiates and the sources cannot supply.

Some Tiny Tapeout designs instantiate a block that exists only as layout or schematic (a DAC, a ring oscillator,
a ROM macro, an artwork nameplate): its function is not in any Verilog the design lists. Such a design cannot be
built or simulated here. When yosys or Verilator fails because a module is missing, the reason is restated as
`needs analog/hard macro <names>: <the tool's reason>`, naming every module that the design's compilation units
use (comments stripped), do not declare, and that the checkout has layout or schematic files for. This changes
only the text of a failure; nothing is ever substituted for such a block.
"""

import re
from pathlib import Path

from tthfv import fetch, safefile
from tthfv.fetch import FetchResult

LAYOUT_SUFFIXES = (".mag", ".gds", ".gds.gz", ".lef", ".sch", ".sym")
PREFIX = "needs analog/hard macro"
_MISSING = re.compile(
    r"Module `\\[\w$]+' referenced in module .* is not part of the design|Cannot find file containing module"
)
_WORD = re.compile(r"(?<![\w$])[A-Za-z_][\w$]*")


def layout_names(checkout: Path) -> set[str]:
    """Stems of the layout and schematic files anywhere in the checkout (.git skipped, nothing followed)."""
    try:
        entries = safefile.list_tree(checkout)
    except (OSError, safefile.NotPlainFile, safefile.TreeTooDeep):
        return set()
    names = set()
    for rel, kind in entries:
        base = rel.rsplit("/", 1)[-1]
        for suffix in LAYOUT_SUFFIXES:
            if kind == "file" and base.endswith(suffix) and len(base) > len(suffix):
                names.add(base[: -len(suffix)])
    return names


def missing(fetched: FetchResult) -> list[str]:
    """The modules the compilation units use, do not declare, and the checkout has layout or schematics for."""
    if fetched.checkout is None:
        return []
    used = {
        w
        for p in fetched.synth_sources
        for w in _WORD.findall(fetch.strip_comments(fetch.read_source_text(p)))
    }
    return sorted((used & layout_names(fetched.checkout)) - fetch.declared_modules(fetched.synth_sources))


def restate(reason: str, output: str, fetched: FetchResult) -> str:
    """reason, restated as PREFIX if the tool reported a missing module and the design uses analog or hard macros."""
    if reason.startswith(PREFIX) or not _MISSING.search(output):
        return reason
    names = missing(fetched)
    return f"{PREFIX} {', '.join(names)}: {reason}" if names else reason
