# SPDX-License-Identifier: Apache-2.0
"""The parallel runner, with fake stage operations in real worker processes (forkserver)."""

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import fake_ops
import pytest
from fake_ops import FakeOps, KillOps

from tthfv import manifest, paths, runner
from tthfv.cli import main
from tthfv.errors import UserError

LINE = re.compile(r"^\d+/\d+ \S+ \S+ \d+(\.\d+)?s eta \d\d:\d\d$")
IDS = ["tt99/tt_um_a", "tt99/tt_um_b", "tt99/tt_um_c"]


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return tmp_path / "work"


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "state"
    s.mkdir()
    return s


def calls(state):
    p = state / "calls.log"
    return p.read_text().splitlines() if p.exists() else []


def log_lines(summary):
    return summary.log.read_text().splitlines()


def test_jobs_are_clamped():
    assert runner.clamp_jobs(200) == 16
    assert runner.clamp_jobs(16) == 16
    assert runner.clamp_jobs(0) == 1
    assert runner.DEFAULT_JOBS == 4 and runner.MAX_JOBS == 16


def test_jobs_200_runs_with_16_workers_at_most(work, state):
    s = runner.run("build", IDS, jobs=200, ops=FakeOps(state))
    assert s.workers == 3  # never more workers than designs, and never more than 16
    assert runner.clamp_jobs(200) == 16


def test_the_command_line_refuses_more_than_the_maximum_jobs(work, monkeypatch, capsys):
    monkeypatch.setattr(runner.manifest, "load", lambda **kw: [])
    monkeypatch.setattr(runner, "select", lambda designs, only, candidates: ["tt99/tt_um_a"])
    assert main(["run", "build", "--jobs", "17"]) == 1
    assert "at most 16" in capsys.readouterr().err


def machine(monkeypatch, gib=64, cpus=88):
    monkeypatch.setattr(runner, "_total_memory", lambda: gib << 30)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: cpus)


def test_up_to_the_default_jobs_nothing_is_printed(monkeypatch):
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.setenv("TTHFV_LIMITS", "scope")
    machine(monkeypatch)
    assert runner.commitment(4) == ""


def test_more_jobs_print_what_they_could_commit(monkeypatch):
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.setenv("TTHFV_LIMITS", "scope")
    monkeypatch.delenv("TTHFV_LIMIT_MEMORY", raising=False)
    machine(monkeypatch, gib=128, cpus=88)
    line = runner.commitment(6)
    assert "6 jobs x 8G memory = up to 48 GiB of the machine's 128 GiB" in line
    assert "6 x 400% CPU = up to 24 of its 88 cores" in line


def test_jobs_beyond_half_the_memory_are_refused_unless_forced(monkeypatch):
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.setenv("TTHFV_LIMITS", "scope")
    machine(monkeypatch, gib=64)
    with pytest.raises(UserError, match="more than 50% of the memory.*--force-jobs"):
        runner.commitment(5)  # 5 x 8G = 40G of 64G
    assert runner.commitment(4 + 0) == ""
    machine(monkeypatch, gib=512)
    assert "5 jobs" in runner.commitment(5)
    machine(monkeypatch, gib=64)
    assert "5 jobs" in runner.commitment(5, force_jobs=True)


def test_without_limits_more_jobs_say_nothing_bounds_them(monkeypatch):
    monkeypatch.setenv("TTHFV_LIMITS", "none")
    machine(monkeypatch)
    assert "no resource limits" in runner.commitment(8)


def test_progress_lines_and_summary(work, state, capsys):
    s = runner.run("all", IDS, jobs=2, ops=FakeOps(state))
    lines = log_lines(s)
    assert len(lines) == len(IDS) + 1
    for n, line in enumerate(lines[:-1], 1):
        assert LINE.match(line), line
        assert line.startswith(f"{n}/3 ")
        assert " ok,ok,ok " in line
    assert re.fullmatch(
        r"done 3 designs in \d+(\.\d+)?s: fetch 3 ok; build 3 ok; reference 3 ok", lines[-1]
    ), lines[-1]
    assert s.log.name.startswith("run-all-") and s.log.parent == work
    assert re.fullmatch(r"run-all-\d{8}T\d{6}Z\.log", s.log.name)
    assert capsys.readouterr().out.splitlines() == lines  # the same lines on stdout


def test_single_stage_status_is_just_that_stage(work, state):
    s = runner.run("build", IDS[:1], jobs=1, ops=FakeOps(state))
    lines = log_lines(s)
    assert re.match(r"^1/1 tt99/tt_um_a ok \d", lines[0])
    assert re.fullmatch(r"done 1 designs in \d+(\.\d+)?s: 1 ok", lines[-1])
    assert [c.split()[0] for c in calls(state)] == ["fetch", "build"]  # build needs the fetch


