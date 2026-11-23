# SPDX-License-Identifier: Apache-2.0
"""tthfv.proc: timeouts kill the whole process group; logged output is bounded."""

import os
import time
from pathlib import Path

import pytest

from tthfv import proc

# A shell that starts a grandchild, records both pids and waits.
SPAWNER = 'sleep 60 & echo $! > "$1"; echo $$ > "$2"; wait'


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is dead for our purposes
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False
    return True


def gone(pid: int, wait: float = 3.0) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def pids(tmp_path: Path) -> tuple[int, int]:
    return int((tmp_path / "child").read_text()), int((tmp_path / "parent").read_text())


def spawner(tmp_path: Path) -> list[str]:
    return ["sh", "-c", SPAWNER, "sh", str(tmp_path / "child"), str(tmp_path / "parent")]


def test_run_returns_the_exit_code():
    assert proc.run(["sh", "-c", "exit 7"], timeout=10) == 7


def test_run_timeout_kills_the_child_and_its_grandchild(tmp_path):
    started = time.monotonic()
    assert proc.run(spawner(tmp_path), timeout=1) == proc.TIMEOUT_RC
    assert time.monotonic() - started < 10
    child, parent = pids(tmp_path)
    assert gone(child) and gone(parent)


def test_capture_returns_output_and_kills_the_group_on_timeout(tmp_path):
    ok = proc.capture(["sh", "-c", "echo out; echo err >&2; exit 3"], timeout=10)
    assert ok.rc == 3 and "out" in ok.text and "err" in ok.text
    slow = proc.capture([*spawner(tmp_path)], timeout=1)
    assert slow.rc == proc.TIMEOUT_RC
    assert gone(pids(tmp_path)[0])


def test_run_logged_writes_a_header_and_the_output(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("old\n")
    r = proc.run_logged(["sh", "-c", "echo hello"], timeout=10, log=log)
    assert r.rc == 0
    assert log.read_text() == "old\n$ sh -c echo hello\nhello\n"
    assert r.text == "hello\n"


def test_run_logged_timeout_kills_the_group_and_says_so(tmp_path):
    log = tmp_path / "x.log"
    r = proc.run_logged(spawner(tmp_path), timeout=1, log=log)
    assert r.rc == proc.TIMEOUT_RC
    assert "timed out after 1s" in log.read_text()
    child, parent = pids(tmp_path)
    assert gone(child) and gone(parent)


def test_a_grandchild_that_outlives_its_parent_does_not_hang_the_run(tmp_path):
    log = tmp_path / "x.log"
    started = time.monotonic()
    r = proc.run_logged(["sh", "-c", "sleep 60 & echo $! > " + str(tmp_path / "gc"), "sh"], timeout=30, log=log)
    assert r.rc == 0
    assert time.monotonic() - started < 20
    assert gone(int((tmp_path / "gc").read_text()))


def test_output_beyond_the_cap_is_truncated_with_a_marker_but_the_end_is_kept(tmp_path):
    log = tmp_path / "sim.log"
    code = "i=0; while [ $i -lt 40000 ]; do echo 0123456789012345678901234567890123456789012345678901234567890123; i=$((i+1)); done; echo THE-LAST-LINE"
    r = proc.run_logged(["sh", "-c", code], timeout=60, log=log, cap=proc.LOG_CAP)
    size = log.stat().st_size
    assert 40000 * 65 > 2 * proc.LOG_CAP  # the output really was far over the cap
    assert size < proc.LOG_CAP + proc.LOG_TAIL + 200
    text = log.read_text()
    assert "[output truncated:" in text
    assert text.rstrip().endswith("THE-LAST-LINE")
    assert r.text == text.split("\n", 1)[1]  # the header line aside, what was written is what is returned


def test_uncapped_output_is_kept_whole(tmp_path):
    log = tmp_path / "x.log"
    r = proc.run_logged(["sh", "-c", "head -c 3000000 /dev/zero | tr '\\0' a"], timeout=60, log=log, header=False)
    assert log.stat().st_size == 3_000_000 and len(r.text) == 3_000_000


def test_a_missing_program_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        proc.run(["/nonexistent/tool"], timeout=5)
