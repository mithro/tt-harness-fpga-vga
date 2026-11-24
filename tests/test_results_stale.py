# SPDX-License-Identifier: Apache-2.0
"""A record that does not match the current manifest, fetch and tools is `stale`; the latest run wins."""

import json
import os
from datetime import UTC, datetime

import pytest
from test_results import design, put, work  # noqa: F401 - fixtures

from tthfv import paths, results
from tthfv.cli import main

ID = "tt01/tt_um_b"
COMMIT = "c" * 40
SHA = "5" * 64
TOOLS = {"yosys": "Yosys 1", "nextpnr-ice40": "nextpnr 1", "icepack": "icepack 1", "verilator": "Verilator 1"}

FETCH = {"commit": COMMIT, "sources_sha256": SHA, "top_module_used": "tt_um_b", "top_module_fallback": False}
BUILD = {
    "id": ID, "status": "ok", "reason": "", "bitstream_sha256": "ab" * 32, "clock_hz": 25_175_000,
    "fmax_hz": 31_250_000, "lut_percent": 12.5, "recipe": {"synth": "default", "seed": 2},
    "attempts": [{}, {}], "commit": COMMIT, "sources_sha256": SHA, "top_module_used": "tt_um_b",
    "tools": {"yosys": "Yosys 1", "nextpnr-ice40": "nextpnr 1", "icepack": "icepack 1"},
}  # fmt: skip
REF = {
    "mode": "640x480@60", "width": 640, "height": 480, "frames_complete": 5,
    "frames": [{"hash": "00ff"}], "tools": {"verilator": "Verilator 1"},
    "key": {"commit": COMMIT, "sources_sha256": SHA, "verilator": "Verilator 1", "top_module_used": "tt_um_b"},
}  # fmt: skip


@pytest.fixture
def full(work):  # noqa: F811
    put(work, "src", ID, "fetch.json", FETCH)
    put(work, "build", ID, "build.json", BUILD)
    put(work, "reference", ID, "reference.json", REF)
    return work


def collect(work, tools=TOOLS, **kw):  # noqa: F811
    (rec,) = results.collect([design(ID, commit=COMMIT, **kw)], work, tools)
    return rec


def test_matching_records_are_not_stale(full):
    rec = collect(full)
    assert (rec["fetch"]["status"], rec["build"]["status"], rec["reference"]["status"]) == ("ok",) * 3


def test_a_new_manifest_commit_makes_all_three_stale(full):
    (rec,) = results.collect([design(ID, commit="d" * 40)], full, TOOLS)
    for stage in ("fetch", "build", "reference"):
        assert rec[stage]["status"] == "stale", stage
        assert "commit" in rec[stage]["reason"], stage
    assert rec["build"]["lut_percent"] is None and rec["build"]["bitstream_sha256"] is None
    assert rec["reference"]["hash"] is None


def test_a_changed_source_digest_makes_build_and_reference_stale(full):
    put(full, "src", ID, "fetch.json", {**FETCH, "sources_sha256": "6" * 64})
    rec = collect(full)
    assert rec["fetch"]["status"] == "ok"
    for stage in ("build", "reference"):
        assert rec[stage]["status"] == "stale"
        assert "sources_sha256" in rec[stage]["reason"]


def test_without_a_fetch_the_digest_is_not_compared(full):
    (full / "src" / ID / "fetch.json").unlink()
    rec = collect(full)
    assert rec["build"]["status"] == "ok" and rec["reference"]["status"] == "ok"


def test_a_tool_version_change_makes_build_stale(full):
    rec = collect(full, tools={**TOOLS, "yosys": "Yosys 2"})
    assert rec["build"]["status"] == "stale" and "yosys" in rec["build"]["reason"]
    assert rec["reference"]["status"] == "ok"


def test_a_verilator_change_makes_reference_stale(full):
    rec = collect(full, tools={**TOOLS, "verilator": "Verilator 2"})
    assert rec["reference"]["status"] == "stale" and "verilator" in rec["reference"]["reason"]
    assert rec["build"]["status"] == "ok"


def test_without_current_tool_versions_tools_are_not_compared(full):
    assert collect(full, tools=None)["build"]["status"] == "ok"


def test_records_from_before_the_fix_are_stale(full):
    old = {k: v for k, v in BUILD.items() if k not in ("commit", "sources_sha256")}
    put(full, "build", ID, "build.json", old)
    put(full, "reference", ID, "reference.json", {k: v for k, v in REF.items() if k != "key"})
    rec = collect(full)
    assert rec["build"]["status"] == "stale" and "commit" in rec["build"]["reason"]
    assert rec["reference"]["status"] == "stale" and "key" in rec["reference"]["reason"]


