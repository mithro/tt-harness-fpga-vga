# SPDX-License-Identifier: Apache-2.0
"""write_reference and the `tthfv reference` subcommand, on the bars fixture and fake simulations."""

import dataclasses
import gzip
import hashlib
import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from tthfv import frames, paths, reference
from tthfv.cli import main
from tthfv.fetch import FetchResult
from tthfv.manifest import Design

DATA = Path(__file__).parent / "data"


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return tmp_path / "work"


@pytest.fixture(scope="module")
def bars_raw() -> bytes:
    return gzip.decompress((DATA / "bars.bin.gz").read_bytes())


def fake_design(id="calibration/tt_um_vgacal_bars", **kw) -> Design:
    shuttle, macro = id.split("/")
    base = Design(
        id=id, shuttle=shuttle, macro=macro, title="", author="", repo="", commit="", top_module=macro,
        source_files=(), clock_hz=25_175_000, clock_declared=True, pinout={}, pmods=(), tiles="1x1",
        render_verdict=None, override={}, patch=None, qspi=None, ui_in=0, uio_in=0, scripted=False,
        candidate=False,
    )  # fmt: skip
    return dataclasses.replace(base, **kw)


@pytest.fixture(autouse=True)
def fake_verilator_version(monkeypatch):
    monkeypatch.setattr(reference, "_verilator_version", lambda: "V 1")


def fake_sim(
    design: Design, dump_bytes: bytes, uio_bytes: bytes | None = None, clocks: int | None = None
) -> reference.SimResult:
    n = len(dump_bytes) if clocks is None else clocks  # the clocks asked for, as the real simulate records
    key = reference.sim_key(design, FetchResult("ok", Path(), (), (), Path(), ""), n)
    out = paths.design_dir("reference", design.id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "uo_out.bin").write_bytes(dump_bytes)
    (out / "uio.bin").write_bytes(uio_bytes if uio_bytes is not None else bytes(2 * len(dump_bytes)))
    (out / "sim.json").write_text(
        json.dumps({"status": "ok", "key": key, "key_sha256": reference.key_sha256(key)})
    )
    return reference.SimResult("ok", out / "uo_out.bin", out / "uio.bin", len(dump_bytes), out / "sim.log")


def test_write_reference(work, bars_raw):
    d = fake_design(ui_in=3, uio_in=5)
    sim = fake_sim(d, bars_raw)
    fs = frames.cut_frames(np.frombuffer(bars_raw, np.uint8))
    path = frames.write_reference(d, sim, fs, n=2)
    out = path.parent
    assert path == out / "reference.json"
    j = json.loads(path.read_text())
    assert j["id"] == d.id
    assert j["clock_hz"] == 25_175_000 and j["clock_declared"] is True
    assert (j["reset_clocks"], j["ui_in"], j["uio_in"], j["clocks"]) == (10, 3, 5, len(bars_raw))
    assert j["dump"] == "uo_out.bin.gz"
    assert j["dump_sha256"] == hashlib.sha256(bars_raw).hexdigest()
    assert gzip.decompress((out / "uo_out.bin.gz").read_bytes()) == bars_raw
    assert j["uio_dump"] == "uio.bin.gz"
    assert j["uio_dump_sha256"] == hashlib.sha256(bytes(2 * len(bars_raw))).hexdigest()
    assert not (out / "uo_out.bin").exists() and not (out / "uio.bin").exists()
    assert j["mode"] == fs.mode and j["line_clocks"] == fs.line_clocks and j["lines"] == fs.lines
    assert (j["width"], j["height"]) == (fs.width, fs.height)
    assert j["hsync_active_low"] is fs.hsync_active_low
    assert j["partial_frames_dropped"] == fs.partial_frames_dropped
    assert j["tools"] == {"verilator": "V 1"}
    assert j["schema"] == 2
    assert len(fs.frames) > 2  # the fixture holds more frames than PNGs are written for
    assert len(j["frames"]) == len(fs.frames) == j["frames_complete"]  # every complete frame is listed
    for f, listed in zip(fs.frames, j["frames"], strict=True):
        assert (listed["index"], listed["start_clock"], listed["hash"]) == (
            f.index, f.start_clock, f"{f.hash:016x}"
        )
        assert ("png" in listed) == (f.index < 2)  # PNGs of the first n only; no png key beyond them
    assert j["frames"][0] == {
        "index": 0, "start_clock": fs.frames[0].start_clock,
        "hash": f"{fs.frames[0].hash:016x}", "png": "frame-000.png",
    }  # fmt: skip
    img = Image.open(out / "frame-000.png")
    assert img.mode == "RGB" and img.size == (fs.width, fs.height)
    assert img.tobytes() == fs.frames[0].rgb888
    assert (out / "frame-001.png").exists() and not (out / "frame-002.png").exists()
    assert not list(out.glob("*.tmp"))


