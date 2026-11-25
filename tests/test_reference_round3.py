# SPDX-License-Identifier: Apache-2.0
"""Fix round 3 of plan 2 task 5: a reused reference reports exactly what a fresh one does; an interrupt during
--sensitivity keeps the finished reference. Real Verilator."""

import dataclasses
from pathlib import Path

import pytest
from gitrepo import commit_files, git, init
from test_reference_profile import CLOCKS, COPY_UI, board, project, record, ref_dir, work  # noqa: F401
from test_reference_stimulus import UNSETTLED_TIED

from tthfv import paths, reference, tools
from tthfv.cli import main
from tthfv.manifest import Design

try:
    tools.tool("verilator")
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate the tool means skip
    _MISSING = f"verilator not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

SYNTH = (Path(__file__).parent / "data" / "tt_um_synth_vga.v").read_text()
SYNC_CLOCKS = 1_300_000


def synth_design(tmp_path) -> Design:
    """A design that syncs (640x480), in a local git repository."""
    repo = init(tmp_path / "proj-synth")
    commit = commit_files(repo, {"src/tt_um_synth_vga.v": SYNTH})
    base = Design(
        id="tt99/tt_um_synth_vga", shuttle="tt99", macro="tt_um_synth_vga", title="", author="", repo=str(repo),
        commit=commit, top_module="tt_um_synth_vga", source_files=("tt_um_synth_vga.v",), clock_hz=25_175_000,
        clock_declared=True, pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None,
        qspi=None, ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    assert git(repo, "rev-parse", "HEAD") == commit
    return dataclasses.replace(base)


def twice(monkeypatch, capsys, tmp_path, d, clocks):
    """Run `tthfv reference` under fpga-4 twice on the same work directory; (out1, err1, out2, err2, simulations
    made by the second run)."""
    monkeypatch.setattr(reference.manifest, "load", lambda **kw: [d])
    board(tmp_path)  # writes fpga-4.json
    args = ["reference", d.id, "--clocks", str(clocks), "--profile", str(tmp_path / "fpga-4.json")]
    main(args)
    out1, err1 = capsys.readouterr()
    sims = []
    real = reference.simulate
    monkeypatch.setattr(reference, "simulate", lambda *a, **k: (sims.append(1), real(*a, **k))[1])
    main(args)
    out2, err2 = capsys.readouterr()
    return out1, err1, out2, err2, len(sims)


def test_a_reused_reference_that_syncs_reports_its_unmet_inputs_like_the_fresh_one(
    tmp_path, monkeypatch, capsys
):
    d = synth_design(tmp_path)
    out1, err1, out2, err2, sims = twice(monkeypatch, capsys, tmp_path, d, SYNC_CLOCKS)
    assert "[inputs unmet: uio_in[7:6] requested 0, held at 1]" in out1  # the fresh run says it
    assert out2 == out1 and err2 == err1 and "WARNING: 1 of 1 references have unmet inputs" in err2
    assert sims == 0  # the second run reused the reference


def test_a_reused_reference_without_sync_keeps_its_reason_line(tmp_path, monkeypatch, capsys):
    d = project(tmp_path, COPY_UI)
    out1, err1, out2, err2, sims = twice(monkeypatch, capsys, tmp_path, d, CLOCKS)
    assert out1.startswith(f"{d.id} no-sync (inputs unmet: uio_in[7:6] requested 0, held at 1)")
    assert out2 == out1 and err2 == err1 and sims == 0


def test_a_failed_reference_reports_the_same_reason_each_time(tmp_path, monkeypatch, capsys):
    d = project(tmp_path, UNSETTLED_TIED)
    out1, _, out2, _, _ = twice(monkeypatch, capsys, tmp_path, d, CLOCKS)
    assert "sim-unsettled (ref_tb: the pad did not settle" in out1
    assert out2 == out1  # a failure is never reused: it is simulated again and fails the same way


# ---- an interrupt during --sensitivity keeps the finished reference


def test_an_interrupt_during_sensitivity_keeps_the_reference_and_the_next_run_retries(tmp_path, monkeypatch):
    d = project(tmp_path, COPY_UI)
    profile = board(tmp_path)

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    real = reference._sensitivity
    monkeypatch.setattr(reference, "_sensitivity", interrupted)
    with pytest.raises(KeyboardInterrupt):
        reference.make_reference(d, clocks=CLOCKS, profile=profile, sensitivity=True)
    assert (ref_dir(d, "fpga-4") / "reference.json").exists()
    assert record(d, "fpga-4")["reset_determined"] is None
    monkeypatch.setattr(reference, "_sensitivity", real)
    sims = []
    real_sim = reference.simulate
    monkeypatch.setattr(reference, "simulate", lambda *a, **k: (sims.append(1), real_sim(*a, **k))[1])
    reference.make_reference(d, clocks=CLOCKS, profile=profile, sensitivity=True)
    assert record(d, "fpga-4")["reset_determined"] is True and sims == []  # retried, not simulated again
    assert paths.WORK.exists()
