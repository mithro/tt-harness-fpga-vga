# SPDX-License-Identifier: Apache-2.0
"""tthfv.safefile: what the host will and will not take from a directory third-party code could write."""

import os
import time

import pytest

from tthfv import safefile


@pytest.fixture
def box(tmp_path):
    d = tmp_path / "scratch"
    d.mkdir()
    fd = safefile.open_dir(d)
    yield d, fd
    os.close(fd)


def test_a_plain_file_of_the_right_size_is_read(box):
    d, fd = box
    (d / "f").write_bytes(b"abcd")
    assert safefile.read_plain(fd, "f", max_size=10, exact=4) == b"abcd"


def test_a_symlink_is_refused_even_to_a_file_of_the_right_size(box, tmp_path):
    d, fd = box
    (tmp_path / "target").write_bytes(b"abcd")
    (d / "f").symlink_to(tmp_path / "target")
    with pytest.raises(safefile.NotPlainFile, match="not a plain file.*symbolic link"):
        safefile.read_plain(fd, "f", max_size=10, exact=4)


def test_a_hard_link_is_refused(box):
    d, fd = box
    (d / "a").write_bytes(b"abcd")
    os.link(d / "a", d / "b")
    with pytest.raises(safefile.NotPlainFile, match="2 hard links"):
        safefile.read_plain(fd, "b", max_size=10, exact=4)


def test_a_fifo_is_refused_and_does_not_block(box):
    d, fd = box
    os.mkfifo(d / "f")
    started = time.monotonic()
    with pytest.raises(safefile.NotPlainFile, match="FIFO"):
        safefile.read_plain(fd, "f", max_size=10)
    assert time.monotonic() - started < 5


def test_a_directory_is_refused(box):
    d, fd = box
    (d / "f").mkdir()
    with pytest.raises(safefile.NotPlainFile, match="directory"):
        safefile.read_plain(fd, "f", max_size=10)


@pytest.mark.parametrize("size", [3, 5, 40])
def test_a_wrong_or_too_large_size_is_refused(box, size):
    d, fd = box
    (d / "f").write_bytes(b"x" * size)
    with pytest.raises(safefile.NotPlainFile, match="size|large"):
        safefile.read_plain(fd, "f", max_size=10, exact=4)


def test_a_missing_file_is_refused_with_a_reason(box):
    _d, fd = box
    with pytest.raises(safefile.NotPlainFile, match="missing"):
        safefile.read_plain(fd, "f", max_size=10)


def test_a_name_with_a_slash_is_a_programming_error(box):
    _d, fd = box
    with pytest.raises(ValueError):
        safefile.read_plain(fd, "sub/f", max_size=10)


def test_copy_plain_writes_by_rename_and_returns_the_digest(box, tmp_path):
    d, fd = box
    (d / "f").write_bytes(b"abcd")
    dest = tmp_path / "out.bin"
    digest = safefile.copy_plain(fd, "f", dest, max_size=10, exact=4)
    assert dest.read_bytes() == b"abcd"
    assert digest == "88d4266fd4e6338d13b845fcf289579d209c897823b9217da3e161936f031589"
    assert not (tmp_path / "out.bin.tmp").exists()


def test_a_symlink_as_the_scratch_directory_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(safefile.NotPlainFile):
        safefile.open_dir(tmp_path / "link")


def test_remove_tree_does_not_follow_a_symlink_at_the_top_or_inside(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    top = tmp_path / "top"
    top.symlink_to(victim)
    safefile.remove_tree(top)
    assert not top.exists() and not top.is_symlink() and (victim / "keep").exists()
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "dirlink").symlink_to(victim)
    (inside / "filelink").symlink_to(victim / "keep")
    safefile.remove_tree(inside)
    assert not inside.exists() and (victim / "keep").read_text() == "x"


def test_remove_tree_removes_directories_the_sandbox_made_unwritable(tmp_path):
    d = tmp_path / "d"
    (d / "sub" / "deeper").mkdir(parents=True)
    (d / "sub" / "deeper" / "f").write_text("x")
    os.chmod(d / "sub" / "deeper", 0o500)
    os.chmod(d / "sub", 0o000)
    safefile.remove_tree(d)
    assert not d.exists()


def test_make_empty_dir_replaces_a_symlink_or_a_file(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "s"
    link.symlink_to(target)
    safefile.make_empty_dir(link)
    assert link.is_dir() and not link.is_symlink() and target.exists()
    (tmp_path / "f").write_text("x")
    safefile.make_empty_dir(tmp_path / "f")
    assert (tmp_path / "f").is_dir()


def test_tree_size_counts_files_and_does_not_follow_links(tmp_path):
    (tmp_path / "big").write_bytes(b"x" * 1_000_000)
    d = tmp_path / "d"
    d.mkdir()
    (d / "a").write_bytes(b"x" * 100_000)
    (d / "l").symlink_to(tmp_path / "big")
    assert 100_000 <= safefile.tree_size(d) < 500_000


def test_tree_usage_counts_entries_and_scratch_overrun_names_bytes_or_entries(tmp_path, monkeypatch):
    d = tmp_path / "s"
    (d / "a" / "b").mkdir(parents=True)
    for i in range(5):
        (d / "a" / f"f{i}").write_text("x")
    (d / "a" / "b" / "g").write_text("y")
    used, entries = safefile.tree_usage(d)
    assert entries == 8 and used > 0  # a, a/b, five files, one file
    assert safefile.scratch_overrun(d) == ""
    monkeypatch.setattr(safefile, "MAX_ENTRIES", 7)
    assert "8 entries, at most 7" in safefile.scratch_overrun(d)
    monkeypatch.setattr(safefile, "MAX_ENTRIES", 100)
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 1)
    assert "bytes, at most 1 allowed" in safefile.scratch_overrun(d)