def test_stale_is_shown_in_the_table_and_counted(full):
    other = design("tt01/tt_um_a", commit=COMMIT)
    recs = results.collect([other, design(ID, commit="d" * 40)], full, TOOLS)
    md = results.render_markdown(recs)
    assert "build: 1 stale, 1 not-run" in md or "build: 1 not-run, 1 stale" in md
    assert "reference: 1 stale, 1 not-run" in md or "reference: 1 not-run, 1 stale" in md
    row = next(r for r in md.splitlines() if r.startswith(f"| {ID}"))
    assert " stale " in row


def NOW() -> str:
    return datetime.now(UTC).isoformat()


def set_age(path, seconds_ago):
    t = path.stat().st_mtime - seconds_ago
    os.utime(path, (t, t))


@pytest.mark.parametrize("status", ["error", "not-run"])
def test_a_later_failed_build_attempt_wins_over_an_older_ok(full, status):
    put(
        full,
        "run",
        ID,
        "outcome.json",
        {"build": {"status": status, "reason": "worker died", "finished": NOW()}},
    )
    set_age(full / "build" / ID / "build.json", 100)
    rec = collect(full)
    assert (rec["build"]["status"], rec["build"]["reason"]) == (status, "worker died")
    assert rec["build"]["lut_percent"] is None and rec["build"]["bitstream_sha256"] is None


def test_an_older_failed_attempt_does_not_hide_a_newer_build(full):
    put(full, "run", ID, "outcome.json", {"build": {"status": "error", "reason": "boom"}})
    set_age(full / "run" / ID / "outcome.json", 100)
    assert collect(full)["build"]["status"] == "ok"


@pytest.mark.parametrize("status", ["error", "not-run"])
def test_a_later_failed_reference_attempt_wins_over_an_older_ok(full, status):
    put(
        full,
        "run",
        ID,
        "outcome.json",
        {"reference": {"status": status, "reason": "fetch failed", "finished": NOW()}},
    )
    set_age(full / "reference" / ID / "ideal" / "reference.json", 100)
    rec = collect(full)
    assert (rec["reference"]["status"], rec["reference"]["reason"]) == (status, "fetch failed")
    assert rec["reference"]["hash"] is None


def test_an_ok_outcome_does_not_override_the_record(full):
    put(full, "run", ID, "outcome.json", {"build": {"status": "ok", "reason": ""}})
    set_age(full / "build" / ID / "build.json", 100)
    assert collect(full)["build"]["lut_percent"] == 12.5


def test_index_json_carries_tools_commit_and_per_design_identity(full, tmp_path, monkeypatch):
    monkeypatch.setattr(results.manifest, "load", lambda **kw: [design(ID, commit=COMMIT)])
    monkeypatch.setattr(results.tools, "versions", lambda: dict(TOOLS))
    monkeypatch.setattr(results, "_videos_repo_commit", lambda: "f" * 40)
    assert main(["results", "--out-md", str(tmp_path / "r.md")]) == 0
    index = json.loads((paths.WORK / "results" / "index.json").read_text())
    assert index["tools"] == TOOLS and index["videos_repo_commit"] == "f" * 40
    (d,) = index["designs"]
    assert (d["commit"], d["sources_sha256"], d["top_module_used"]) == (COMMIT, SHA, "tt_um_b")
    assert d["build"]["status"] == "ok"


def test_index_without_a_toolchain_still_works(full, tmp_path, monkeypatch):
    monkeypatch.setattr(results.manifest, "load", lambda **kw: [design(ID, commit=COMMIT)])

    def missing():
        raise results.tools.ToolMissing("not installed")

    monkeypatch.setattr(results.tools, "versions", missing)
    monkeypatch.setattr(results, "_videos_repo_commit", lambda: None)
    assert main(["results", "--out-md", str(tmp_path / "r.md")]) == 0
    index = json.loads((paths.WORK / "results" / "index.json").read_text())
    assert index["tools"] == {} and index["videos_repo_commit"] is None


def test_results_markdown_defaults_to_the_work_directory(full, monkeypatch):
    monkeypatch.setattr(results.manifest, "load", lambda **kw: [design(ID, commit=COMMIT)])
    monkeypatch.setattr(results.tools, "versions", lambda: dict(TOOLS))
    assert main(["results"]) == 0
    md = paths.WORK / "results" / "results.md"
    assert md.read_text().startswith("# Results") and (paths.WORK / "results" / "index.json").exists()
