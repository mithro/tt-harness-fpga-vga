# SPDX-License-Identifier: Apache-2.0
import dataclasses
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tthfv import fetch, paths
from tthfv.manifest import Design

TOP = "module tt_um_x;\nendmodule\n"
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
    first = commit_files(project, {"src/a.v": TOP.replace("endmodule", "// one\nendmodule")})
    commit_files(project, {"src/a.v": TOP.replace("endmodule", "// two\nendmodule")})
    r = fetch.fetch(design(project, first))
    assert r.status == "ok", r.reason
    assert r.sources == (r.src_dir / "src" / "a.v",)
    assert r.sources[0].read_text() == TOP.replace("endmodule", "// one\nendmodule")
    assert r.sources[0].is_absolute()
    assert r.src_dir / "src" in r.include_dirs
    assert r.log.exists()


def test_subdir_sources_resolve_under_subdir_src(project):
    c = commit_files(project, {"designs/top/src/a.v": TOP, "src/a.v": "wrong\n"})
    r = fetch.fetch(design(project, c, subdir="designs/top"))
    assert r.status == "ok", r.reason
    assert r.sources[0].read_text() == TOP
    assert r.src_dir.name == "top" and r.src_dir.parent.name == "designs"
    assert r.src_dir / "src" in r.include_dirs


def test_missing_commit_is_fetch_fail_with_git_error(project):
    commit_files(project, {"src/a.v": TOP})
    r = fetch.fetch(design(project, "0123456789abcdef0123456789abcdef01234567"))
    assert r.status == "fetch-fail"
    assert "fetch" in r.reason and r.reason.strip()
    assert r.sources == ()


def test_unreachable_repo_is_fetch_fail(tmp_path):
    r = fetch.fetch(design(tmp_path / "nope", "0" * 40))
    assert r.status == "fetch-fail"


def test_patch_applies(project, tmp_path):
    c = commit_files(project, {"src/a.v": TOP})
    patch = tmp_path / "p.diff"
    patch.write_text("--- a/src/a.v\n+++ b/src/a.v\n@@ -1,2 +1,2 @@\n-module tt_um_x;\n+module tt_um_x; // patched\n endmodule\n")
    r = fetch.fetch(design(project, c, patch=patch))
    assert r.status == "ok", r.reason
    assert r.sources[0].read_text() == "module tt_um_x; // patched\nendmodule\n"


def test_bad_patch_is_patch_fail(project, tmp_path):
    c = commit_files(project, {"src/a.v": TOP})
    patch = tmp_path / "p.diff"
    patch.write_text("--- a/src/a.v\n+++ b/src/a.v\n@@ -1 +1 @@\n-nothing like it\n+x\n")
    r = fetch.fetch(design(project, c, patch=patch))
    assert r.status == "patch-fail"
    assert r.reason


def test_missing_source_file_names_it(project):
    c = commit_files(project, {"src/a.v": TOP})
    r = fetch.fetch(design(project, c, source_files=("a.v", "gone.v")))
    assert r.status == "fetch-fail"
    assert "gone.v" in r.reason


def test_second_call_does_no_network_work(project):
    c = commit_files(project, {"src/a.v": TOP})
    d = design(project, c)
    first = fetch.fetch(d)
    assert first.status == "ok"
    shutil.rmtree(project)
    again = fetch.fetch(d)
    assert again.status == "ok" and again.sources == first.sources


def test_force_refetches(project):
    c = commit_files(project, {"src/a.v": TOP})
    d = design(project, c)
    assert fetch.fetch(d).status == "ok"
    shutil.rmtree(project)
    assert fetch.fetch(d, force=True).status == "fetch-fail"


def test_changed_commit_invalidates_cache(project):
    one = commit_files(project, {"src/a.v": TOP.replace("endmodule", "// one\nendmodule")})
    two = commit_files(project, {"src/a.v": TOP.replace("endmodule", "// two\nendmodule")})
    assert fetch.fetch(design(project, one)).sources[0].read_text() == TOP.replace("endmodule", "// one\nendmodule")
    assert fetch.fetch(design(project, two)).sources[0].read_text() == TOP.replace("endmodule", "// two\nendmodule")


def test_failed_fetch_is_not_cached(project):
    c = commit_files(project, {"src/a.v": TOP})
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
    commit_files(lib, {"lib.v": "module lib;\nendmodule\n"})
    commit_files(project, {"src/a.v": TOP})
    git(project, "submodule", "add", "-q", str(lib), "src/lib")
    c = commit_files(project, {}, "add submodule")
    d = design(project, c, source_files=("a.v", "lib/lib.v"))
    r = fetch.fetch(d)
    assert r.status == "ok", r.reason
    assert r.sources[1].read_text() == "module lib;\nendmodule\n"


def test_source_escaping_checkout_is_fetch_fail(project, tmp_path):
    work = tmp_path / "work" / "src" / "tt99" / "tt_um_x"
    work.mkdir(parents=True)
    c = commit_files(project, {"src/a.v": TOP})
    # <work>/repo/src/../../outside.v is <work>/outside.v: outside the checkout but existing.
    (work / "outside.v").write_text("evil\n")
    r = fetch.fetch(design(project, c, source_files=("a.v", "../../outside.v")))
    assert r.status == "fetch-fail"
    assert "../../outside.v" in r.reason
    assert r.sources == ()


def test_absolute_source_outside_checkout_is_fetch_fail(project, tmp_path):
    c = commit_files(project, {"src/a.v": TOP})
    outside = tmp_path / "abs.v"
    outside.write_text("evil\n")
    r = fetch.fetch(design(project, c, source_files=(str(outside),)))
    assert r.status == "fetch-fail"
    assert str(outside) in r.reason


def test_symlink_escaping_checkout_is_fetch_fail(project, tmp_path):
    outside = tmp_path / "target.v"
    outside.write_text("evil\n")
    (project / "src").mkdir()
    (project / "src" / "link.v").symlink_to(outside)
    c = commit_files(project, {})
    r = fetch.fetch(design(project, c, source_files=("link.v",)))
    assert r.status == "fetch-fail"
    assert "link.v" in r.reason


def test_source_in_sibling_dir_of_same_repo_is_ok(project):
    c = commit_files(project, {"src/a.v": TOP, "lib/x.v": "module x;\nendmodule\n"})
    r = fetch.fetch(design(project, c, source_files=("a.v", "../lib/x.v")))
    assert r.status == "ok", r.reason
    assert r.sources[1].read_text() == "module x;\nendmodule\n"


def test_missing_git_is_fetch_fail_not_exception(project, monkeypatch):
    c = commit_files(project, {"src/a.v": TOP})
    monkeypatch.setenv("PATH", "")
    r = fetch.fetch(design(project, c))
    assert r.status == "fetch-fail"
    assert "git" in r.reason
    assert "git" in r.log.read_text()


def test_timeout_is_fetch_fail_not_exception(project, monkeypatch, tmp_path):
    c = commit_files(project, {"src/a.v": TOP})
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\nsleep 30\n")
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setattr(fetch.proc, "GIT_TIMEOUT", 1)
    r = fetch.fetch(design(project, c))
    assert r.status == "fetch-fail"
    assert "timed out" in r.reason


def test_failed_force_keeps_old_checkout(project):
    c = commit_files(project, {"src/a.v": TOP})
    d = design(project, c)
    first = fetch.fetch(d)
    shutil.rmtree(project)
    assert fetch.fetch(d, force=True).status == "fetch-fail"
    assert first.sources[0].read_text() == TOP
