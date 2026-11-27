# SPDX-License-Identifier: Apache-2.0
"""Locating a hardware capture in its reference (compare.locate_capture), comparing it over the overlap, and
`tthfv board compare-capture`. A capture starts an unknown number of clocks after reset release (thousands
at 500 kHz, well under a frame), so its first ANCHOR_CLOCKS samples are searched for in the reference's first
max_start_clocks."""

import json

import numpy as np
import pytest
from test_compare import KEY, write_ref
from test_frames import make_dump
from vgacap_files import CHUNK, write_capture

from tthfv import cli, compare, paths, reference

PROFILE = paths.REPO_ROOT / "boards" / "fpga-4.json"
DESIGN = "t/tt_um_x"


@pytest.fixture(scope="module")
def noise():
    """A reference with no repeats: every anchor has one place."""
    return np.random.default_rng(7).integers(0, 256, 1_000_000, dtype=np.uint8)


@pytest.fixture(scope="module")
def video():
    # A still picture: the signal repeats every frame (420,000 clocks). Four frames, so that a capture starting
    # a few thousand clocks in still holds the three vsyncs the frame cutter needs to find frames in it.
    return make_dump(nframes=4)


# ---- locate_capture


@pytest.mark.parametrize("start", [0, 3, 5_000, 300_000])
def test_a_capture_cut_out_of_the_reference_is_found_where_it_was_cut(noise, start):
    cap = noise[start : start + 200_000].copy()
    loc = compare.locate_capture(cap, noise, max_start_clocks=840_000)
    assert (loc.start_clock, loc.candidates) == (start, 1)
    assert loc.anchor_clocks == compare.ANCHOR_CLOCKS == 4096 and loc.max_start_clocks == 840_000


def test_a_periodic_reference_takes_the_earliest_match_and_counts_the_others(video):
    loc = compare.locate_capture(video[5_000:600_000].copy(), video, max_start_clocks=840_000)
    assert loc.start_clock == 5_000 and loc.candidate_diffs == 0
    # The test picture repeats every 4 lines (3,200 clocks), so the anchor also matches 4, 8, ... lines higher
    # up, and in the next frame: those disagree once the sync comes round, and the earliest exact place wins.
    assert loc.earliest_clock == 1_800 and loc.candidates > 2


def test_with_a_flipped_bit_the_place_that_differs_least_is_chosen(video):
    cap = video[5_000:600_000].copy()
    cap[100_000] ^= 0x01
    loc = compare.locate_capture(cap, video, max_start_clocks=840_000)
    assert loc.start_clock == 5_000 and loc.candidate_diffs == 1


def test_a_frame_periodic_reference_takes_the_earliest_of_equal_matches(noise):
    frame = noise[:420_000]
    ref = np.concatenate([frame, frame, frame])
    loc = compare.locate_capture(ref[5_000:400_000].copy(), ref, max_start_clocks=840_000)
    assert (loc.start_clock, loc.candidates, loc.candidate_diffs) == (5_000, 2, 0)  # 5,000 and 425,000


def test_no_match_is_none(noise):
    cap = np.random.default_rng(8).integers(0, 256, 10_000, dtype=np.uint8)
    loc = compare.locate_capture(cap, noise, max_start_clocks=840_000)
    assert (loc.start_clock, loc.candidates) == (None, 0)


def test_only_the_window_is_searched(noise):
    cap = noise[900_000:].copy()
    assert compare.locate_capture(cap, noise, max_start_clocks=840_000).start_clock is None
    assert compare.locate_capture(cap, noise, max_start_clocks=900_001).start_clock == 900_000
    assert compare.locate_capture(cap, noise, max_start_clocks=900_000).start_clock is None


def test_a_capture_shorter_than_the_anchor_is_refused(noise):
    with pytest.raises(compare.Refused, match="4096"):
        compare.locate_capture(noise[:100].copy(), noise, max_start_clocks=840_000)


def test_the_default_window_is_two_frames_of_the_reference():
    assert compare.start_window({"lines": 525, "line_clocks": 800}) == 840_000
    assert compare.start_window({"lines": 628, "line_clocks": 1056}) == 2 * 628 * 1056
    assert compare.start_window({}) == compare.DEFAULT_MAX_START_CLOCKS == 840_000


# ---- compare over the overlap