def test_write_reference_is_reproducible(work, bars_raw):
    d = fake_design()
    fs = frames.cut_frames(np.frombuffer(bars_raw, np.uint8))
    frames.write_reference(d, fake_sim(d, bars_raw), fs)
    first = (work / "reference" / d.id / "uo_out.bin.gz").read_bytes()
    shutil.rmtree(work)
    frames.write_reference(d, fake_sim(d, bars_raw), fs)
    assert (work / "reference" / d.id / "uo_out.bin.gz").read_bytes() == first


def test_write_reference_with_no_frames(work):
    raw = bytes(5000)
    d = fake_design()
    path = frames.write_reference(d, fake_sim(d, raw), frames.cut_frames(np.frombuffer(raw, np.uint8)))
    j = json.loads(path.read_text())
    assert j["frames"] == [] and j["mode"] is None and j["line_clocks"] is None


def test_write_reference_from_an_already_gzipped_dump(work, bars_raw):
    d = fake_design()
    sim = fake_sim(d, bars_raw)
    fs = frames.cut_frames(np.frombuffer(bars_raw, np.uint8))
    first = json.loads(frames.write_reference(d, sim, fs).read_text())
    gz = sim.dump.with_suffix(".bin.gz")
    again = reference.SimResult("ok", gz, sim.uio.with_suffix(".bin.gz"), sim.clocks, sim.log)
    assert json.loads(frames.write_reference(d, again, fs).read_text()) == first


# ---- the subcommand


def stub_pipeline(monkeypatch, raw_by_id: dict[str, bytes], calls: list):
    designs = [fake_design(i) for i in raw_by_id]
    monkeypatch.setattr(reference.manifest, "load", lambda **kw: designs)
    monkeypatch.setattr(
        reference.fetch, "fetch", lambda d, force=False: FetchResult("ok", Path(), (), (), Path(), "")
    )

    def simulate(design, fetched, clocks=reference.DEFAULT_CLOCKS, force=False):
        calls.append((design.id, clocks, force))
        return fake_sim(design, raw_by_id[design.id], clocks=clocks)

    monkeypatch.setattr(reference, "simulate", simulate)


