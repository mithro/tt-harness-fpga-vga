# SPDX-License-Identifier: Apache-2.0
"""Loading the manifest before the videos repo is synced is a clear UserError."""

import pytest

from tthfv import manifest, paths
from tthfv.cli import main
from tthfv.errors import UserError


def test_load_without_a_synced_videos_repo_says_to_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    monkeypatch.delenv("TTHFV_VIDEOS_REPO", raising=False)
    with pytest.raises(UserError, match="tthfv manifest sync"):
        manifest.load()


def test_an_explicit_missing_repo_says_to_sync_too(tmp_path):
    with pytest.raises(UserError, match="tthfv manifest sync"):
        manifest.load(videos_repo=tmp_path / "nope")


def test_the_cli_shows_the_message_without_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    monkeypatch.delenv("TTHFV_VIDEOS_REPO", raising=False)
    assert main(["manifest", "list"]) == 1
    assert "tthfv manifest sync" in capsys.readouterr().err
