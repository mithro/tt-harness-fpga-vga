# SPDX-License-Identifier: Apache-2.0
import dataclasses
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tthfv import fetch, paths
from tthfv.manifest import Design

IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **IDENTITY)  # read at call time so tests can add GIT_CONFIG_*
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


def commit_files(repo: Path, files: dict[str, str], msg: str = "c") -> str:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return tmp_path / "work"


@pytest.fixture
def project(tmp_path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    return repo


def design(repo: Path, commit: str, **kw) -> Design:
    base = Design(
        id="tt99/tt_um_x", shuttle="tt99", macro="tt_um_x", title="", author="", repo=str(repo),
        commit=commit, top_module="tt_um_x", source_files=("a.v",), clock_hz=25_175_000,
        clock_declared=True, pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={},
        patch=None, qspi=None, ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    return dataclasses.replace(base, **kw)


def test_fetch_gives_pinned_commit_not_head(project):
    first = commit_files(project, {"src/a.v": "module one;\n"})
    commit_files(project, {"src/a.v": "module two;\n"})
    r = fetch.fetch(design(project, first))
    assert r.status == "ok", r.reason
    assert r.sources == (r.src_dir / "src" / "a.v",)
    assert r.sources[0].read_text() == "module one;\n"
    assert r.sources[0].is_absolute()
    assert r.src_dir / "src" in r.include_dirs
    assert r.log.exists()


def test_subdir_sources_resolve_under_subdir_src(project):
    c = commit_files(project, {"designs/top/src/a.v": "module a;\n", "src/a.v": "wrong\n"})
    r = fetch.fetch(design(project, c, subdir="designs/top"))
    assert r.status == "ok", r.reason
    assert r.sources[0].read_text() == "module a;\n"
    assert r.src_dir.name == "top" and r.src_dir.parent.name == "designs"
    assert r.src_dir / "src" in r.include_dirs


def test_missing_commit_is_fetch_fail_with_git_error(project):
    commit_files(project, {"src/a.v": "x\n"})
    r = fetch.fetch(design(project, "0123456789abcdef0123456789abcdef01234567"))
    assert r.status == "fetch-fail"
    assert "fetch" in r.reason and r.reason.strip()
    assert r.sources == ()


def test_unreachable_repo_is_fetch_fail(tmp_path):
    r = fetch.fetch(design(tmp_path / "nope", "0" * 40))
    assert r.status == "fetch-fail"


def test_patch_applies(project, tmp_path):
    c = commit_files(project, {"src/a.v": "module a;\n"})
    patch = tmp_path / "p.diff"
    patch.write_text("--- a/src/a.v\n+++ b/src/a.v\n@@ -1 +1 @@\n-module a;\n+module patched;\n")
    r = fetch.fetch(design(project, c, patch=patch))
    assert r.status == "ok", r.reason
    assert r.sources[0].read_text() == "module patched;\n"


def test_bad_patch_is_patch_fail(project, tmp_path):
    c = commit_files(project, {"src/a.v": "module a;\n"})
    patch = tmp_path / "p.diff"
    patch.write_text("--- a/src/a.v\n+++ b/src/a.v\n@@ -1 +1 @@\n-nothing like it\n+x\n")
    r = fetch.fetch(design(project, c, patch=patch))
    assert r.status == "patch-fail"
    assert r.reason


def test_missing_source_file_names_it(project):
    c = commit_files(project, {"src/a.v": "x\n"})
    r = fetch.fetch(design(project, c, source_files=("a.v", "gone.v")))
    assert r.status == "fetch-fail"
    assert "gone.v" in r.reason


def test_second_call_does_no_network_work(project):
    c = commit_files(project, {"src/a.v": "x\n"})
    d = design(project, c)
    first = fetch.fetch(d)
    assert first.status == "ok"
    shutil.rmtree(project)
    again = fetch.fetch(d)
    assert again.status == "ok" and again.sources == first.sources


def test_force_refetches(project):
    c = commit_files(project, {"src/a.v": "x\n"})
    d = design(project, c)
    assert fetch.fetch(d).status == "ok"
    shutil.rmtree(project)
    assert fetch.fetch(d, force=True).status == "fetch-fail"


def test_changed_commit_invalidates_cache(project):
    one = commit_files(project, {"src/a.v": "one\n"})
    two = commit_files(project, {"src/a.v": "two\n"})
    assert fetch.fetch(design(project, one)).sources[0].read_text() == "one\n"
    assert fetch.fetch(design(project, two)).sources[0].read_text() == "two\n"


def test_failed_fetch_is_not_cached(project):
    c = commit_files(project, {"src/a.v": "x\n"})
    d = design(project, c, source_files=("a.v", "late.v"))
    assert fetch.fetch(d).status == "fetch-fail"
    commit_files(project, {"src/late.v": "y\n"})  # does not change the pinned commit
    assert fetch.fetch(d).status == "fetch-fail"  # reruns rather than returning a stale result


def test_submodules_are_fetched(project, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    lib = tmp_path / "lib"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    commit_files(lib, {"lib.v": "module lib;\n"})
    commit_files(project, {"src/a.v": "x\n"})
    git(project, "submodule", "add", "-q", str(lib), "src/lib")
    c = commit_files(project, {}, "add submodule")
    d = design(project, c, source_files=("a.v", "lib/lib.v"))
    r = fetch.fetch(d)
    assert r.status == "ok", r.reason
    assert r.sources[1].read_text() == "module lib;\n"