def test_cli_reference_prints_one_line_per_design(work, bars_raw, monkeypatch, capsys):
    calls: list = []
    stub_pipeline(
        monkeypatch, {"calibration/tt_um_vgacal_bars": bars_raw, "tt99/tt_um_blank": bytes(9000)}, calls
    )
    rc = main(["reference", "calibration/tt_um_vgacal_bars", "tt99/tt_um_blank", "--clocks", "1300000"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("calibration/tt_um_vgacal_bars ok 640x480@60 ")
    assert lines[0].endswith(" frames")
    assert lines[1] == "tt99/tt_um_blank no-sync - 0 frames"
    assert rc == 1  # a no-sync design is not ok
    assert (work / "reference/tt99/tt_um_blank/reference.json").exists()
    assert calls == [
        ("calibration/tt_um_vgacal_bars", 1_300_000, False),
        ("tt99/tt_um_blank", 1_300_000, False),
    ]


def test_cli_reference_all_ok_returns_zero_and_skips_finished_work(work, bars_raw, monkeypatch, capsys):
    calls: list = []
    stub_pipeline(monkeypatch, {"calibration/tt_um_vgacal_bars": bars_raw}, calls)
    assert main(["reference", "calibration/tt_um_vgacal_bars"]) == 0
    first = capsys.readouterr().out
    assert main(["reference", "calibration/tt_um_vgacal_bars"]) == 0
    assert capsys.readouterr().out == first
    assert len(calls) == 1  # the second run found reference.json
    assert main(["reference", "calibration/tt_um_vgacal_bars", "--force"]) == 0
    assert len(calls) == 2 and calls[1][2] is True


def test_reset_clocks_constant_matches_the_testbench():
    tb = (Path(reference.__file__).parent / "harness" / "ref_tb.cpp").read_text()
    m = re.search(r"RESET_CLOCKS\s*=\s*(\d+)\s*;", tb)
    assert m and int(m.group(1)) == frames.RESET_CLOCKS


def test_an_old_or_damaged_reference_is_not_finished(work, bars_raw, monkeypatch, capsys):
    calls: list = []
    stub_pipeline(monkeypatch, {"calibration/tt_um_vgacal_bars": bars_raw}, calls)
    ref = work / "reference/calibration/tt_um_vgacal_bars/reference.json"
    assert main(["reference", "calibration/tt_um_vgacal_bars"]) == 0
    good = json.loads(ref.read_text())
    assert len(calls) == 1
    # no frames_complete (an older writer): redone, not a KeyError
    del good["frames_complete"]
    ref.write_text(json.dumps(good))
    assert main(["reference", "calibration/tt_um_vgacal_bars"]) == 0
    assert len(calls) == 2 and "frames_complete" in json.loads(ref.read_text())
    # the dump it names is gone: redone
    (ref.parent / "uo_out.bin.gz").unlink()
    assert main(["reference", "calibration/tt_um_vgacal_bars"]) == 0
    assert len(calls) == 3
    capsys.readouterr()


def test_cli_reference_reports_a_failed_simulation(work, monkeypatch, capsys):
    designs = [fake_design("tt99/tt_um_bad")]
    monkeypatch.setattr(reference.manifest, "load", lambda **kw: designs)
    monkeypatch.setattr(
        reference.fetch, "fetch", lambda d, force=False: FetchResult("ok", Path(), (), (), Path(), "")
    )
    monkeypatch.setattr(
        reference,
        "simulate",
        lambda d, f, clocks=0, force=False: reference.SimResult(
            "verilate-fail", Path(), Path(), 0, Path(), "boom"
        ),
    )
    assert main(["reference", "tt99/tt_um_bad"]) == 1
    assert capsys.readouterr().out == "tt99/tt_um_bad verilate-fail (boom) - 0 frames\n"
    assert not (work / "reference/tt99/tt_um_bad/reference.json").exists()


def test_make_reference_returns_a_result_and_records_a_failure(work, bars_raw, monkeypatch):
    stub_pipeline(monkeypatch, {"calibration/tt_um_vgacal_bars": bars_raw}, [])
    d = fake_design("calibration/tt_um_vgacal_bars")
    r = reference.make_reference(d, clocks=1_300_000)
    assert (r.status, r.mode, r.reason) == ("ok", "640x480@60", "")
    assert r.frames_complete > 0

    bad = fake_design("tt99/tt_um_bad")
    monkeypatch.setattr(
        reference,
        "simulate",
        lambda d, f, clocks=0, force=False: reference.SimResult(
            "sim-fail", Path(), Path(), 0, Path(), "boom"
        ),
    )
    r = reference.make_reference(bad)
    assert (r.status, r.reason) == ("sim-fail", "boom")
    fail = json.loads((work / "reference/tt99/tt_um_bad/reference-fail.json").read_text())
    assert fail == {"id": "tt99/tt_um_bad", "status": "sim-fail", "reason": "boom"}