def test_an_exception_is_an_error_with_a_traceback_and_the_run_continues(work, state):
    ids = ["tt99/tt_um_a", "tt99/tt_um_boom", "tt99/tt_um_c"]
    s = runner.run("all", ids, jobs=2, ops=FakeOps(state))
    assert s.counts["build"] == {"ok": 2, "error": 1}
    assert s.counts["reference"] == {"ok": 3}  # the reference does not depend on the build
    boom = next(line for line in log_lines(s) if "tt_um_boom" in line)
    assert " ok,error,ok " in boom
    tb = (work / "run" / "tt99" / "tt_um_boom" / "error.log").read_text()
    assert "RuntimeError: synthetic failure" in tb and "Traceback" in tb
    assert "1 error" in log_lines(s)[-1]
    assert not (work / "run" / "tt99" / "tt_um_a" / "error.log").exists()


def test_a_fetch_failure_means_not_run_for_the_other_stages(work, state):
    s = runner.run("all", ["tt99/tt_um_fetchfail"], jobs=1, ops=FakeOps(state))
    assert " fetch-fail,not-run,not-run " in log_lines(s)[0]
    assert s.counts == {"fetch": {"fetch-fail": 1}, "build": {"not-run": 1}, "reference": {"not-run": 1}}
    assert [c.split()[0] for c in calls(state)] == ["fetch"]
    outcome = json.loads((work / "run" / "tt99" / "tt_um_fetchfail" / "outcome.json").read_text())
    assert outcome["fetch"] == {
        "status": "fetch-fail",
        "reason": "no network",
        "finished": outcome["fetch"]["finished"],
    }
    assert outcome["build"]["status"] == "not-run"


def test_outcome_json_merges_stages_across_runs(work, state):
    ops = FakeOps(state)
    runner.run("fetch", IDS[:1], jobs=1, ops=ops)
    runner.run("reference", IDS[:1], jobs=1, ops=ops)
    outcome = json.loads((work / "run" / "tt99" / "tt_um_a" / "outcome.json").read_text())
    assert set(outcome) == {"fetch", "reference"}
    for stage, entry in outcome.items():
        assert entry == {"status": "ok", "reason": "", "finished": entry["finished"]}, stage
        assert datetime.fromisoformat(entry["finished"]).tzinfo is not None


def test_a_stages_of_one_design_never_overlap_and_run_in_order(work, state):
    runner.run("all", IDS, jobs=3, ops=FakeOps(state))
    for i in IDS:
        stages = [c.split()[0] for c in calls(state) if c.split()[1] == i]
        assert stages == ["fetch", "build", "reference"]
        pids = {c.split()[3] for c in calls(state) if c.split()[1] == i}
        assert len(pids) == 1  # one worker handles one design end to end


def test_finished_designs_are_skipped_and_force_does_them_again(work, state):
    ops = FakeOps(state)
    runner.run("build", IDS, jobs=2, ops=ops)
    first = len(calls(state))
    assert first == 6  # fetch and build for three designs
    s = runner.run("build", IDS, jobs=2, ops=ops)
    assert len(calls(state)) == first
    assert s.counts == {"build": {"ok": 3}}
    runner.run("build", IDS[:1], jobs=1, force=True, ops=ops)  # --force applies to the named stage only
    assert [c.split()[:3] for c in calls(state)[first:]] == [
        ["build", IDS[0], "force=True"],
    ]


def test_select_applies_only_and_candidates():
    def d(id, cand):
        return manifest.Design(
            id=id, shuttle="s", macro="m", title="", author="", repo="", commit="", top_module="m",
            source_files=(), clock_hz=1, clock_declared=True, pinout={}, pmods=(), tiles="1x1",
            render_verdict=None, override={}, patch=None, qspi=None, ui_in=0, uio_in=0,
            scripted=False, candidate=cand,
        )  # fmt: skip

    designs = [d("tt1/a", True), d("tt1/b", False), d("tt1/c", True)]
    assert runner.select(designs, [], False) == ["tt1/a", "tt1/b", "tt1/c"]
    assert runner.select(designs, [], True) == ["tt1/a", "tt1/c"]
    assert runner.select(designs, ["tt1/c", "tt1/b"], False) == ["tt1/b", "tt1/c"]
    assert runner.select(designs, ["tt1/b"], True) == []
    with pytest.raises(UserError, match="no design 'tt1/zzz'"):
        runner.select(designs, ["tt1/zzz"], False)


