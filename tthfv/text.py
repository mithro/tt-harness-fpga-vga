# SPDX-License-Identifier: Apache-2.0
"""Cleaning of text that comes from third parties before it reaches a terminal or a document."""

import re

_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # everything below 0x20 except tab, and DEL


def strip_control(text: str) -> str:
    """Remove control characters (terminal escape sequences start with one). Tab stays."""
    return _CONTROL.sub("", text)
