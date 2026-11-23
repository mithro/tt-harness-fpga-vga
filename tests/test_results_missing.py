# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""A stage whose record file is missing is never `ok`; outcome entries are compared per stage by time."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from test_results import put, work  # noqa: F401 - fixtures
from test_results_stale import ID, collect, full  # noqa: F401 - fixtures

from tthfv import results

MISSING = "record missing (interrupted run?)"


def stamp(seconds_from_now: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds_from_now)).isoformat()


def age(path, seconds_ago):
    t = path.stat().st_mtime - seconds_ago
    os.utime(path, (t, t))


def drop_records(work):
    (work / "src" / ID / "fetch.json").unlink()
    (work / "build" / ID / "build.json").unlink()
    (work / "reference" / ID / "reference.json").unlink()


def test_missing_records_with_an_ok_outcome_are_not_ok(full):
    drop_records(full)
    ok = {"status": "ok", "reason": "", "finished": stamp(-100)}
    put(full, "run", ID, "outcome.json", {"fetch": ok, "build": ok, "reference": ok})
    rec = collect(full)
    for stage in ("fetch", "build", "reference"):
        assert (rec[stage]["status"], rec[stage]["reason"]) == ("not-run", MISSING), stage
    assert rec["build"]["lut_percent"] is None and rec["reference"]["hash"] is None
    md = results.render_markdown([rec])
    assert "build: 1 not-run" in md and "reference: 1 not-run" in md and "fetch: 1 not-run" in md
    assert "1 ok" not in md


def test_missing_records_with_an_old_style_ok_outcome_are_not_ok(full):
    drop_records(full)
    ok = {"status": "ok", "reason": ""}
    put(full, "run", ID, "outcome.json", {"fetch": ok, "build": ok, "reference": ok})
    rec = collect(full)
    assert [rec[s]["status"] for s in ("fetch", "build", "reference")] == ["not-run"] * 3


@pytest.mark.parametrize("stage", ["fetch", "build", "reference"])
def test_a_missing_record_with_an_error_outcome_shows_the_error(full, stage):
    drop_records(full)
    put(full, "run", ID, "outcome.json", {stage: {"status": "error", "reason": "boom", "finished": stamp(0)}})
    rec = collect(full)
    assert (rec[stage]["status"], rec[stage]["reason"]) == ("error", "boom")


def test_an_old_build_error_does_not_beat_a_newer_ok_record_after_a_fetch_only_rewrite(full):
    # The build error is older than build.json; a later `run fetch` rewrites outcome.json (new file
    # mtime) but leaves the build entry, and its time, alone.
    put(
        full, "run", ID, "outcome.json",
        {
            "fetch": {"status": "ok", "reason": "", "finished": stamp(0)},
            "build": {"status": "error", "reason": "old", "finished": stamp(-1000)},
        },
    )  # fmt: skip
    age(full / "build" / ID / "build.json", 100)
    assert collect(full)["build"]["status"] == "ok"


def test_an_older_ok_record_loses_to_a_newer_outcome_error(full):
    put(
        full, "run", ID, "outcome.json", {"build": {"status": "error", "reason": "new", "finished": stamp(0)}}
    )
    age(full / "build" / ID / "build.json", 100)
    rec = collect(full)
    assert (rec["build"]["status"], rec["build"]["reason"]) == ("error", "new")


def test_an_outcome_entry_without_a_time_is_older_than_any_record(full):
    put(full, "run", ID, "outcome.json", {"build": {"status": "error", "reason": "old file"}})
    assert collect(full)["build"]["status"] == "ok"


def test_the_runner_stamps_each_stage_and_keeps_the_stamp_of_untouched_stages(tmp_path):
    from tthfv import runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runner._write_outcomes(run_dir, {"build": runner.Outcome("error", "x")})
    first = json.loads((run_dir / "outcome.json").read_text())["build"]["finished"]
    runner._write_outcomes(run_dir, {"fetch": runner.Outcome("ok", "")})
    after = json.loads((run_dir / "outcome.json").read_text())
    assert after["build"]["finished"] == first
    assert datetime.fromisoformat(after["fetch"]["finished"]).tzinfo is not None
