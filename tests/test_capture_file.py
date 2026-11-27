# SPDX-License-Identifier: Apache-2.0
"""tthfv.board.capture_file: a vgacap capture stream read into one uo_out byte per project clock, with every
gap the board recorded refused rather than stitched over."""

import numpy as np
import pytest
from ttcap.boards import RP2040_TT06, RP2350_DBV3
from vgacap.stream import Header, Writer
from vgacap.synth import to_rp2040_layout
from vgacap_files import CHUNK, header_bytes, write_capture

from tthfv.board.capture_file import CaptureError, CaptureGap, load_capture
from tthfv.errors import UserError


@pytest.fixture
def samples():
    return np.random.default_rng(1).integers(0, 256, 3 * CHUNK + 123, dtype=np.uint8)


def test_raw_capture_is_one_uo_out_byte_per_clock(tmp_path, samples):
    path = write_capture(tmp_path / "c.vgacap", samples, design="tt_um_vgacal_bars", clock_hz=500_000)
    cap = load_capture(path)
    assert cap.uo_out.dtype == np.uint8 and cap.uo_out.ndim == 1
    assert np.array_equal(cap.uo_out, samples)
    assert cap.samples == len(samples)
    assert cap.clock_hz == 500_000
    assert cap.design == "tt_um_vgacal_bars"
    assert cap.board_profile == "rp2350-dbv3"
    assert cap.board is None  # ttcap does not record the board's slug unless --desc says it
    assert cap.edge == "falling"
    assert (cap.overruns, cap.dropped, cap.rxstall) == (0, 0, 0)
    assert cap.summary == "overruns=0 rxstall=0 sysclk_hz=150000000"
    assert cap.sysclk_hz == 150_000_000
    assert cap.first_sample_clock is None  # the stream does not say how long after reset release it starts


def test_board_named_in_the_desc_is_carried(tmp_path, samples):
    path = write_capture(tmp_path / "c.vgacap", samples, desc="board=fpga-4")
    assert load_capture(path).board == "fpga-4"


def test_rp2040_window_is_mapped_to_uo_out_and_ui_in_dropped(tmp_path, samples):
    # The RP2040's 12-bit window holds uo_out 0-3, ui_in 4-7 (here 0xA), uo_out 4-7.
    wide = to_rp2040_layout(samples)
    path = write_capture(tmp_path / "c.vgacap", wide, profile=RP2040_TT06)
    cap = load_capture(path)
    assert np.array_equal(cap.uo_out, samples)
    assert cap.board_profile == "rp2040-tt06map"


def test_rle_runs_are_expanded_losslessly(tmp_path):
    pairs = [(0x80, 5), (0x81, 1), (0x00, 300), (0xFF, 2)]
    path = write_capture(tmp_path / "c.vgacap", None, parts=[("rle", pairs)])
    want = np.concatenate([np.full(r, v, np.uint8) for v, r in pairs])
    assert np.array_equal(load_capture(path).uo_out, want)


def test_an_inline_overrun_is_refused_naming_where(tmp_path, samples):
    parts = [("raw", samples[: 2 * CHUNK]), ("time", CHUNK, "overrun"), ("raw", samples[2 * CHUNK :])]
    path = write_capture(
        tmp_path / "c.vgacap", None, parts=parts, summary="overruns=1 rxstall=0", summary_dropped=CHUNK
    )
    with pytest.raises(CaptureGap) as e:
        load_capture(path)
    msg = str(e.value)
    assert "overrun" in msg and f"sample {2 * CHUNK}" in msg and f"{CHUNK} samples dropped" in msg
    assert isinstance(e.value, UserError)


def test_a_dropped_count_in_any_time_item_is_refused(tmp_path, samples):
    parts = [("raw", samples[:CHUNK]), ("time", 7, "something else"), ("raw", samples[CHUNK:])]
    path = write_capture(tmp_path / "c.vgacap", None, parts=parts)
    with pytest.raises(CaptureGap, match=f"sample {CHUNK}"):
        load_capture(path)


