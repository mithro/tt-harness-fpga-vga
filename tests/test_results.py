# SPDX-License-Identifier: Apache-2.0
"""The results index and the Markdown table, from stage records written into a scratch WORK."""

import dataclasses
import json
from pathlib import Path

import pytest

from tthfv import paths, results
from tthfv.cli import main
from tthfv.manifest import Design


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return tmp_path / "work"


def design(id: str, **kw) -> Design:
    shuttle, macro = id.split("/")
    base = Design(
        id=id, shuttle=shuttle, macro=macro, title=f"Title of {macro}", author="Ann", repo="", commit="",
        top_module=macro, source_files=(), clock_hz=25_175_000, clock_declared=True, pinout={}, pmods=(),
        tiles="1x1", render_verdict="ok", override={}, patch=None, qspi=None, ui_in=0, uio_in=0,
        scripted=False, candidate=True,
    )  # fmt: skip
    return dataclasses.replace(base, **kw)


def put(work: Path, stage: str, id: str, name: str, data: dict) -> None:
    d = work / stage / id
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(data))


BUILD_OK = {
    "id": "tt01/tt_um_b", "status": "ok", "reason": "", "bitstream_sha256": "ab" * 32,
    "clock_hz": 25_175_000, "fmax_hz": 31_250_000, "lut_percent": 12.5,
    "recipe": {"synth": "default", "seed": 2},
    "attempts": [{"synth": "default", "seed": 1}, {"synth": "default", "seed": 2}],
}  # fmt: skip
REF_OK = {
    "mode": "640x480@60",
    "width": 640,
    "height": 480,
    "frames_complete": 5,
    "frames": [{"hash": "00ff"}],
}


def test_a_design_with_nothing_run_is_not_run_everywhere(work):
    (rec,) = results.collect([design("tt01/tt_um_a")], work)
    assert rec["id"] == "tt01/tt_um_a"
    assert rec["fetch"] == {"status": "not-run", "reason": None}
    assert rec["build"] == {
        "status": "not-run", "reason": None, "lut_percent": None, "fmax_hz": None, "recipe": None,
        "attempts": None, "bitstream_sha256": None,
    }  # fmt: skip
    assert rec["reference"] == {
        "status": "not-run", "reason": None, "mode": None, "width": None, "height": None,
        "frames_complete": None, "hash": None,
    }  # fmt: skip


def test_the_record_carries_the_manifest_fields(work):
    d = design("tt01/tt_um_a", qspi="map", scripted=True, ui_in=3, clock_declared=False, tiles="2x2")
    (rec,) = results.collect([d], work)
    for key, value in {
        "title": "Title of tt_um_a", "author": "Ann", "shuttle": "tt01", "macro": "tt_um_a", "tiles": "2x2",
        "clock_hz": 25_175_000, "clock_declared": False, "candidate": True, "render_verdict": "ok",
        "qspi": True, "scripted": True, "ui_in": 3,
    }.items():  # fmt: skip
        assert rec[key] == value, key
    assert results.collect([design("tt01/tt_um_a")], work)[0]["qspi"] is False


def test_stage_files_are_joined(work):
    put(work, "src", "tt01/tt_um_b", "fetch.json", {})
    put(work, "build", "tt01/tt_um_b", "build.json", BUILD_OK)
    put(work, "reference", "tt01/tt_um_b", "reference.json", REF_OK)
    (rec,) = results.collect([design("tt01/tt_um_b")], work)
    assert rec["fetch"]["status"] == "ok"
    assert rec["build"] == {
        "status": "ok", "reason": "", "lut_percent": 12.5, "fmax_hz": 31_250_000,
        "recipe": {"synth": "default", "seed": 2}, "attempts": 2, "bitstream_sha256": "ab" * 32,
    }  # fmt: skip
    assert rec["reference"] == {
        "status": "ok", "reason": "", "mode": "640x480@60", "width": 640, "height": 480,
        "frames_complete": 5, "hash": "00ff",
    }  # fmt: skip