def test_cli_unknown_id_is_a_user_error_before_any_work(work, monkeypatch, capsys):
    monkeypatch.setattr(runner.manifest, "load", lambda **kw: [])
    assert main(["run", "all", "--only", "tt1/nope"]) == 1
    assert "no design" in capsys.readouterr().err
    assert not list(work.glob("run-*.log"))


def test_cli_exit_code_is_zero_even_when_designs_failed(work, state, monkeypatch):
    monkeypatch.setattr(runner, "REAL_OPS", FakeOps(state))
    monkeypatch.setattr(runner.manifest, "load", lambda **kw: [])
    monkeypatch.setattr(runner, "select", lambda designs, only, candidates: ["tt99/tt_um_boom"])
    assert main(["run", "build", "--jobs", "1"]) == 0
    assert "1 error" in next(work.glob("run-build-*.log")).read_text()


def test_a_killed_worker_does_not_take_the_other_designs_with_it(work, state):
    ids = [f"tt99/tt_um_d{n:02d}" for n in range(12)] + ["tt99/tt_um_a_killme"]
    s = runner.run("all", ids, jobs=4, ops=KillOps(state))
    assert s.counts["build"] == {"ok": 12, "error": 1}
    assert s.counts["reference"] == {"ok": 12, "error": 1}
    for i in ids[:-1]:
        built = [c for c in calls(state) if c.startswith(f"build {i} ")]
        assert len(built) == 1, (i, built)
    killed = "tt99/tt_um_a_killme"
    assert any(f" {killed} " in line and "error" in line for line in log_lines(s))
    run_dir = work / "run" / "tt99" / "tt_um_a_killme"
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["build"] == {
        "status": "error",
        "reason": "worker died",
        "finished": outcome["build"]["finished"],
    }
    assert "worker died" in (run_dir / "error.log").read_text()
    assert "1 error" in log_lines(s)[-1]
    assert len([line for line in log_lines(s) if LINE.match(line)]) == len(ids)


def test_ctrl_c_stops_promptly_with_a_summary_and_leaves_nothing_running(work, state):
    env = dict(os.environ, TTHFV_WORK=str(work))
    driver = Path(__file__).parent / "interrupt_driver.py"
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(state)], env=env, stdout=subprocess.PIPE, text=True
    )
    deadline = time.monotonic() + 30
    while not list(work.glob("run/tt99/*/inflight")) and time.monotonic() < deadline:
        time.sleep(0.1)  # wait until workers hold designs
    time.sleep(0.5)
    sent = time.monotonic()
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=4)
    assert proc.returncode == 130
    assert time.monotonic() - sent < 4
    assert re.search(r"^interrupted after 0/4 designs", out, re.MULTILINE), out
    time.sleep(fake_ops.SlowOps.SECONDS + 1)  # long enough for survivors to write their markers
    assert not (state / "worker-survived").exists()
    assert not (state / "grandchild").exists()
    assert not list(work.glob("run/tt99/*/inflight"))
    assert not list(work.glob("run/tt99/*_late*/outcome.json"))  # never started: untouched
    assert re.search(r"interrupted after 0/4", next(work.glob("run-build-*.log")).read_text())


def test_two_runs_in_the_same_second_do_not_share_a_log(work, state):
    a = runner.run("fetch", IDS[:1], jobs=1, ops=FakeOps(state))
    b = runner.run("fetch", IDS[:1], jobs=1, ops=FakeOps(state))
    assert a.log != b.log and a.log.exists() and b.log.exists()
    assert len(a.log.read_text().splitlines()) == 2  # progress line and summary, not the other run's


def test_eta_ignores_designs_that_were_skipped_in_no_time(monkeypatch):
    monkeypatch.setattr(runner.time, "time", lambda: 1_000_000.0)
    hhmm = lambda t: time.strftime("%H:%M", time.localtime(t))
    # 90 designs skipped at 0s, 10 timed ones averaging 100s; 100 left, 10 workers: 1000s to go
    assert runner._eta(left=100, timed=10, spent=1000.0, workers=10) == hhmm(1_000_000 + 1000)
    assert runner._eta(left=100, timed=0, spent=0.0, workers=10) == hhmm(1_000_000)  # nothing to go by


def test_a_stale_status_is_replaced_when_a_worker_dies(work, state):
    run_dir = work / "run" / "tt99" / "tt_um_killme"
    run_dir.mkdir(parents=True)
    (run_dir / "outcome.json").write_text(json.dumps({"build": {"status": "ok", "reason": ""}}))
    runner.run("build", ["tt99/tt_um_killme", "tt99/tt_um_b"], jobs=2, ops=KillOps(state))
    assert json.loads((run_dir / "outcome.json").read_text())["build"]["status"] == "error"
