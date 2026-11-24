# SPDX-License-Identifier: Apache-2.0
"""compare.py with synthetic references; the toolchain is not needed (one test makes a real reference in
tmp_path when the toolchain is installed)."""

import argparse
import gzip
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pytest
from gitrepo import commit_files, init
from PIL import Image
from test_frames import make_dump

from tthfv import compare, frames, paths, reference, tools
from tthfv.manifest import Design

KEY = {"commit": "c0ffee", "sources_sha256": "aa" * 32, "top_module_used": "tt_um_x", "stimulus_version": 2}


def write_ref(path: Path, samples: np.ndarray, key: dict | None = None, **over) -> Path:
    """A schema-2 reference.json (and its gzipped dump) in path, made from a sample array."""
    path.mkdir(parents=True, exist_ok=True)
    raw = np.asarray(samples, np.uint8).tobytes()
    (path / "uo_out.bin.gz").write_bytes(gzip.compress(raw, 1, mtime=0))
    fs = frames.cut_frames(samples)
    record = {
        "schema": 2, "id": "t/tt_um_x", "reset_clocks": 10, "clocks": len(raw),
        "dump": "uo_out.bin.gz", "dump_sha256": hashlib.sha256(raw).hexdigest(),
        "mode": fs.mode, "line_clocks": fs.line_clocks, "lines": fs.lines,
        "width": fs.width, "height": fs.height, "frames_complete": len(fs.frames),
        "frames": [{"index": f.index, "start_clock": f.start_clock, "hash": f"{f.hash:016x}"} for f in fs.frames],
        "key": KEY if key is None else key,
    }  # fmt: skip
    record.update(over)
    (path / "reference.json").write_text(json.dumps(record))
    return path


def write_build(path: Path, **over) -> Path:
    rec = {k: KEY[k] for k in ("commit", "sources_sha256", "top_module_used")} | over
    path.write_text(json.dumps(rec))
    return path


@pytest.fixture(scope="module")
def video():
    return make_dump(nframes=3)  # 1,262,... clocks of a 640x480 picture


@pytest.fixture
def ref(tmp_path, video):
    return write_ref(tmp_path / "ref", video)


def test_identical_is_exact(ref, video):
    c = compare.compare(video.copy(), ref, build_json=None)
    assert c.verdict == "exact"
    assert c.compared_clocks == c.capture_clocks == c.reference_clocks == len(video)
    assert c.first_diff_clock is None and c.diff_clocks == 0 and c.diff_bits == 0
    assert c.per_bit_first_diff == {} and c.offset == 0


def test_one_flipped_bit(ref, video):
    cap = video.copy()
    cap[1_234_567] ^= 0x10
    c = compare.compare(cap, ref, build_json=None)
    assert c.verdict == "mismatch"
    assert c.first_diff_clock == 1_234_567
    assert c.diff_clocks == 1 and c.diff_bits == 0x10
    assert c.per_bit_first_diff == {4: 1_234_567}


def test_several_bits_and_clocks(ref, video):
    cap = video.copy()
    cap[100] ^= 0x03
    cap[900] ^= 0x01
    cap[50_000] ^= 0x80
    c = compare.compare(cap, ref, build_json=None)
    assert c.first_diff_clock == 100 and c.diff_clocks == 3 and c.diff_bits == 0x83
    assert c.per_bit_first_diff == {0: 100, 1: 100, 7: 50_000}


def test_shifted_capture_is_a_mismatch_and_find_offset_finds_the_shift(ref, video):
    cap = np.concatenate([np.array([1, 2, 3], np.uint8), video])  # capture started 3 clocks early
    assert compare.compare(cap, ref, build_json=None).verdict == "mismatch"
    assert compare.find_offset(cap, video) == 3
    c = compare.compare(cap, ref, build_json=None, offset=3)
    assert c.verdict == "exact" and c.offset == 3  # compare does not judge the offset; the caller does
    late = video[5:]  # capture started 5 clocks late
    assert compare.find_offset(late, video) == -5
    c = compare.compare(late, ref, build_json=None, offset=-5)  # equal where both exist, but 5 clocks missing
    assert c.verdict == "mismatch" and c.first_diff_clock == 0 and c.diff_clocks == 5 and c.diff_bits == 0