def test_closing_summary_with_overruns_is_refused(tmp_path, samples):
    # No inline report (a trailer from a board whose interim chunks were lost, say): the totals still refuse it.
    path = write_capture(tmp_path / "c.vgacap", samples, summary="overruns=2 rxstall=0 sysclk_hz=1")
    with pytest.raises(CaptureGap, match="overruns=2"):
        load_capture(path)


def test_closing_summary_with_dropped_samples_is_refused(tmp_path, samples):
    path = write_capture(tmp_path / "c.vgacap", samples, summary="overruns=0 rxstall=0", summary_dropped=5)
    with pytest.raises(CaptureGap, match="5 samples dropped"):
        load_capture(path)


def test_rx_fifo_stall_is_refused(tmp_path, samples):
    path = write_capture(tmp_path / "c.vgacap", samples, summary="overruns=0 rxstall=1 sysclk_hz=1")
    with pytest.raises(CaptureGap, match="rxstall=1"):
        load_capture(path)


def test_a_board_error_is_refused(tmp_path, samples):
    path = write_capture(
        tmp_path / "c.vgacap", samples[:10], summary="error=dma write-addr register offset mismatch"
    )
    with pytest.raises(CaptureError, match="error=dma"):
        load_capture(path)


def test_a_capture_without_its_closing_summary_is_refused(tmp_path, samples):
    # The summary is the only place the board reports RXSTALL (capture_rp2.py), and it is missing exactly when
    # ttcap's host gave up on the board (it went quiet, or the framing broke): such a capture cannot show it
    # lost nothing. No flag overrides this.
    path = write_capture(tmp_path / "c.vgacap", samples, summary=None)
    with pytest.raises(CaptureGap, match="no closing summary"):
        load_capture(path)


def test_a_summary_that_is_not_the_last_item_does_not_count(tmp_path, samples):
    parts = [
        ("raw", samples[:CHUNK]),
        ("time", 0, "overruns=0 rxstall=0 sysclk_hz=1"),
        ("raw", samples[CHUNK:]),
    ]
    path = write_capture(tmp_path / "c.vgacap", None, parts=parts, summary=None)
    with pytest.raises(CaptureGap, match="no closing summary"):
        load_capture(path)


def test_a_permuted_8_bit_signal_map_is_mapped_to_uo_out(tmp_path, samples):
    # 8-bit samples whose bits are not in the Tiny VGA order: the fast path must not take them as uo_out.
    smap = (0, 1, 2, 3, 4, 5, 6, 7)  # HSYNC in sample bit 0, VSYNC in 1, R1 in 2, ...
    tiny = (7, 3, 0, 4, 1, 5, 2, 6)  # the uo_out bit of each signal
    permuted = np.zeros_like(samples)
    for k in range(8):
        permuted |= ((samples >> tiny[k]) & 1) << smap[k]
    path = tmp_path / "c.vgacap"
    with path.open("wb") as fp:
        w = Writer(fp, Header(sample_bits=8, samples_per_word=4, mode=0, clock_hz=1, signal_map=smap))
        w.raw([int(s) for s in permuted])
        w.time(0, 0, 0, "overruns=0 rxstall=0")
    assert not np.array_equal(permuted, samples)
    assert np.array_equal(load_capture(path).uo_out, samples)


def test_a_malformed_stream_is_a_clear_error(tmp_path, samples):
    path = write_capture(tmp_path / "c.vgacap", samples)
    path.write_bytes(path.read_bytes()[:-3])
    with pytest.raises(CaptureError, match=str(path)):
        load_capture(path)


def test_a_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(CaptureError, match="cannot read"):
        load_capture(tmp_path / "nope.vgacap")


def test_two_headers_are_refused(tmp_path, samples):
    path = write_capture(tmp_path / "c.vgacap", samples)
    path.write_bytes(path.read_bytes() * 2)
    with pytest.raises(CaptureError, match="2 VGCH headers"):
        load_capture(path)


