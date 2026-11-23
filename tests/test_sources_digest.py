# SPDX-License-Identifier: Apache-2.0
"""The source digest and the host's reads of source files never follow a symlink out of the checkout."""

import hashlib
import os
import time

from tthfv import fetch


def tree(tmp_path):
    src = tmp_path / "checkout" / "src"
    src.mkdir(parents=True)
    (src / "a.v").write_text("module a; endmodule\n")
    return src


def test_a_regular_tree_has_the_digest_it_always_had(tmp_path):
    src = tree(tmp_path)
    data = b"module a; endmodule\n"
    expected = hashlib.sha256(b"tree:a.v\0%d\0" % len(data) + data).hexdigest()
    assert fetch.sources_sha256(src) == expected


def test_the_digest_does_not_depend_on_what_a_symlink_points_at(tmp_path):
    src = tree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("one")
    (src / "link").symlink_to(outside)
    first = fetch.sources_sha256(src)
    outside.write_text("two, and longer")  # a confirmation oracle if it mattered
    assert fetch.sources_sha256(src) == first
    (src / "link").unlink()
    (src / "link").symlink_to(tmp_path / "somewhere-else")
    assert fetch.sources_sha256(src) != first  # the link text is part of the digest


def test_a_symlinked_directory_is_not_entered(tmp_path):
    src = tree(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "f").write_text("x")
    (src / "dirlink").symlink_to(other)
    first = fetch.sources_sha256(src)
    (other / "f").write_text("changed")
    (other / "new").write_text("new")
    assert fetch.sources_sha256(src) == first


def test_a_symlink_to_a_missing_file_or_a_loop_is_fine(tmp_path):
    src = tree(tmp_path)
    (src / "dangling").symlink_to(tmp_path / "nowhere")
    (src / "loop").symlink_to("loop")
    assert fetch.sources_sha256(src)


def test_a_fifo_in_the_tree_is_not_read_and_does_not_block(tmp_path):
    src = tree(tmp_path)
    os.mkfifo(src / "pipe")
    started = time.monotonic()
    fetch.sources_sha256(src)
    assert time.monotonic() - started < 5


def test_big_files_are_hashed_in_chunks_with_the_same_result(tmp_path, monkeypatch):
    src = tree(tmp_path)
    (src / "big.bin").write_bytes(bytes(range(256)) * 5000)
    whole = fetch.sources_sha256(src)
    monkeypatch.setattr(fetch, "_HASH_CHUNK", 4096)
    assert fetch.sources_sha256(src) == whole


def test_module_names_are_not_read_through_a_symlink_and_reads_are_capped(tmp_path, monkeypatch):
    src = tree(tmp_path)
    outside = tmp_path / "outside.v"
    outside.write_text("module tt_um_secret; endmodule\n")
    (src / "l.v").symlink_to(outside)
    assert fetch.declared_modules([src / "l.v"]) == set()
    assert fetch.declared_modules([src / "a.v"]) == {"a"}
    monkeypatch.setattr(fetch, "MAX_SOURCE_READ", 4)
    assert fetch.declared_modules([src / "a.v"]) == set()  # cut inside the word "module"
