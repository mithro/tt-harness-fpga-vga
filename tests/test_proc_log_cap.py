# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""A tool that talks without end cannot fill the disk or the host's memory, and what the parsers need
survives the cap."""

import tracemalloc

from test_build import YOSYS, fake_tool, make_design, make_fetched, rec, toolchain  # noqa: F401

from tthfv import build, paths, proc

BIG = 200_000_000


def test_a_default_run_logged_caps_the_log_and_the_memory_of_a_200_mb_output(tmp_path):
    log = tmp_path / "x.log"
    tracemalloc.start()
    r = proc.run_logged(["sh", "-c", f"yes 0123456789abcdef | head -c {BIG}; echo THE-END"], 300, log)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert r.rc == 0
    assert log.stat().st_size < proc.LOG_CAP + proc.LOG_TAIL + 500
    assert peak < 64 << 20, peak
    assert log.read_text().rstrip().endswith("THE-END") and "[output truncated:" in log.read_text()
    assert len(r.text) < proc.LOG_CAP + proc.LOG_TAIL + 500


def test_lines_the_parsers_look_for_survive_in_the_part_that_was_left_out(tmp_path):
    log = tmp_path / "x.log"
    script = (
        "yes 0123456789abcdef | head -c 3000000; echo 'ERROR: line 7: syntax error, unexpected TOK_ID'; "
        "yes 0123456789abcdef | head -c 3000000; echo FINAL-STATISTICS"
    )
    r = proc.run_logged(["sh", "-c", script], 60, log, watch=("syntax error",))
    assert "syntax error, unexpected TOK_ID" in r.text
    assert "syntax error, unexpected TOK_ID" in log.read_text()
    assert r.text.rstrip().endswith("FINAL-STATISTICS")


def test_a_match_split_across_two_reads_is_found(tmp_path):
    log = tmp_path / "x.log"
    script = "yes a | head -c 1048000; printf 'xx syntax'; sleep 0.3; printf ' error here\\n'; yes b | head -c 500000"
    r = proc.run_logged(["sh", "-c", script], 60, log, watch=("syntax error",))
    assert "xx syntax error here" in r.text


def test_the_build_still_parses_a_yosys_syntax_error_in_the_middle_of_a_huge_log(toolchain, rec, tmp_path):
    huge = YOSYS.replace(
        'cat "$FAKE_YOSYS_LOG"',
        'case "$(cat "$2")" in *sv2v.v*) cat "$FAKE_YOSYS_LOG"; exit 0;; esac\n'
        "yes 0123456789abcdef | head -c 3000000; echo 'ERROR: syntax error near foo'\n"
        "yes 0123456789abcdef | head -c 3000000; exit 1",
    )
    fake_tool(toolchain, "yosys", huge)
    d = make_design(source_files=("a.v",))
    r = build.build(d, make_fetched(tmp_path, design=d))
    # the retry with -sv happens only when "syntax error" is seen in a .v build: it was, though it was cut
    yosys_args = (rec / "yosys.args").read_text().splitlines()
    assert len(yosys_args) >= 2, yosys_args
    assert r.status == "synth-fail"
    assert (paths.design_dir("build", d.id) / "yosys-default.log").stat().st_size < proc.LOG_CAP + 200_000


def test_the_build_reads_the_nextpnr_timing_summary_from_the_end_of_a_huge_log(toolchain, rec, tmp_path):
    from test_build import NEXTPNR

    text = NEXTPNR.replace(
        'echo "$key" >> "$FAKE_REC/pnr.order"',
        'echo "$key" >> "$FAKE_REC/pnr.order"\nyes 0123456789abcdef | head -c 5000000',
    )
    fake_tool(toolchain, "nextpnr-ice40", text)
    d = make_design()
    r = build.build(d, make_fetched(tmp_path, design=d))
    assert r.status == "ok" and r.fmax_hz, r
    log = paths.design_dir("build", d.id) / "nextpnr-default-1.log"
    assert log.stat().st_size < proc.LOG_CAP + 200_000
