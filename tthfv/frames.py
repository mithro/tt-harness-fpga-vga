# SPDX-License-Identifier: Apache-2.0
"""Frames and hashes: cut a per-clock uo_out dump into VGA frames. See docs/frame-definition.md."""

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK = (1 << 64) - 1


def frame_hash(rgb888: bytes) -> int:
    """FNV-1a 64-bit hash of the bytes."""
    h = _FNV_OFFSET
    for b in rgb888:
        h = ((h ^ b) * _FNV_PRIME) & _MASK
    return h