def test_longer_capture_is_exact_when_the_reference_part_matches(ref, video):
    cap = np.concatenate([video, np.arange(1000, dtype=np.uint8)])
    c = compare.compare(cap, ref, build_json=None)
    assert c.verdict == "exact"
    assert c.compared_clocks == len(video) and c.capture_clocks == len(video) + 1000


def test_shorter_capture_is_a_mismatch_even_if_what_there_is_matches(ref, video):
    c = compare.compare(video[:500_000], ref, build_json=None)
    assert c.verdict == "mismatch"
    assert c.compared_clocks == 500_000 and c.first_diff_clock == 500_000
    assert c.diff_clocks == len(video) - 500_000 and "ends" in c.reason


def test_shorter_capture_with_a_difference_reports_the_difference(ref, video):
    cap = video[:500_000].copy()
    cap[7] ^= 0x40
    c = compare.compare(cap, ref, build_json=None)
    assert c.verdict == "mismatch" and c.first_diff_clock == 7 and c.diff_bits == 0x40


def test_refuses_a_different_sources_sha256(ref, video, tmp_path):
    build = write_build(tmp_path / "build.json", sources_sha256="bb" * 32)
    c = compare.compare(video, ref, build_json=build)
    assert c.verdict == "refused" and "sources_sha256" in c.reason
    assert c.compared_clocks == 0 and c.first_diff_clock is None


@pytest.mark.parametrize("field,value", [("commit", "deadbeef"), ("top_module_used", "other")])
def test_refuses_other_build_fields(ref, video, tmp_path, field, value):
    c = compare.compare(video, ref, build_json=write_build(tmp_path / "b.json", **{field: value}))
    assert c.verdict == "refused" and field in c.reason


def test_a_matching_build_json_is_accepted(ref, video, tmp_path):
    assert compare.compare(video, ref, build_json=write_build(tmp_path / "b.json")).verdict == "exact"


def test_refuses_an_unreadable_build_json(ref, video, tmp_path):
    bad = tmp_path / "b.json"
    bad.write_text("{not json")
    assert compare.compare(video, ref, build_json=bad).verdict == "refused"
    assert compare.compare(video, ref, build_json=tmp_path / "missing.json").verdict == "refused"


def test_expect_key_must_match_every_item(ref, video):
    ok = compare.compare(video, ref, build_json=None, expect_key={"stimulus_version": 2})
    assert ok.verdict == "exact"
    c = compare.compare(video, ref, build_json=None, expect_key={"commit": "c0ffee", "stimulus_version": 3})
    assert c.verdict == "refused" and "stimulus_version" in c.reason
    c = compare.compare(video, ref, build_json=None, expect_key={"nonexistent": 1})
    assert c.verdict == "refused" and "nonexistent" in c.reason


def test_refuses_schema_1_and_a_corrupt_dump(tmp_path, video):
    old = write_ref(tmp_path / "old", video, schema=1)
    assert compare.compare(video, old, build_json=None).verdict == "refused"
    bad = write_ref(tmp_path / "bad", video, dump_sha256="0" * 64)
    c = compare.compare(video, bad, build_json=None)
    assert c.verdict == "refused" and "sha256" in c.reason
    assert compare.compare(video, tmp_path / "nowhere", build_json=None).verdict == "refused"


def test_refuses_a_capture_that_is_not_bytes(ref):
    assert compare.compare(np.zeros(10, np.uint16), ref, build_json=None).verdict == "refused"
    assert compare.compare(np.zeros((2, 5), np.uint8), ref, build_json=None).verdict == "refused"


# ---- find_offset


def test_find_offset_none_when_nothing_matches():
    rng = np.random.default_rng(1)
    a = rng.integers(0, 256, 300_000, dtype=np.uint8)
    b = rng.integers(0, 256, 300_000, dtype=np.uint8)
    assert compare.find_offset(a, b) is None


def test_find_offset_zero_when_equal_and_prefers_the_smallest_shift_for_a_static_signal():
    rng = np.random.default_rng(2)
    a = rng.integers(0, 256, 300_000, dtype=np.uint8)
    assert compare.find_offset(a, a.copy()) == 0
    flat = np.full(300_000, 0x88, np.uint8)
    assert compare.find_offset(flat, flat.copy()) == 0
    assert compare.find_offset(flat[:200_000], flat) == 0


