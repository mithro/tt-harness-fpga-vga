# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from another test module
"""A kill by the free-space rule is not proof against the design (the rule watches the whole filesystem), so it is
recorded as "disk-free" and never reused; a kill by the scratch rules stays "disk" and is cached like any result.
Also: a free-space check that cannot run says so and kills nothing."""

import json
import time
from pathlib import Path

from test_build import NEXTPNR, fake_tool, make_design, make_fetched, rec, run, toolchain  # noqa: F401

from tthfv import paths, proc, safefile

GIB = 1 << 30


def yosys_runs(rec: Path) -> int:
    f = rec / "yosys.count"
    return len(f.read_text().split()) if f.exists() else 0


def slow_nextpnr(toolchain, extra=""):
    """A nextpnr that announces itself with a marker file, then sleeps (so the watchdog ticks a few times)."""
    text = NEXTPNR.replace(
        '[ -n "$FAKE_PNR_SLEEP" ] && sleep "$FAKE_PNR_SLEEP"',
        'touch "$FAKE_REC/pnr.started"\n' + extra + '[ -n "$FAKE_PNR_SLEEP" ] && sleep "$FAKE_PNR_SLEEP"',
    )
    assert text != NEXTPNR
    fake_tool(toolchain, "nextpnr-ice40", text)


def test_a_free_space_kill_is_disk_free_transient_and_rebuilt_on_the_next_run(
    toolchain, rec, tmp_path, monkeypatch
):
    slow_nextpnr(toolchain)
    monkeypatch.setenv("FAKE_PNR_SLEEP", "6")
    marker = rec / "pnr.started"

    def free(path):
        # 100 GiB until a second after nextpnr has started, then 5 GiB less: another process filling the disk
        if marker.exists() and time.time() - marker.stat().st_mtime > 1.2:
            return 95 * GIB
        return 100 * GIB

    monkeypatch.setattr(proc, "free_bytes", free)
    monkeypatch.setattr(proc._Watchdog, "FREE_MARGIN", 100 << 20)
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100 << 20)
    d, r = run(tmp_path, max_attempts=1)
    assert r.status == "pnr-fail" and r.limited == "disk-free" and r.transient is True, r
    assert "free space on the work filesystem fell by" in r.reason and "the result is not cached" in r.reason
    assert "other processes writing to the same filesystem" in r.reason
    assert json.loads((paths.design_dir("build", d.id) / "build.json").read_text())["limited"] == "disk-free"
    before = yosys_runs(rec)
    monkeypatch.setattr(proc, "free_bytes", lambda path: 100 * GIB)  # the disk is quiet again
    monkeypatch.delenv("FAKE_PNR_SLEEP")
    again = run(tmp_path, max_attempts=1)[1]
    assert again.status == "ok" and again.limited is None
    assert yosys_runs(rec) > before, "the free-space kill was served from the cache"


def test_a_scratch_cap_kill_stays_disk_and_is_cached(toolchain, rec, tmp_path, monkeypatch):
    slow_nextpnr(
        toolchain, extra="head -c 20000000 /dev/zero > big\n"
    )  # the cwd of nextpnr is the scratch directory
    monkeypatch.setenv("FAKE_PNR_SLEEP", "6")
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 5 << 20)
    monkeypatch.setattr(proc, "free_bytes", lambda path: 100 * GIB)
    d, r = run(tmp_path, max_attempts=1)
    assert r.status == "pnr-fail" and r.limited == "disk" and r.transient is False, r
    assert "scratch limit (5 MiB) exceeded during the run" in r.reason
    before = yosys_runs(rec)
    again = run(tmp_path, max_attempts=1)[1]
    assert again.limited == "disk" and yosys_runs(rec) == before, (
        "a scratch-cap kill is a verdict on the design: cached"
    )
    assert d.id == "tt99/tt_um_x"


def test_a_free_space_check_that_cannot_run_is_recorded_and_kills_nothing(
    toolchain, rec, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(proc, "free_bytes", lambda path: None)
    monkeypatch.setenv("FAKE_PNR_SLEEP", "2")
    slow_nextpnr(toolchain)
    d, r = run(tmp_path, max_attempts=1)
    assert r.status == "ok", r
    assert r.free_space_rule.startswith("off: statvfs failed")
    assert (
        "WARNING: the free-space check of the disk watchdog is off: statvfs failed" in capsys.readouterr().err
    )
    assert json.loads((paths.design_dir("build", d.id) / "build.json").read_text())[
        "free_space_rule"
    ].startswith("off")


def test_the_free_space_check_failing_later_is_recorded_too(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(proc._Watchdog, "TICK", 0.05)
    values = iter([100 * GIB, None, None, None, None, None, None, None, None, None])
    monkeypatch.setattr(proc, "free_bytes", lambda path: next(values, None))
    r = proc.run_logged(
        ["sleep", "1"], 30, tmp_path / "x.log", cwd=tmp_path, scratch_check=lambda: "", scratch_dir=tmp_path
    )
    assert r.limited is None and r.rc == 0
    assert r.free_space_rule == "off: statvfs failed during the run"
    assert capsys.readouterr().err.count("WARNING: the free-space check") == 1  # once, not every tick


def test_results_mark_the_limited_statuses():
    from tthfv import results

    for kind in ("memory", "tasks", "disk", "disk-free", "unknown"):
        assert results._status_cell({"status": "pnr-fail", "limited": kind}) == f"pnr-fail ({kind} limit)"
    assert results._status_cell({"status": "ok"}) == "ok"
