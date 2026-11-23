# SPDX-License-Identifier: Apache-2.0
"""make_reference must not reuse a reference whose simulation key differs from the current one."""

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from gitrepo import commit_files, git, init

from tthfv import paths, reference, tools
from tthfv.manifest import Design

DATA = Path(__file__).parent / "data"
COUNTER = (DATA / "tt_um_counter_vga.v").read_text()

try:
    tools.tool("verilator")
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate the tool means skip
    _MISSING = f"verilator not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return tmp_path / "work"


@pytest.fixture
def sims(monkeypatch):
    """One entry per real simulation."""
    calls = []
    real = reference.simulate

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(reference, "simulate", counting)
    return calls


@pytest.fixture
def project(tmp_path):
    repo = init(tmp_path / "proj")
    commit_files(repo, {"src/tt_um_counter_vga.v": COUNTER})
    return repo


def design_at(repo: Path, commit: str, **kw) -> Design:
    base = Design(
        id="tt99/tt_um_counter_vga", shuttle="tt99", macro="tt_um_counter_vga", title="", author="",
        repo=str(repo), commit=commit, top_module="tt_um_counter_vga",
        source_files=("tt_um_counter_vga.v",), clock_hz=25_175_000, clock_declared=True, pinout={},
        pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None, qspi=None, ui_in=0,
        uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    return dataclasses.replace(base, **kw)


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def record(d: Design) -> dict:
    return json.loads((paths.design_dir("reference", d.id) / "reference.json").read_text())


def test_unchanged_is_reused(project, sims):
    d = design_at(project, head(project))
    assert reference.make_reference(d, clocks=40).status == "no-sync"
    assert reference.make_reference(d, clocks=40).status == "no-sync"
    assert len(sims) == 1


def test_record_carries_the_key_and_its_digest(project, sims):
    d = design_at(project, head(project))
    reference.make_reference(d, clocks=40)
    j = record(d)
    blob = json.dumps(j["key"], sort_keys=True, separators=(",", ":")).encode()
    assert j["key_sha256"] == hashlib.sha256(blob).hexdigest()
    for field in (
        "ui_in", "uio_in", "top_module_used", "verilator_flags", "clocks", "verilator",
        "testbench_sha256", "cutter_version", "commit", "repo",
    ):  # fmt: skip
        assert field in j["key"], field


def test_changed_ui_in_is_redone(project, sims):
    d = design_at(project, head(project))
    reference.make_reference(d, clocks=40)
    reference.make_reference(dataclasses.replace(d, ui_in=d.ui_in ^ 0xFF), clocks=40)
    assert len(sims) == 2
    assert record(d)["key"]["ui_in"] == 0xFF


def test_changed_uio_in_is_redone(project, sims):
    d = design_at(project, head(project))
    reference.make_reference(d, clocks=40)
    reference.make_reference(dataclasses.replace(d, uio_in=0x0F), clocks=40)
    assert len(sims) == 2


def test_new_commit_with_the_same_text_is_redone(project, sims):
    d = design_at(project, head(project))
    reference.make_reference(d, clocks=40)
    again = commit_files(project, {"README": "x\n"})
    reference.make_reference(dataclasses.replace(d, commit=again), clocks=40)
    assert len(sims) == 2
    assert record(d)["key"]["commit"] == again


def test_changed_source_content_is_redone(project, sims):
    d = design_at(project, head(project))
    reference.make_reference(d, clocks=40)
    text = COUNTER.replace("count + 8'd1", "count + 8'd3")
    changed = commit_files(project, {"src/tt_um_counter_vga.v": text})
    reference.make_reference(dataclasses.replace(d, commit=changed), clocks=40)
    assert len(sims) == 2


def test_a_short_reference_is_not_reused_by_a_default_run(project, sims, monkeypatch):
    monkeypatch.setattr(reference, "DEFAULT_CLOCKS", 60)
    d = design_at(project, head(project))
    reference.make_reference(d, clocks=40)
    reference.make_reference(d, clocks=None)
    assert len(sims) == 2
    assert record(d)["clocks"] == 60
    reference.make_reference(d, clocks=None)  # now the default run is reused
    assert len(sims) == 2
