# SPDX-License-Identifier: Apache-2.0
"""The parallel runner, with fake stage operations in real worker processes (forkserver)."""

import re

import pytest
from fake_ops import FakeOps

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
    assert runner.clamp_jobs(200) == 80
    assert runner.clamp_jobs(40) == 40
    assert runner.clamp_jobs(0) == 1
    assert runner.DEFAULT_JOBS == 40


def test_jobs_200_runs_with_80_workers_at_most(work, state):
    s = runner.run("build", IDS, jobs=200, ops=FakeOps(state))
    assert s.workers == 3  # never more workers than designs, and never more than 80
    assert runner.clamp_jobs(200) == 80


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
