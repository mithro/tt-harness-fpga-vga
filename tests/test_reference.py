# SPDX-License-Identifier: Apache-2.0
import dataclasses
import json
import re
from pathlib import Path

import pytest

from tthfv import paths, reference, tools
from tthfv.fetch import FetchResult
from tthfv.manifest import Design

DATA = Path(__file__).parent / "data"

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


def make(tmp_path, text: str, name="tt_um_counter_vga", **kw) -> tuple[Design, FetchResult]:
    src = tmp_path / "proj" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / f"{name}.v").write_text(text)
    base = Design(
        id=f"tt99/{name}", shuttle="tt99", macro=name, title="", author="", repo="", commit="",
        top_module=name, source_files=(f"{name}.v",), clock_hz=25_175_000, clock_declared=True,
        pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None, qspi=None,
        ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    d = dataclasses.replace(base, **kw)
    fr = FetchResult("ok", src.parent, (src / f"{name}.v",), (src,), tmp_path / "fetch.log")
    return d, fr


COUNTER = (DATA / "tt_um_counter_vga.v").read_text()


def test_counter_dump(tmp_path):
    d, fr = make(tmp_path, COUNTER)
    r = reference.simulate(d, fr, clocks=300)
    assert r.status == "ok", r.log.read_text()
    data = r.dump.read_bytes()
    assert len(data) == 300 and r.clocks == 300
    assert data[0:10] == bytes(10)
    assert data[10] == 1 and data[11] == 2
    assert data[10 + 255] == 0  # 8-bit wrap
    uio = r.uio.read_bytes()
    assert len(uio) == 600
    assert uio[2 * 11 : 2 * 11 + 2] == bytes([0x2A, 0])  # uio_out = {count[3:0], 4'hA}, uio_oe = uio_in
    sim = json.loads((r.dump.parent / "sim.json").read_text())
    assert sim["status"] == "ok" and sim["clocks"] == 300 and sim["ui_in"] == 0


def test_inputs_reach_the_design(tmp_path):
    text = COUNTER.replace("assign uo_out  = count;", "assign uo_out  = ui_in;")
    d, fr = make(tmp_path, text, ui_in=7, uio_in=0x5C)
    r = reference.simulate(d, fr, clocks=50)
    assert r.status == "ok", r.log.read_text()
    assert r.dump.read_bytes() == bytes([7]) * 50
    uio = r.uio.read_bytes()
    assert uio[1::2] == bytes([0x5C]) * 50


def test_syntax_error_is_a_result(tmp_path):
    d, fr = make(tmp_path, COUNTER.replace("endmodule", "assign = ;\nendmodule"))
    r = reference.simulate(d, fr, clocks=10)
    assert r.status == "verilate-fail"
    assert re.search(r"%Error", r.log.read_text())


def test_already_done_is_reused_and_force_redoes(tmp_path):
    d, fr = make(tmp_path, COUNTER)
    r = reference.simulate(d, fr, clocks=40)
    stamp = (r.dump.parent / "sim.json").stat().st_mtime_ns
    r2 = reference.simulate(d, fr, clocks=40)
    assert r2.status == "ok" and (r.dump.parent / "sim.json").stat().st_mtime_ns == stamp
    reference.simulate(d, fr, clocks=40, force=True)
    assert (r.dump.parent / "sim.json").stat().st_mtime_ns != stamp
    r3 = reference.simulate(d, fr, clocks=60)  # different clock count: redo
    assert len(r3.dump.read_bytes()) == 60


def test_edited_source_is_not_served_from_a_stale_dump(tmp_path):
    d, fr = make(tmp_path, COUNTER)
    first = reference.simulate(d, fr, clocks=30).dump.read_bytes()
    assert first[11] == 2
    fr.sources[0].write_text(COUNTER.replace("count + 8'd1", "count + 8'd3"))
    second = reference.simulate(d, fr, clocks=30)
    assert second.status == "ok", second.log.read_text()
    assert second.dump.read_bytes()[11] == 6
    assert second.dump.read_bytes() != first


def test_new_commit_is_not_served_from_a_stale_dump(tmp_path):
    d, fr = make(tmp_path, COUNTER, commit="aaaa")
    reference.simulate(d, fr, clocks=30)
    stamp = tmp_path / "work" / "reference" / d.id / "sim.json"
    before = stamp.stat().st_mtime_ns
    reference.simulate(dataclasses.replace(d, commit="bbbb"), fr, clocks=30)
    assert stamp.stat().st_mtime_ns != before