def test_failures_and_the_runners_outcome_are_used(work):
    id = "tt01/tt_um_c"
    put(work, "run", id, "outcome.json", {
        "fetch": {"status": "patch-fail", "reason": "bad patch"},
        "build": {"status": "not-run", "reason": "fetch failed"},
    })  # fmt: skip
    put(work, "src", id, "fetch.json", {})  # a stale good fetch: the latest run wins
    put(work, "reference", id, "reference-fail.json", {"status": "sim-fail", "reason": "boom"})
    (rec,) = results.collect([design(id)], work)
    assert rec["fetch"] == {"status": "patch-fail", "reason": "bad patch"}
    assert rec["build"]["status"] == "not-run"
    assert (rec["reference"]["status"], rec["reference"]["reason"]) == ("sim-fail", "boom")


def test_no_sync_reference_and_a_failed_build(work):
    id = "tt01/tt_um_d"
    put(work, "reference", id, "reference.json", {**REF_OK, "mode": None, "frames_complete": 0, "frames": []})
    put(
        work,
        "build",
        id,
        "build.json",
        {**BUILD_OK, "status": "timing-fail", "reason": "slow", "bitstream_sha256": None},
    )
    (rec,) = results.collect([design(id)], work)
    assert rec["reference"]["status"] == "no-sync" and rec["reference"]["hash"] is None
    assert (rec["build"]["status"], rec["build"]["reason"]) == ("timing-fail", "slow")


def test_markdown_is_sorted_escaped_and_has_a_summary(work):
    put(work, "build", "tt01/tt_um_b", "build.json", BUILD_OK)
    put(work, "reference", "tt01/tt_um_b", "reference.json", REF_OK)
    designs = [
        design("tt02/tt_um_z", title="Pipes | in | title"),
        design("tt01/tt_um_b", tiles="1x2"),
        design("tt01/tt_um_a", clock_hz=50_000_000),
    ]
    md = results.render_markdown(results.collect(designs, work))
    rows = [line for line in md.splitlines() if line.startswith("| tt0")]
    assert [r.split(" | ")[0] for r in rows] == ["| tt01/tt_um_a", "| tt01/tt_um_b", "| tt02/tt_um_z"]
    assert "| id | title | tiles | build | LUT % | fmax / clock (MHz) | reference | mode | frames |" in md
    assert (
        "| tt01/tt_um_b | Title of tt_um_b | 1x2 | ok | 12.5 | 31.25 / 25.175 | ok | 640x480@60 | 5 |" in md
    )
    assert "| tt01/tt_um_a | Title of tt_um_a | 1x1 | not-run | - | - / 50 | not-run | - | - |" in md
    assert r"Pipes \| in \| title" in md
    assert all(len(r.replace(r"\|", "").split("|")) == 11 for r in rows)  # 9 columns, no stray cells
    summary = md.split("| id |")[0]
    assert "3 designs" in summary
    assert "build: 1 ok, 2 not-run" in summary
    assert "reference: 1 ok, 2 not-run" in summary
    assert "fetch: 3 not-run" in summary


def test_markdown_has_no_timestamp_and_is_byte_identical(work):
    designs = [design("tt01/tt_um_a"), design("tt01/tt_um_b")]
    put(work, "build", "tt01/tt_um_b", "build.json", BUILD_OK)
    a = results.render_markdown(results.collect(designs, work))
    b = results.render_markdown(results.collect(designs, work))
    assert a == b
    assert "20" not in a.split("| id |")[0].replace("25.175", "")  # no year-like date in the summary


def test_cli_writes_index_and_markdown(work, tmp_path, monkeypatch):
    designs = [design("tt01/tt_um_b"), design("tt01/tt_um_a")]
    monkeypatch.setattr(results.manifest, "load", lambda **kw: designs)
    put(work, "build", "tt01/tt_um_b", "build.json", BUILD_OK)
    out = tmp_path / "out" / "results.md"
    assert main(["results", "--out-md", str(out)]) == 0
    index = json.loads((work / "results" / "index.json").read_text())
    assert index["count"] == 2 and [r["id"] for r in index["designs"]] == ["tt01/tt_um_a", "tt01/tt_um_b"]
    assert index["generated"].endswith("Z") and "T" in index["generated"]
    first = out.read_bytes()
    assert main(["results", "--out-md", str(out)]) == 0
    assert out.read_bytes() == first
