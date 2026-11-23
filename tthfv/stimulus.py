# SPDX-License-Identifier: Apache-2.0
"""Fixed input values for a design.

The rule: the override's fixed ui_in (and uio_in) if present, else the first line of its .inputs
script, else 0. Timed scripts and gamepad scripts are not replayed.
"""


def parse_first_line(script: str) -> tuple[int, int] | None:
    """Return (ui_in, uio_in) from the first data line of an .inputs script.

    Lines are "<clock> <ui_in> <uio_in>"; the numbers may be decimal or 0x hex. Blank lines and
    "#" comments are skipped.
    """
    for raw in script.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError(f"bad .inputs line (want '<clock> <ui_in> <uio_in>'): {raw!r}")
        return int(fields[1], 0), int(fields[2], 0)
    return None


def fixed_inputs(override: dict, inputs_script: str | None) -> tuple[int, int]:
    """Return (ui_in, uio_in) held constant for the design."""
    if "ui_in" in override or "uio_in" in override:
        return int(override.get("ui_in", 0)), int(override.get("uio_in", 0))
    if inputs_script is not None:
        first = parse_first_line(inputs_script)
        if first is not None:
            return first
    return 0, 0
