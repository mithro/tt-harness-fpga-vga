# SPDX-License-Identifier: Apache-2.0
"""One design through the normal build and reference stages with the sandbox ON (the rest of the suite runs
with it off): both records say bwrap and nothing of the sandbox's directory is left."""

import json
from pathlib import Path

import pytest
from gitrepo import commit_files, init

from tthfv import build, fetch, paths, reference, sandbox, tools
from tthfv.manifest import Design

DATA = Path(__file__).parent / "data"

try:
    for _t in ("verilator", "yosys", "nextpnr-ice40", "icepack"):
        tools.tool(_t)
    _MISSING = "" if sandbox.available() else f"bwrap cannot run here: {sandbox.unavailable_reason()}"
except Exception as e:  # noqa: BLE001
    _MISSING = f"toolchain not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]


def test_build_and_reference_with_the_sandbox_on(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    repo = init(tmp_path / "proj")
    text = (DATA / "tt_um_synth_vga.v").read_text().replace("module tt_um_synth_vga", "module tt_um_x")
    commit = commit_files(repo, {"src/tt_um_x.v": text})
    d = Design(
        id="tt99/tt_um_x", shuttle="tt99", macro="tt_um_x", title="", author="", repo=str(repo), commit=commit,
        top_module="tt_um_x", source_files=("tt_um_x.v",), clock_hz=25_175_000, clock_declared=True,
        pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None, qspi=None,
        ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    fetched = fetch.fetch(d)
    assert fetched.status == "ok", fetched.reason

    b = build.build(d, fetched)
    assert b.status in ("ok", "timing-fail"), b.reason
    bdir = paths.design_dir("build", d.id)
    assert json.loads((bdir / "build.json").read_text())["sandbox"] == "bwrap"
    assert not (bdir / "scratch").exists() and (bdir / "design.bin").is_file()

    r = reference.make_reference(d, 1_300_000)
    assert r.status == "ok", r
    rdir = reference.reference_dir(d.id, "ideal")
    assert json.loads((rdir / "reference.json").read_text())["sandbox"] == "bwrap"
    assert json.loads((rdir / "sim.json").read_text())["sandbox"] == "bwrap"
    assert not (rdir / "scratch").exists()
    assert not (paths.WORK / "trash").exists()
