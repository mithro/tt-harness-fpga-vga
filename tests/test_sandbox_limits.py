# SPDX-License-Identifier: Apache-2.0
"""More of what the sandbox takes away: nested user namespaces, the host name, unbounded tmpfs, unbounded
resource use. Real bwrap; skipped where it cannot run."""

import socket
import subprocess

import pytest

from tthfv import sandbox

SKIP = "" if sandbox.available() else f"bwrap cannot run here: {sandbox.unavailable_reason()}"
needs_bwrap = pytest.mark.skipif(bool(SKIP), reason=SKIP)


@pytest.fixture
def dirs(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    rw = tmp_path / "rw"
    rw.mkdir()
    return ro, rw


def sh(script: str, dirs) -> subprocess.CompletedProcess:
    ro, rw = dirs
    argv = sandbox.wrap(["sh", "-c", script], cwd=rw, rw=[rw], ro=[ro])
    return subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
    )


@needs_bwrap
def test_a_nested_user_namespace_cannot_be_made(dirs):
    r = sh("unshare -Ur true", dirs)
    assert r.returncode != 0, "a program in the sandbox could make a user namespace of its own"


@needs_bwrap
def test_the_host_name_is_not_the_hosts(dirs):
    r = sh("cat /proc/sys/kernel/hostname", dirs)
    assert r.stdout.strip() == "sandbox" and socket.gethostname() != "sandbox"


@needs_bwrap
def test_the_tmpfs_mounts_are_size_limited(dirs):
    big = sandbox.TMPFS_BYTES + (1 << 20)
    for where in ("/tmp", sandbox.HOME):
        assert sh(f"head -c {big} /dev/zero > {where}/big", dirs).returncode != 0, where
    assert sh("echo ok > /tmp/small", dirs).returncode == 0


@needs_bwrap
def test_resource_limits_apply_to_everything_inside(dirs):
    r = sh("cat /proc/self/limits", dirs)
    assert r.returncode == 0, r.stderr
    rows = {ln[:26].strip(): ln[26:].split() for ln in r.stdout.splitlines()}
    assert rows["Max address space"][:2] == ["17179869184"] * 2
    assert rows["Max file size"][:2] == ["1073741824"] * 2
    assert rows["Max core file size"][:2] == ["0", "0"]
    assert rows["Max open files"][:2] == ["1024", "1024"]


def test_prlimit_is_called_by_its_absolute_path_not_through_the_jails_path(dirs):
    ro, rw = dirs
    argv = sandbox.wrap(["true"], cwd=rw, rw=[rw], ro=[ro], path=[ro])
    after = argv[argv.index("--") + 1]
    assert after == sandbox.PRLIMIT == "/usr/bin/prlimit"


def test_a_missing_prlimit_makes_the_sandbox_unavailable_with_a_clear_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "PRLIMIT", str(tmp_path / "no-prlimit"))
    sandbox.reset()
    try:
        assert not sandbox.available()
        assert "prlimit" in sandbox.unavailable_reason()
    finally:
        sandbox.reset()