def test_frame_chunks_are_refused(tmp_path):
    # Whole-frame / window capture (frame_rp2.py) is not one sample per clock from a start.
    path = tmp_path / "c.vgacap"
    with path.open("wb") as fp:
        w = Writer(fp, Header(sample_bits=8, samples_per_word=4, mode=0, clock_hz=25_000_000))
        w.frame(0, 0, 1, 800, [0] * 800)
    with pytest.raises(CaptureError, match="FRAM"):
        load_capture(path)


def test_a_header_not_sampled_on_the_project_clock_is_refused(tmp_path):
    path = tmp_path / "c.vgacap"
    with path.open("wb") as fp:
        w = Writer(fp, Header(sample_bits=8, samples_per_word=4, mode=1, clock_hz=1))
        w.raw([1, 2, 3, 4])
    with pytest.raises(CaptureError, match="mode 1"):
        load_capture(path)


def test_a_version_2_clock_bit_stream_is_refused(tmp_path):
    path = tmp_path / "c.vgacap"
    with path.open("wb") as fp:
        w = Writer(fp, Header(version=2, sample_bits=9, samples_per_word=2, mode=0, clock_hz=1,
                              samples_per_clock=2, clock_bit=8, clock_first=0))  # fmt: skip
        w.runs([(1, 2), (2, 2)])
    with pytest.raises(CaptureError, match="2 samples per clock"):
        load_capture(path)


def test_an_absent_signal_is_refused(tmp_path, samples):
    path = tmp_path / "c.vgacap"
    with path.open("wb") as fp:
        w = Writer(fp, Header(sample_bits=8, samples_per_word=4, mode=0, clock_hz=1,
                              signal_map=(7, 3, 0, 4, 1, 5, 2, 0xFF)))  # fmt: skip
        w.raw([1, 2, 3, 4])
    with pytest.raises(CaptureError, match="B0"):
        load_capture(path)


def test_the_header_bytes_helper_is_what_ttcap_writes():
    # The tests build files the way `ttcap capture` does: version 1, the board's sample layout, desc with
    # project=, clock=, edge= and profile=.
    h = header_bytes(RP2350_DBV3, design="tt_um_y", clock_hz=123)
    assert b"project=tt_um_y clock=123 mode=extclk edge=falling profile=rp2350-dbv3" in h


# -- a real capture: the script `ttcap capture` sends to the board, run on vgacap's board emulator


COUNT = 251  # a counting design whose period divides no chunk length, so a gap shows as a jump


def _emulated_capture(tmp_path, period: int, stop_after: int):
    from ttcap import mp
    from ttcap.capture import capture_cfg
    from ttcap.emu import Board, Waveform

    profile = RP2350_DBV3
    wave = Waveform(bytes(range(COUNT)), profile.uo_gpios, profile.clk_gpio, period=period)
    board = Board(wave)
    board.stop_after_bytes = stop_after
    out = board.run_script(mp.build(capture_cfg(profile), "capture_rp2.py"))
    path = tmp_path / "emu.vgacap"
    path.write_bytes(header_bytes(profile, clock_hz=board.sysclk_hz // period) + out)
    return path


def test_an_emulated_board_capture_is_contiguous(tmp_path):
    cap = load_capture(_emulated_capture(tmp_path, period=266, stop_after=4 * 16_400))
    assert cap.samples >= 3 * CHUNK and cap.overruns == 0 and cap.rxstall == 0
    steps = (cap.uo_out[1:].astype(int) - cap.uo_out[:-1]) % COUNT
    assert np.all(steps == 1)


def test_an_emulated_board_that_cannot_keep_up_is_refused(tmp_path):
    # 2.66 MHz of one-byte samples is ~3.8x what the emulated USB link carries (vgacap's own overrun test).
    with pytest.raises(CaptureGap, match="overrun"):
        load_capture(_emulated_capture(tmp_path, period=50, stop_after=200_000))
