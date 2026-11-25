# SPDX-License-Identifier: Apache-2.0
"""The bubblewrap sandbox, with the real bwrap. Skipped where bwrap cannot create namespaces (CI may not
allow it); nothing else in the suite depends on it."""

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tthfv import proc, sandbox
from tthfv.errors import UserError

SKIP = "" if sandbox.available() else f"bwrap cannot run here: {sandbox.unavailable_reason()}"
needs_bwrap = pytest.mark.skipif(bool(SKIP), reason=SKIP)


@pytest.fixture
def dirs(tmp_path):
    """A fake home with a secret, one read-only and one writable directory."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "secret.txt").write_text("secret")
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "in.txt").write_text("readable\n")
    rw = tmp_path / "rw"
    rw.mkdir()
    return home, ro, rw


def sh(script: str, dirs, *, timeout=30, extra_ro=()) -> subprocess.CompletedProcess:
    _home, ro, rw = dirs
    argv = sandbox.wrap(["sh", "-c", script], cwd=rw, rw=[rw], ro=[ro, *extra_ro])
    return subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
    )


@needs_bwrap
def test_files_outside_the_allowed_paths_are_invisible(dirs):
    home, _ro, _rw = dirs
    r = sh(f"cat {home}/secret.txt", dirs)
    assert r.returncode != 0 and "secret" not in r.stdout
    here = Path(__file__).resolve()  # a file of this repository
    assert sh(f"test -e {here}", dirs).returncode != 0
    # of the directory that holds the allowed ones only they are there (not "home", its sibling with the
    # secret), wherever it is: under the real home directory or, as on GitHub's runner, under /tmp
    listing = sh(f"ls -A {Path(dirs[2]).parent}", dirs).stdout.split()
    assert listing == ["ro", "rw"]


@needs_bwrap
def test_the_allowed_paths_can_be_read_and_the_rw_directory_written(dirs):
    _home, ro, rw = dirs
    r = sh(f"cat {ro}/in.txt && echo data > {rw}/out.txt && pwd", dirs)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["readable", str(rw)]
    assert (rw / "out.txt").read_text() == "data\n"


@needs_bwrap
def test_nothing_can_be_written_outside_the_rw_directory(dirs, tmp_path):
    _home, ro, _rw = dirs
    outside = tmp_path / "outside.txt"
    for target in (ro / "new.txt", outside, Path("/usr/new.txt"), Path("/new.txt"), Path("/etc/new.txt")):
        r = sh(f"echo x > {target}", dirs)
        # Under /tmp (where pytest's tmp_path is on GitHub's runner) the jail's own private tmpfs is writable
        # on purpose, also at the host's path of `outside`: that write lands in the jail's memory and is gone
        # with it, which the last assertions below check; everywhere else the write must fail.
        if not str(target).startswith("/tmp/"):
            assert r.returncode != 0, target
    assert not (ro / "new.txt").exists() and not outside.exists()
    assert sh(f"echo x >> {ro}/in.txt", dirs).returncode != 0
    assert (ro / "in.txt").read_text() == "readable\n"


@needs_bwrap
def test_tmp_is_private_and_writable(dirs, tmp_path):
    r = sh("echo x > /tmp/mine && cat /tmp/mine", dirs)
    assert r.returncode == 0 and r.stdout == "x\n"
    assert not Path("/tmp/mine").exists()


@needs_bwrap
def test_there_is_no_network(dirs):
    assert sh("getent hosts github.com", dirs).returncode != 0
    _home, ro, rw = dirs
    argv = sandbox.wrap(["bash", "-c", "exec 3<>/dev/tcp/1.1.1.1/53"], cwd=rw, rw=[rw], ro=[ro])
    assert subprocess.run(argv, check=False, capture_output=True, timeout=30).returncode != 0
    names = [ln.split(":")[0].strip() for ln in sh("tail -n +3 /proc/net/dev", dirs).stdout.splitlines()]
    assert names in ([], ["lo"])  # no interface but, at most, a loopback of its own


@needs_bwrap
def test_only_path_home_and_lang_are_in_the_environment(dirs, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    monkeypatch.setenv("VERILATOR_ROOT", "/host/verilator")
    r = sh("env", dirs)
    assert r.returncode == 0
    names = {ln.split("=", 1)[0] for ln in r.stdout.splitlines()}
    assert {"PATH", "HOME", "LANG"} <= names
    assert names <= {"PATH", "HOME", "LANG", "PWD", "OLDPWD", "SHLVL", "_"}
    assert "hunter2" not in r.stdout and "/host/verilator" not in r.stdout
    assert sh('test -z "$(ls -A "$HOME")" && echo x > "$HOME/f"', dirs).returncode == 0  # empty and writable


@needs_bwrap
def test_the_pinned_bin_directory_comes_first_on_path(dirs, tmp_path):
    _home, ro, rw = dirs
    bindir = tmp_path / "pinned" / "bin"
    bindir.mkdir(parents=True)
    argv = sandbox.wrap(["sh", "-c", "echo $PATH"], cwd=rw, rw=[rw], ro=[ro, bindir], path=[bindir])
    r = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == f"{bindir}:/usr/bin:/bin"


def pids_running(token: str) -> list[int]:
    """Every process whose command line (its arguments joined with spaces) contains token: bwrap's and sh's
    carry it as part of the `sh -c` argument and the sleeps as their own arguments."""
    found = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit() and int(p.name) != os.getpid():
            try:
                if token in (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"):
                    found.append(int(p.name))
            except OSError:
                pass
    return found


def payload_pids(token: str) -> list[int]:
    """The processes running the payload: `comm` is `sleep`. bwrap's and sh's command lines carry the token as
    well, but they are not the payload, and a process whose `comm` is `sleep` has exec'd it inside the jail."""
    found = []
    for pid in pids_running(token):
        try:
            if (Path("/proc") / str(pid) / "comm").read_text().strip() == "sleep":
                found.append(pid)
        except OSError:
            pass
    return found


