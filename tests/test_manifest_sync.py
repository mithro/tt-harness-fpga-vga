# SPDX-License-Identifier: Apache-2.0
"""sync_videos_repo against a real local bare repository: clone, no-op when pinned, move on a new pin."""

import pytest
from gitrepo import commit_files, git, init

from tthfv import manifest, paths, pins


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """A bare repository with two commits; returns (first, second). The URL is a plain path, which is
    fine here: pins.VIDEOS_REPO_URL is our own constant, only manifest entries must be https."""
    src = init(tmp_path / "src")
    first = commit_files(src, {"a.txt": "one\n"}, "first")
    second = commit_files(src, {"a.txt": "two\n"}, "second")
    bare = tmp_path / "upstream.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    monkeypatch.setattr(pins, "VIDEOS_REPO_URL", str(bare))
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return first, second


def head(dest) -> str:
    return git(dest, "rev-parse", "HEAD")


def test_sync_clones_and_pins_the_commit(upstream, monkeypatch):
    first, _second = upstream
    monkeypatch.setattr(pins, "VIDEOS_REPO_COMMIT", first)
    dest = manifest.sync_videos_repo()
    assert dest == paths.WORK / "videos-repo" and (dest / ".git").exists()
    assert head(dest) == first
    assert (dest / "a.txt").read_text() == "one\n"


def test_sync_again_is_a_no_op(upstream, monkeypatch):
    first, _second = upstream
    monkeypatch.setattr(pins, "VIDEOS_REPO_COMMIT", first)
    dest = manifest.sync_videos_repo()
    marker = dest / "untracked"
    marker.write_text("kept")
    assert manifest.sync_videos_repo() == dest
    assert head(dest) == first and marker.read_text() == "kept"


def test_repinning_moves_head(upstream, monkeypatch):
    first, second = upstream
    monkeypatch.setattr(pins, "VIDEOS_REPO_COMMIT", first)
    dest = manifest.sync_videos_repo()
    monkeypatch.setattr(pins, "VIDEOS_REPO_COMMIT", second)
    assert manifest.sync_videos_repo() == dest
    assert head(dest) == second
    assert (dest / "a.txt").read_text() == "two\n"
