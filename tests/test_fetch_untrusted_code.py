# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""Fetching runs git on third-party repositories without a sandbox (it needs the network). Nothing a
repository carries or a hook configured on the host may run a program through git."""

import os
import stat
import subprocess
from pathlib import Path

import pytest
from test_fetch import TOP, commit_files, design, git, project, work  # noqa: F401 - fixtures

from tthfv import fetch


def script(path: Path, marker: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_git_gets_the_safety_settings_through_its_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    env = fetch.git_env()
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert pairs["protocol.file.allow"] == "always"  # what the caller set is kept
    assert pairs["core.hooksPath"] == "/dev/null"
    assert pairs["protocol.ext.allow"] == "never"
    assert pairs["core.fsmonitor"] == "false"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_hook_that_git_would_install_in_the_new_checkout_does_not_run(project, tmp_path, monkeypatch):
    """git init copies hooks from its template directory; post-checkout would run on our checkout."""
    marker = tmp_path / "hook-ran"
    template = tmp_path / "template"
    script(template / "hooks" / "post-checkout", marker)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    c = commit_files(project, {"src/a.v": TOP})

    control = tmp_path / "control"
    control.mkdir()
    git(control, "init", "-q", "-b", "main")
    commit_files(control, {"f": "x"})
    git(control, "checkout", "-q", "-b", "other")
    assert marker.exists(), "the control: this host's git runs a template hook, so the test means something"
    marker.unlink()

    r = fetch.fetch(design(project, c))
    assert r.status == "ok", r.reason
    assert not marker.exists()


def test_a_fsmonitor_hook_from_the_host_configuration_does_not_run(project, tmp_path, monkeypatch):
    marker = tmp_path / "fsmonitor-ran"
    hook = script(tmp_path / "fsmonitor.sh", marker)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[core]\n\tfsmonitor = {hook}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    c = commit_files(project, {"src/a.v": TOP})

    control = tmp_path / "control"
    control.mkdir()
    git(control, "init", "-q")
    (control / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=control, check=True, capture_output=True)
    subprocess.run(["git", "status"], cwd=control, check=True, capture_output=True)
    if not marker.exists():
        pytest.skip("this git does not run the fsmonitor hook on status, so there is nothing to test")
    marker.unlink()

    r = fetch.fetch(design(project, c))
    assert r.status == "ok", r.reason
    assert not marker.exists()


def with_submodule(project, tmp_path, monkeypatch, gitmodules_extra: str, url: str | None = None):
    """A project whose submodule section is hostile. Returns the commit."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    lib = tmp_path / "lib"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    commit_files(lib, {"lib.v": "module lib;\nendmodule\n"})
    commit_files(project, {"src/a.v": TOP})
    git(project, "submodule", "add", "-q", str(lib), "src/lib")
    modules = project / ".gitmodules"
    text = modules.read_text()
    if url:
        text = text.replace(str(lib), url)
    modules.write_text(text + gitmodules_extra)
    return commit_files(project, {}, "hostile submodule")


def test_a_gitmodules_update_command_does_not_run(project, tmp_path, monkeypatch):
    marker = tmp_path / "pwned"
    c = with_submodule(project, tmp_path, monkeypatch, f"\tupdate = !touch {marker}\n")
    fetch.fetch(design(project, c, source_files=("a.v", "lib/lib.v")))  # ok or fetch-fail, either way
    assert not marker.exists()
    assert not (tmp_path / "work" / "src" / "tt99" / "tt_um_x" / "repo" / "pwned").exists()


def test_a_submodule_url_that_runs_a_command_does_not_run_it(project, tmp_path, monkeypatch):
    marker = tmp_path / "pwned"
    c = with_submodule(project, tmp_path, monkeypatch, "", url=f"ext::sh -c 'touch {marker}'")
    r = fetch.fetch(design(project, c, source_files=("a.v", "lib/lib.v")))
    assert r.status == "fetch-fail"
    assert not marker.exists()


def test_the_checkout_has_no_hooks_of_the_repository(project, tmp_path):
    """Hooks are not part of what git transfers; make sure none turns up in the checkout either."""
    marker = tmp_path / "pwned"
    script(project / ".git" / "hooks" / "post-checkout", marker)
    c = commit_files(project, {"src/a.v": TOP})
    r = fetch.fetch(design(project, c))
    assert r.status == "ok", r.reason
    assert not marker.exists()
    assert not (r.checkout / ".git" / "hooks" / "post-checkout").exists()
    assert os.path.isdir(r.checkout / ".git")