def wait_payload(token: str, count: int, wait=30.0) -> bool:
    """True once `count` payload processes run (not merely once a command line carries the token)."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if len(payload_pids(token)) >= count:
            return True
        time.sleep(0.05)
    return len(payload_pids(token)) >= count


def wait_gone(token: str, wait=10.0) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not pids_running(token):
            return True
        time.sleep(0.1)
    return not pids_running(token)


@needs_bwrap
def test_a_timeout_kills_a_child_that_called_setsid(dirs):
    _home, ro, rw = dirs
    token = f"sleep 4242.{os.getpid()}"
    script = f"setsid {token} & {token}"
    box = sandbox.Sandbox(rw=(rw,), ro=(ro,))
    started = time.monotonic()
    seen = []  # the payload must have been running when the timeout hit, or this proves nothing
    watcher = threading.Thread(target=lambda: seen.append(wait_payload(token, 2, wait=7.5)))
    watcher.start()
    assert proc.run(["sh", "-c", script], 8, cwd=rw, sandbox=box) == proc.TIMEOUT_RC
    watcher.join()
    assert time.monotonic() - started < 25
    assert seen == [True], "the payload (two sleeps) was not running when the timeout hit"
    assert wait_gone(token), pids_running(token)


@needs_bwrap
def test_killing_the_parent_kills_everything_inside(dirs):
    _home, ro, rw = dirs
    token = f"sleep 4343.{os.getpid()}"
    argv = sandbox.wrap(["sh", "-c", f"setsid {token} & {token}"], cwd=rw, rw=[rw], ro=[ro])
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    # started = both sleeps run inside the jail; bwrap's own command line holds the token from its exec on
    assert wait_payload(token, 2), (pids_running(token), payload_pids(token))
    p.kill()
    p.wait()
    assert wait_gone(token), pids_running(token)


@needs_bwrap
def test_run_logged_through_the_sandbox_keeps_output_and_the_command_line(dirs):
    _home, ro, rw = dirs
    box = sandbox.Sandbox(rw=(rw,), ro=(ro,))
    log = rw / "x.log"
    r = proc.run_logged(["sh", "-c", "echo hello; pwd"], 20, log, cwd=rw, sandbox=box)
    assert r.rc == 0 and r.text.split() == ["hello", str(rw)]
    assert log.read_text().startswith("$ sh -c echo hello; pwd\n")  # the command, not the bwrap line


def test_the_mode_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    assert sandbox.mode() == "none"
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    assert sandbox.mode() == "bwrap"
    monkeypatch.setenv("TTHFV_SANDBOX", "docker")
    with pytest.raises(UserError, match="TTHFV_SANDBOX"):
        sandbox.mode()
    monkeypatch.delenv("TTHFV_SANDBOX")
    assert sandbox.mode() == "bwrap"  # the default; never silently none


def test_a_missing_bwrap_is_a_user_error_not_an_unsandboxed_run(monkeypatch, tmp_path):
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.setattr(sandbox, "BWRAP", str(tmp_path / "no-such-bwrap"))
    sandbox.reset()
    try:
        with pytest.raises(UserError, match="TTHFV_SANDBOX=none"):
            sandbox.for_stage(rw=tmp_path, ro=[])
    finally:
        sandbox.reset()


def test_none_gives_no_sandbox_and_announce_warns_once(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    monkeypatch.setattr(sandbox, "_warned", False)
    sandbox.announce()
    sandbox.announce()
    assert sandbox.for_stage(rw=tmp_path, ro=[]) is None
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1 and "TTHFV_SANDBOX=none" in err  # once, and not again per stage


@pytest.mark.parametrize(
    "bad",
    [
        "/usr/share",
        "/etc/foo",
        "/proc",
        "/dev/shm",
        "/bin",
        "/lib64",
        "/sandbox-home",
        "/sandbox-home/x",
        "/",
        "/tmp",
    ],
)
def test_paths_the_jail_builds_itself_are_refused(bad, tmp_path):
    with pytest.raises(UserError, match="sandbox"):
        sandbox.check_paths([Path(bad)])
    monkey_ro = [Path(bad)]
    with pytest.raises(UserError):
        sandbox.wrap(["true"], cwd=tmp_path, rw=[tmp_path], ro=monkey_ro)


def test_a_path_under_tmp_or_home_is_accepted(tmp_path):
    sandbox.check_paths([tmp_path, Path("/tmp/pytest-of-runner/x"), Path("/srv/runner/work")])


def test_for_stage_refuses_a_work_directory_under_usr(monkeypatch, tmp_path):
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    with pytest.raises(UserError, match="/usr"):
        sandbox.for_stage(rw=Path("/usr/work/scratch"), ro=[tmp_path])


def test_where_the_sandbox_is_required_it_must_work():
    """CI sets TTHFV_REQUIRE_BWRAP=1: there the bubblewrap tests above must run, so a runner that stops
    allowing user namespaces turns the build red instead of silently skipping them."""
    if os.environ.get("TTHFV_REQUIRE_BWRAP") == "1":
        assert sandbox.available(), sandbox.unavailable_reason()
