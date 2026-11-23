# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""git runs unsandboxed on repositories chosen by third parties, so nothing the owner configured for git may
apply: no filter (git-lfs), no ssh, whatever the owner's global configuration and PATH say."""

import os
import stat
import subprocess
from pathlib import Path

import pytest
from test_fetch import TOP, commit_files, design, git, project, work  # noqa: F401 - fixtures

from tthfv import fetch, gitenv


def fake_program(directory: Path, name: str, marker: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text(f'#!/bin/sh\necho "$0 $@" >> {marker}\nexit 1\n')
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """The owner's environment: a global git configuration with an lfs filter, ssh and git-lfs on PATH (fakes
    that leave a marker), and no hardening of their own."""
    marker = tmp_path / "ran-markers"
    bindir = tmp_path / "fakebin"
    fake_program(bindir, "git-lfs", marker)
    fake_program(bindir, "ssh", marker)
    cfg = tmp_path / "owner-gitconfig"
    cfg.write_text(
        '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n'
        "\tprocess = git-lfs filter-process\n\trequired = true\n"
    )
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    return marker


def test_the_environment_is_built_from_an_allow_list_and_locked_down(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "x")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /owner/key")
    monkeypatch.setenv("GIT_EXEC_PATH", "/elsewhere")
    monkeypatch.setenv("TTHFV_WORK", str(tmp_path))
    env = gitenv.git_env()
    assert "SECRET_TOKEN" not in env and env["GIT_SSH_COMMAND"] == "/bin/false" and "GIT_EXEC_PATH" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "/bin/true"
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert pairs["core.hooksPath"] == "/dev/null" and pairs["core.fsmonitor"] == "false"
    assert pairs["protocol.allow"] == "never" and pairs["protocol.https.allow"] == "always"
    assert pairs["protocol.ext.allow"] == "never"
    assert os.listdir(env["HOME"]) == []  # an empty directory


def test_the_default_allows_https_only(monkeypatch):
    monkeypatch.delenv("TTHFV_TEST_ALLOW_FILE_PROTOCOL")
    env = gitenv.git_env()
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert pairs["protocol.file.allow"] == "never"


def lfs_repo(project, monkeypatch):
    commit_files(
        project, {"src/a.v": TOP}
    )  # before the filter is declared: the owner's filter must not see it
    with monkeypatch.context() as m:  # making the commit must not run the owner's filter either
        m.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        m.setenv("GIT_CONFIG_NOSYSTEM", "1")
        return commit_files(
            project,
            {
                ".gitattributes": "*.v filter=lfs diff=lfs merge=lfs -text\n",
                ".lfsconfig": "[lfs]\n\turl = https://example.invalid/lfs\n",
            },
        )


def test_a_filter_lfs_repository_does_not_run_git_lfs(project, owner, monkeypatch):
    c = lfs_repo(project, monkeypatch)
    control = project.parent / "control"
    subprocess.run(["git", "clone", "-q", "--", str(project), str(control)], check=False, capture_output=True)
    assert owner.exists(), "the control: with the owner's configuration plain git runs git-lfs here"
    owner.unlink()
    r = fetch.fetch(design(project, c))
    assert r.status == "ok", r.reason
    assert not owner.exists(), owner.read_text()


def ssh_submodule_repo(project, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    commit_files(lib, {"lib.v": "module lib;\nendmodule\n"})
    commit_files(project, {"src/a.v": TOP})
    env = dict(
        os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.file.allow", GIT_CONFIG_VALUE_0="always"
    )
    subprocess.run(
        ["git", "submodule", "add", "-q", str(lib), "src/lib"],
        cwd=project,
        check=True,
        capture_output=True,
        env=env,
    )
    modules = project / ".gitmodules"
    modules.write_text(modules.read_text().replace(str(lib), "ssh://git@host.invalid/x.git"))
    return commit_files(project, {}, "an ssh submodule")


def test_a_submodule_with_an_ssh_url_does_not_run_ssh(project, tmp_path, owner):
    c = ssh_submodule_repo(project, tmp_path)
    control = tmp_path / "control"
    subprocess.run(["git", "clone", "-q", "--", str(project), str(control)], check=True, capture_output=True)
    subprocess.run(["git", "submodule", "update", "--init"], cwd=control, check=False, capture_output=True)
    assert owner.exists(), "the control: plain git runs ssh for the submodule here"
    owner.unlink()
    r = fetch.fetch(design(project, c, source_files=("a.v", "lib/lib.v")))
    assert r.status == "fetch-fail"
    assert not owner.exists(), owner.read_text()


def test_a_github_ssh_submodule_url_is_still_turned_into_https(project, tmp_path, owner, monkeypatch):
    """The project's own rewrite (git@github.com: -> https://github.com/) keeps working: git then tries https,
    which cannot be reached here, and never ssh."""
    lib = tmp_path / "lib"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    commit_files(lib, {"lib.v": "module lib;\nendmodule\n"})
    commit_files(project, {"src/a.v": TOP})
    subprocess.run(
        ["git", "submodule", "add", "-q", str(lib), "src/lib"],
        cwd=project,
        check=True,
        capture_output=True,
        env=dict(
            os.environ,
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="protocol.file.allow",
            GIT_CONFIG_VALUE_0="always",
        ),
    )
    modules = project / ".gitmodules"
    modules.write_text(modules.read_text().replace(str(lib), "git@github.com:example/none.git"))
    c = commit_files(project, {}, "a github ssh submodule")
    r = fetch.fetch(design(project, c, source_files=("a.v", "lib/lib.v")))
    assert r.status == "fetch-fail"
    log = r.log.read_text()
    assert "https://github.com/example/none.git" in log
    assert not owner.exists()
