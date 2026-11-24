# SPDX-License-Identifier: Apache-2.0
"""safefile's removal and size walk on trees built to defeat path-based code: deeper than PATH_MAX, unreadable
directories, huge directories, odd names."""

import os
import time

from tthfv import safefile


def deep_chain(base, depth, leaf):
    """base/d/d/.../d (depth levels, far beyond PATH_MAX) with leaf(fd) run in the deepest directory."""
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(depth):
        os.mkdir("d", dir_fd=fd)
        nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = nxt
    try:
        leaf(fd)
    finally:
        os.close(fd)


def test_a_chain_far_deeper_than_path_max_ending_in_a_mode_000_directory_is_removed(tmp_path):
    base = tmp_path / "scratch"
    base.mkdir()

    def leaf(fd):
        os.mkdir("locked", dir_fd=fd)
        lfd = os.open("locked", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        for n in ("a", "b"):
            os.close(os.open(n, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=lfd))
        os.close(lfd)
        os.chmod("locked", 0o000, dir_fd=fd)

    deep_chain(base, 2500, leaf)
    safefile.remove_tree(base)
    assert not base.exists()


def test_a_hundred_thousand_files_are_removed_in_time(tmp_path):
    base = tmp_path / "scratch"
    base.mkdir()
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    for i in range(100_000):
        os.close(os.open(f"f{i}", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
    os.close(fd)
    started = time.monotonic()
    safefile.remove_tree(base)
    assert not base.exists() and time.monotonic() - started < 60


def test_names_with_newlines_and_a_dangling_link_are_removed(tmp_path):
    base = tmp_path / "scratch"
    (base / "a\nb").mkdir(parents=True)
    (base / "a\nb" / "x\ny").write_text("1")
    (base / "l\n").symlink_to(tmp_path / "nowhere")
    safefile.remove_tree(base)
    assert not base.exists()


def test_a_symlink_to_a_directory_at_the_top_is_unlinked_not_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    link = tmp_path / "scratch"
    link.symlink_to(victim)
    safefile.remove_tree(link)
    assert not link.is_symlink() and (victim / "keep").exists()


def test_symlinks_inside_are_removed_without_touching_their_targets(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    base = tmp_path / "scratch"
    base.mkdir()
    (base / "dl").symlink_to(victim)
    safefile.remove_tree(base)
    assert not base.exists() and (victim / "keep").read_text() == "x"


def test_tree_size_counts_a_file_deeper_than_path_max(tmp_path):
    base = tmp_path / "scratch"
    base.mkdir()

    def leaf(fd):
        f = os.open("big", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
        os.write(f, os.urandom(1 << 20) * 50)
        os.close(f)

    deep_chain(base, 2100, leaf)
    assert safefile.tree_size(base) >= 50 << 20
    safefile.remove_tree(base)


def test_discard_moves_aside_what_cannot_be_removed(tmp_path, monkeypatch):
    base = tmp_path / "scratch"
    base.mkdir()
    (base / "f").write_text("x")
    trash = tmp_path / "trash"

    def broken(path):
        raise PermissionError("no")

    monkeypatch.setattr(safefile, "remove_tree", broken)
    note = safefile.discard(base, trash)
    assert "could not be removed" in note and not base.exists()
    assert next(trash.iterdir()) / "f"
    assert (next(trash.iterdir()) / "f").exists()
    assert safefile.discard(tmp_path / "gone", trash) == ""