def test_find_offset_needs_a_large_overlap():
    rng = np.random.default_rng(3)
    ref_ = rng.integers(0, 256, 300_000, dtype=np.uint8)
    cap = np.concatenate([np.zeros(64, np.uint8), ref_[: 100_000 - 32]])  # overlap would be < 100,000
    assert compare.find_offset(cap, ref_, max_offset=64) is None
    short = ref_[:5000]  # a short array: the whole shorter array must match
    assert compare.find_offset(np.concatenate([[9, 9], short]).astype(np.uint8), short) == 2


def test_find_offset_is_fast_on_six_million_samples():
    rng = np.random.default_rng(4)
    ref_ = rng.integers(0, 256, 6_000_000, dtype=np.uint8)
    cap = np.concatenate([rng.integers(0, 256, 64, dtype=np.uint8), ref_])  # worst case: found at +64
    t = time.monotonic()
    assert compare.find_offset(cap, ref_) == 64
    assert compare.find_offset(ref_, cap) == -64
    assert compare.find_offset(ref_[1000:], ref_[:-1000]) is None  # nothing matches: every offset is tried
    assert time.monotonic() - t < 10


# ---- diff_report and images


def test_diff_report_places_the_difference_in_the_frame(ref, video, tmp_path):
    meta = json.loads((ref / "reference.json").read_text())
    f0 = meta["frames"][1]["start_clock"]  # second frame
    clock = f0 + (33 + 10) * 800 + 48 + 20  # line 10, pixel 20 of the picture
    cap = video.copy()
    cap[clock] ^= 0x04
    cap[clock + 1] ^= 0x44
    c = compare.compare(cap, ref, build_json=None)
    rep = compare.diff_report(cap, video, c, reference_meta=meta)
    assert rep["first_diff_clock"] == clock and rep["diff_clocks"] == 2
    assert rep["location"] == {"frame": 1, "region": "active", "line": 10, "pixel": 20}
    assert rep["bit_histogram"] == {2: 2, 6: 1}
    assert rep["first_diffs"][0] == {
        "clock": clock,
        "reference": int(video[clock]),
        "capture": int(cap[clock]),
    }
    assert len(rep["first_diffs"]) == 2


def test_diff_report_blanking_and_before_the_first_frame(ref, video):
    meta = json.loads((ref / "reference.json").read_text())
    cap = video.copy()
    start = meta["frames"][0]["start_clock"]
    cap[start + 5] ^= 1  # the first line of frame 0 is vertical blanking
    rep = compare.diff_report(cap, video, compare.compare(cap, ref, build_json=None), reference_meta=meta)
    assert rep["location"]["region"] == "blanking" and rep["location"]["frame"] == 0
    cap = video.copy()
    cap[3] ^= 1
    rep = compare.diff_report(cap, video, compare.compare(cap, ref, build_json=None), reference_meta=meta)
    assert rep["location"] == {"frame": None, "region": "outside frames"}


def test_diff_report_first_16_and_no_meta(ref, video):
    cap = video.copy()
    cap[1000:1040] ^= 0x01
    rep = compare.diff_report(cap, video, compare.compare(cap, ref, build_json=None))
    assert rep["diff_clocks"] == 40 and len(rep["first_diffs"]) == 16
    assert rep["bit_histogram"][0] == 40 and "location" not in rep


def test_diff_report_applies_the_offset(ref, video):
    cap = np.concatenate([np.zeros(4, np.uint8), video])
    cap[4 + 777] ^= 0x20
    c = compare.compare(cap, ref, build_json=None, offset=4)
    rep = compare.diff_report(cap, video, c)
    assert rep["first_diff_clock"] == 777 and rep["first_diffs"][0]["capture"] == int(cap[4 + 777])


def test_write_diff_images(ref, video, tmp_path):
    meta = json.loads((ref / "reference.json").read_text())
    clock = meta["frames"][1]["start_clock"] + (33 + 100) * 800 + 48 + 300
    cap = video.copy()
    cap[clock] ^= 0x01  # R1: red 2-bit value changes
    out = compare.write_diff_images(cap, ref, tmp_path / "diff")
    assert out["frame"] == 1 and out["capture_frame"] == 1 and out["note"] is None
    paths = {k: Path(v) for k, v in out["files"].items()}
    assert set(paths) == {"reference", "capture", "difference"}
    for p in paths.values():
        assert p.exists() and Image.open(p).size == (640, 480)
    d = np.asarray(Image.open(paths["difference"]).convert("RGB"))
    white = np.argwhere(d.any(axis=2))
    assert white.tolist() == [[100, 300]] and tuple(d[100, 300]) == (255, 255, 255)
    assert d.sum() == 255 * 3