def test_overlap_only_compares_from_the_capture_start(tmp_path, noise):
    ref = write_ref(tmp_path / "ref", noise)
    cap = noise[5_000:700_000].copy()
    assert compare.compare(cap, ref, build_json=None, offset=-5_000).verdict == "mismatch"  # 5,000 missing
    c = compare.compare(cap, ref, build_json=None, offset=-5_000, overlap_only=True)
    assert c.verdict == "exact" and c.compared_clocks == len(cap) and c.offset == -5_000
    assert "5000 to 700000" in c.reason
    cap[123_456] ^= 0x40
    c = compare.compare(cap, ref, build_json=None, offset=-5_000, overlap_only=True)
    assert c.verdict == "mismatch" and c.first_diff_clock == 128_456 and c.diff_clocks == 1
    assert c.per_bit_first_diff == {6: 128_456}


# ---- the CLI


@pytest.fixture
def work(tmp_path, monkeypatch, video):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    prof = reference.load_profile(str(PROFILE))
    key = {**KEY, "profile": {"name": prof.name, "data": prof.data}}
    write_ref(reference.reference_dir(DESIGN, prof.name), video, key=key, phase_sensitive=False)
    return tmp_path / "work"


def run(*argv) -> int:
    return cli.main(["board", "compare-capture", *map(str, argv)])


def result(work) -> dict:
    return json.loads((work / "validate" / DESIGN / "fpga-4" / "result.json").read_text())


def test_cli_exact_capture(work, tmp_path, video, capsys):
    path = write_capture(tmp_path / "c.vgacap", video[5_000:500_000], design="tt_um_x")
    assert run(path, DESIGN) == 0
    out = capsys.readouterr().out
    assert "exact:" in out and "first sample is reference clock 5000" in out and "candidate place(s)" in out
    r = result(work)
    assert r["verdict"] == "exact"
    assert (r["capture_start_clock"], r["offset"], r["earliest_candidate_clock"]) == (5_000, -5_000, 1_800)
    assert r["candidates"] > 2  # the test picture repeats every 4 lines
    assert r["anchor_clocks"] == 4096 and r["max_start_clocks"] == 840_000
    assert r["capture_meta"]["samples"] == 495_000 and r["capture_meta"]["clock_hz"] == 500_000
    assert r["capture_meta"]["design"] == "tt_um_x" and r["capture_meta"]["overruns"] == 0
    assert r["comparison"]["compared_clocks"] == 495_000
    assert r["profile"] == "fpga-4"


def test_cli_flipped_bit(work, tmp_path, video, capsys):
    cap = video[5_000:].copy()  # three vsyncs: see the video fixture
    cap[200_000] ^= 0x01
    path = write_capture(tmp_path / "c.vgacap", cap)
    assert run(path, DESIGN) == 1
    out = capsys.readouterr().out
    assert "mismatch:" in out and "first differing clock 205000" in out
    r = result(work)
    assert r["verdict"] == "mismatch" and r["comparison"]["first_diff_clock"] == 205_000
    assert r["report"]["first_diffs"][0] == {
        "clock": 205_000,
        "reference": int(video[205_000]),
        "capture": int(cap[200_000]),
    }
    images = r["images"]["files"]
    assert set(images) == {"reference", "capture", "difference"}
    assert all((work / "validate" / DESIGN / "fpga-4" / f"{n}.png").is_file() for n in images)


def test_cli_no_match(work, tmp_path, video, capsys):
    cap = video[5_000:] ^ 0x77  # every colour bit wrong, the syncs right: frames, but no match
    path = write_capture(tmp_path / "c.vgacap", cap)
    assert run(path, DESIGN) == 1
    out = capsys.readouterr().out
    assert "no match" in out
    r = result(work)
    assert r["verdict"] == "no-match" and r["capture_start_clock"] is None and r["candidates"] == 0
    assert r["comparison"] is None
    assert (
        r["start_disagreement"]["first_diff_clock"] == 0
    )  # the first anchor sample against reference clock 0
    assert set(r["images"]["files"]) == {"reference", "capture", "difference"}  # the first captured frame
    assert r["images"]["capture_frame"] == 0


def test_cli_max_start_clocks_and_out(work, tmp_path, noise, video, capsys):
    path = write_capture(tmp_path / "c.vgacap", video[5_000:500_000])
    out_dir = tmp_path / "elsewhere"
    assert run(path, DESIGN, "--max-start-clocks", 1_000, "--out", out_dir) == 1
    assert "no match" in capsys.readouterr().out
    r = json.loads((out_dir / "result.json").read_text())
    assert r["max_start_clocks"] == 1_000 and r["verdict"] == "no-match"
    assert not (work / "validate").exists()


def test_cli_a_capture_with_a_gap_is_refused(work, tmp_path, video, capsys):
    v = video[5_000:200_000]
    parts = [("raw", v[: 2 * CHUNK]), ("time", CHUNK, "overrun"), ("raw", v[2 * CHUNK :])]
    path = write_capture(tmp_path / "c.vgacap", None, parts=parts)
    assert run(path, DESIGN) == 1
    assert f"at sample {2 * CHUNK}" in capsys.readouterr().err
    assert not (work / "validate").exists()


