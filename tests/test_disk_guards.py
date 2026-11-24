# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""Free-space refusals and the memory-share check at any number of jobs."""

import pytest
from fake_ops import FakeOps
from test_build import make_design, make_fetched, rec, toolchain  # noqa: F401

from tthfv import build, paths, runner
from tthfv.cli import main
from tthfv.errors import UserError

GIB = 1 << 30


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return tmp_path / "work"


def machine(monkeypatch, gib=64):
    monkeypatch.setattr(runner, "_total_memory", lambda: gib << 30)


def with_limits(monkeypatch, memory=None):
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.setenv("TTHFV_LIMITS", "scope")
    monkeypatch.delenv("TTHFV_LIMIT_MEMORY", raising=False)
    if memory:
        monkeypatch.setenv("TTHFV_LIMIT_MEMORY", memory)


@pytest.mark.parametrize("jobs,memory", [(4, "200G"), (1, "2T"), (2, "70G")])
def test_a_memory_limit_that_is_too_big_is_refused_at_any_number_of_jobs(monkeypatch, jobs, memory):
    with_limits(monkeypatch, memory)
    machine(monkeypatch, gib=256)
    with pytest.raises(UserError, match="more than 50% of the memory"):
        runner.commitment(jobs)
    assert (
        runner.commitment(jobs, force_jobs=True) == ""
    )  # forced, and not above the default: nothing to print


def test_the_default_limit_at_the_default_jobs_is_fine_on_a_big_enough_machine(monkeypatch):
    with_limits(monkeypatch)
    machine(monkeypatch, gib=64)
    assert runner.commitment(4) == ""  # 4 x 8G of 64G is exactly half
    machine(monkeypatch, gib=32)
    with pytest.raises(UserError):
        runner.commitment(4)


@pytest.mark.parametrize("jobs", ["0", "-3"])
def test_jobs_below_one_is_a_user_error_not_clamped(work, monkeypatch, capsys, jobs):
    monkeypatch.setattr(runner.manifest, "load", lambda **kw: [])
    monkeypatch.setattr(runner, "select", lambda designs, only, candidates: ["tt99/tt_um_a"])
    assert main(["run", "build", "--jobs", jobs]) == 1
    assert "--jobs must be at least 1" in capsys.readouterr().err
    assert not list(work.glob("run-*.log")) if work.exists() else True


def test_too_little_free_space_is_every_designs_error_and_nothing_is_started(work, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(paths, "free_bytes", lambda p: 5 * GIB)  # 2 jobs x 2 GiB + 10 GiB are needed
    ids = ["tt99/tt_um_a", "tt99/tt_um_b", "tt99/tt_um_c"]
    s = runner.run("build", ids, jobs=2, ops=FakeOps(state))
    assert s.counts["build"] == {"error": 3} and s.finished == 3
    log = s.log.read_text()
    assert log.count("not enough free disk space") == 3 and "2 jobs x the 2 GiB scratch cap + 10 GiB" in log
    assert not (state / "calls.log").exists()


def test_enough_free_space_lets_the_run_go_on(work, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(paths, "free_bytes", lambda p: 14 * GIB)
    s = runner.run("build", ["tt99/tt_um_a"], jobs=2, ops=FakeOps(state))
    assert s.counts["build"] == {"ok": 1}


def test_a_stage_refuses_to_start_below_the_free_space_floor(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "free_bytes", lambda p: 19 * GIB)
    monkeypatch.delenv("TTHFV_MIN_FREE", raising=False)
    d = make_design()
    with pytest.raises(UserError, match="not enough free disk space.*TTHFV_MIN_FREE"):
        build.build(d, make_fetched(tmp_path, design=d))
    monkeypatch.setenv("TTHFV_MIN_FREE", "10G")
    assert build.build(d, make_fetched(tmp_path, design=d)).status == "ok"
    assert not (rec / "never").exists()


def test_min_free_parsing(monkeypatch):
    monkeypatch.delenv("TTHFV_MIN_FREE", raising=False)
    assert paths.min_free() == 20 * GIB
    monkeypatch.setenv("TTHFV_MIN_FREE", "512M")
    assert paths.min_free() == 512 << 20
    monkeypatch.setenv("TTHFV_MIN_FREE", "lots")
    with pytest.raises(UserError, match="TTHFV_MIN_FREE"):
        paths.min_free()