def test_write_diff_images_when_the_capture_has_no_sync(ref, tmp_path):
    out = compare.write_diff_images(np.zeros(2_000_000, np.uint8), ref, tmp_path / "d2")
    assert "no frames" in out["note"] and set(out["files"]) == {"reference"}
    assert Path(out["files"]["reference"]).exists()


def test_write_diff_images_with_an_offset(ref, video, tmp_path):
    cap = np.concatenate([np.zeros(6, np.uint8), video])
    out = compare.write_diff_images(cap, ref, tmp_path / "d3", offset=6)
    d = np.asarray(Image.open(out["files"]["difference"]))
    assert not d.any()


# ---- CLI


def run_cli(*argv):
    p = argparse.ArgumentParser()
    compare.add_parser(p.add_subparsers(dest="command"))
    args = p.parse_args(["compare", *map(str, argv)])
    return args.func(args)


def test_cli_exact_and_mismatch(ref, video, tmp_path, capsys):
    raw = tmp_path / "cap.bin"
    raw.write_bytes(video.tobytes())
    assert run_cli(raw, ref) == 0
    assert "exact" in capsys.readouterr().out
    bad = video.copy()
    bad[5000] ^= 0x01
    gz = tmp_path / "cap.bin.gz"
    gz.write_bytes(gzip.compress(bad.tobytes(), 1))
    assert run_cli(gz, ref, "--diff-dir", tmp_path / "dd") == 1
    out = capsys.readouterr().out
    assert "mismatch" in out and "5000" in out
    assert (tmp_path / "dd").is_dir()


def test_cli_offset_and_build_json(ref, video, tmp_path, capsys):
    shifted = tmp_path / "s.bin"
    shifted.write_bytes(np.concatenate([np.zeros(3, np.uint8), video]).tobytes())
    assert run_cli(shifted, ref) == 1
    assert "find_offset: 3" in capsys.readouterr().out  # the diagnosis names the shift
    assert run_cli(shifted, ref, "--offset", 3) == 1  # exact at a non-zero offset is still a failure
    assert "exact" in capsys.readouterr().out
    bad = write_build(tmp_path / "b.json", commit="other")
    assert run_cli(shifted, ref, "--build-json", bad) == 1
    assert "refused" in capsys.readouterr().out


# ---- a real reference, made here with the real toolchain

try:
    for _tool in ("verilator", "yosys", "nextpnr-ice40", "icepack"):
        tools.tool(_tool)
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate a tool means skip
    _MISSING = f"toolchain not available: {e}"


@pytest.mark.needs_tools
@pytest.mark.skipif(bool(_MISSING), reason=_MISSING)
def test_a_real_reference_against_its_own_dump(tmp_path, monkeypatch):
    """Simulates the synthetic VGA design in tmp_path (nothing is read from the repository's work/), cuts its
    frames and compares the dump with the reference made from it: exact."""
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    repo = init(tmp_path / "proj")
    text = (Path(__file__).parent / "data" / "tt_um_synth_vga.v").read_text()
    commit = commit_files(repo, {"src/tt_um_synth_vga.v": text})
    d = Design(
        id="tt99/tt_um_synth_vga", shuttle="tt99", macro="tt_um_synth_vga", title="", author="", repo=str(repo),
        commit=commit, top_module="tt_um_synth_vga", source_files=("tt_um_synth_vga.v",), clock_hz=25_175_000,
        clock_declared=True, pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None,
        qspi=None, ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    r = reference.make_reference(d, 1_300_000)
    assert r.status == "ok", r
    ref_dir = paths.design_dir("reference", d.id)
    meta = json.loads((ref_dir / "reference.json").read_text())
    assert meta["schema"] == 2
    cap = frames.load_dump(ref_dir / meta["dump"])
    c = compare.compare(cap, ref_dir, build_json=None)
    assert c.verdict == "exact" and c.compared_clocks == 1_300_000
