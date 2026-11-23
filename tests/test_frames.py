# SPDX-License-Identifier: Apache-2.0
from tthfv import frames


def test_frame_hash_published_vectors():
    assert frames.frame_hash(b"") == 0xCBF29CE484222325
    assert frames.frame_hash(b"a") == 0xAF63DC4C8601EC8C
    assert frames.frame_hash(b"foobar") == 0x85944171F73967E8