def test_cli_a_reference_of_another_profile_is_refused(work, tmp_path, video, capsys):
    other = tmp_path / "other.json"
    data = json.loads(PROFILE.read_text())
    data["usb_serial"] = "0000000000000000"
    other.write_text(json.dumps(data))
    write_ref(
        reference.reference_dir(DESIGN, "other"),
        video,
        key={**KEY, "profile": {"name": "fpga-4", "data": data}},
    )
    path = write_capture(tmp_path / "c.vgacap", video[5_000:500_000])
    assert run(path, DESIGN, "--profile", other) == 1
    assert "refused:" in capsys.readouterr().out
    assert result_at(work, "other")["verdict"] == "refused"


def result_at(work, profile: str) -> dict:
    return json.loads((work / "validate" / DESIGN / profile / "result.json").read_text())


def test_cli_a_missing_reference_is_refused(work, tmp_path, video, capsys):
    path = write_capture(tmp_path / "c.vgacap", video[5_000:500_000])
    assert run(path, "t/tt_um_none") == 1
    assert "refused:" in capsys.readouterr().out


def test_cli_end_to_end_with_the_board_emulator(work, tmp_path, video, capsys):
    """The script `ttcap capture` sends, run on vgacap's emulated RP2350 against the reference's own uo_out:
    the emulator starts sampling some clocks into the waveform, as a board does after reset release."""
    from ttcap import mp
    from ttcap.boards import RP2350_DBV3
    from ttcap.capture import capture_cfg
    from ttcap.emu import Board, Waveform
    from vgacap_files import header_bytes

    profile = RP2350_DBV3
    board = Board(Waveform(video.tobytes(), profile.uo_gpios, profile.clk_gpio, period=266))
    board.stop_after_bytes = 8 * 16_400
    out = board.run_script(mp.build(capture_cfg(profile), "capture_rp2.py"))
    path = tmp_path / "emu.vgacap"
    path.write_bytes(header_bytes(profile, design="tt_um_x", clock_hz=500_000) + out)
    assert run(path, DESIGN) == 0
    r = result(work)
    assert r["verdict"] == "exact" and r["capture_start_clock"] is not None
    assert r["comparison"]["compared_clocks"] >= 7 * CHUNK


# ---- the sampling edge. The reference's uo_out.bin byte for clock k is uo_out after the rising edge of clock k,
# before the falling edge (harness/ref_tb.cpp). ttcap's default falling-edge sample of clock k reads the pins at
# the end of that high phase, so it is the same byte for a design whose outputs change only on rising edges. A
# rising-edge sample is read within ns of the edge, before uo_out has settled: refused unless asked for.


def test_cli_a_rising_edge_capture_is_refused(work, tmp_path, video, capsys):
    path = write_capture(tmp_path / "c.vgacap", video[5_000:500_000], edge="rising")
    assert run(path, DESIGN) == 1
    err = capsys.readouterr().err
    assert "edge=rising" in err and "--allow-rising-edge" in err
    assert not (work / "validate").exists()


def test_cli_a_rising_edge_capture_with_the_flag_is_compared(work, tmp_path, video, capsys):
    path = write_capture(tmp_path / "c.vgacap", video[5_000:500_000], edge="rising")
    assert run(path, DESIGN, "--allow-rising-edge") == 0
    assert result(work)["capture_meta"]["edge"] == "rising"


def test_cli_a_capture_that_does_not_record_its_edge_says_so(work, tmp_path, video, capsys):
    from vgacap.stream import Header, Writer

    path = tmp_path / "c.vgacap"
    with path.open("wb") as fp:
        w = Writer(
            fp, Header(sample_bits=8, samples_per_word=4, mode=0, clock_hz=500_000, desc="project=tt_um_x")
        )
        w.raw([int(s) for s in video[5_000:60_000]])
        w.time(0, 0, 0, "overruns=0 rxstall=0")
    assert run(path, DESIGN) == 0
    assert "does not record the sampling edge" in capsys.readouterr().out


def test_cli_note_for_a_phase_sensitive_reference(work, tmp_path, video, capsys):
    ref_dir = reference.reference_dir(DESIGN, "fpga-4")
    meta = json.loads((ref_dir / "reference.json").read_text())
    meta["phase_sensitive"] = True
    (ref_dir / "reference.json").write_text(json.dumps(meta))
    path = write_capture(tmp_path / "c.vgacap", video[5_000:500_000])
    assert run(path, DESIGN) == 0
    out = capsys.readouterr().out
    assert "phase_sensitive" in out and "may legitimately differ" in out
