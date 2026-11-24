# SPDX-License-Identifier: Apache-2.0
"""The reference simulates what the fetch stage resolved (real Verilator)."""

import dataclasses
import json
from pathlib import Path

import pytest
from gitrepo import commit_files
from test_reference_stale import COUNTER, design_at, head, project, record, sims, work  # noqa: F401

from tthfv import paths, reference, tools

try:
    tools.tool("verilator")
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate the tool means skip
    _MISSING = f"verilator not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]


def sim_log(d) -> str:
    return (reference.reference_dir(d.id, "ideal") / "sim.log").read_text()


def test_wrong_top_module_simulates_the_single_tt_um_module(project, sims):  # noqa: F811
    d = design_at(project, head(project), top_module="tt_um_wrong")
    r = reference.make_reference(d, clocks=40)
    assert r.status == "no-sync", r.reason
    assert "--top-module tt_um_counter_vga" in sim_log(d)
    assert "not declared; using tt_um_counter_vga" in sim_log(d)
    assert record(d)["key"]["top_module_used"] == "tt_um_counter_vga"


def test_headers_are_not_compiled_as_units_but_are_searched(project, sims):  # noqa: F811
    text = '`include "defs.svh"\n' + COUNTER.replace("count + 8'd1", "count + `STEP")
    c = commit_files(
        project,
        {"src/tt_um_counter_vga.v": text, "src/defs.svh": "`define STEP 8'd5\n", "src/cfg.vlt": ""},
    )
    d = design_at(project, c, source_files=("tt_um_counter_vga.v", "defs.svh", "cfg.vlt"))
    r = reference.make_reference(d, clocks=40)
    assert r.status == "no-sync", r.reason
    cmd = next(ln for ln in sim_log(d).splitlines() if ln.startswith("$ ") and "--top-module" in ln)
    assert "defs.svh" not in cmd and "cfg.vlt" not in cmd
    assert "tt_um_counter_vga.v" in cmd


def test_changed_unlisted_file_invalidates_the_reference(project, sims):  # noqa: F811
    c = commit_files(project, {"src/rom.hex": "00\n"})
    d = design_at(project, c)
    reference.make_reference(d, clocks=40)
    reference.make_reference(d, clocks=40)
    assert len(sims) == 1
    src = paths.WORK / "src" / d.id / "repo" / "src"
    (src / "rom.hex").write_text("ff\n")  # a file the design may read, not in the source list
    reference.make_reference(d, clocks=40)
    assert len(sims) == 2
    assert record(d)["key"]["sources_sha256"] != ""


def test_verilator_flags_are_recorded_and_a_define_changes_the_dump(project, sims):  # noqa: F811
    text = COUNTER.replace("assign uo_out  = count;", "assign uo_out  = `VAL;")
    c = commit_files(project, {"src/tt_um_counter_vga.v": text})
    d = design_at(project, c, override={"verilator_flags": ["-DVAL=8'h5A"]})
    reference.make_reference(d, clocks=40)
    j = record(d)
    assert j["key"]["verilator_flags"] == ["-DVAL=8'h5A"]
    import gzip

    dump = gzip.decompress((reference.reference_dir(d.id, "ideal") / j["dump"]).read_bytes())
    assert dump == bytes([0x5A]) * 40
    other = dataclasses.replace(d, override={"verilator_flags": ["-DVAL=8'hA5"]})
    reference.make_reference(other, clocks=40)
    assert len(sims) == 2
    dump = gzip.decompress((reference.reference_dir(d.id, "ideal") / record(d)["dump"]).read_bytes())
    assert dump == bytes([0xA5]) * 40
    assert json.loads(Path(reference.reference_dir(d.id, "ideal") / "sim.json").read_text())["status"] == "ok"
