# SPDX-License-Identifier: Apache-2.0
"""The build takes its figures from nextpnr's JSON report and yosys's stat file and fails closed when they are
wrong; the tools' log text can name a capacity failure but never change an outcome. Fake tools."""

import json

import pytest
import test_build
from test_build import LOGS, run

from tthfv import build, paths

toolchain = test_build.toolchain  # the fixtures of the fake toolchain
UTIL = {"ICESTORM_LC": {"used": 100, "available": 5280}}


def raw_report(fmax, util=UTIL) -> str:
    return json.dumps({"fmax": fmax, "utilization": util, "critical_paths": []})


def good(**clocks) -> dict:
    return {name: {"achieved": mhz, "constraint": 20.0} for name, mhz in clocks.items()}


def test_missing_report_is_a_pnr_fail(toolchain, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_NO_REPORT", "1")
    _d, r = run(tmp_path)
    assert r.status == "pnr-fail" and "report" in r.reason and "missing" in r.reason
    assert r.bitstream is None and r.bitstream_sha256 is None


@pytest.mark.parametrize(
    "text,why",
    [
        ('{"fmax": {"clk": ', "not valid JSON"),
        (" ", "not valid JSON"),
        ('{"fmax": {"clk": {"achieved": NaN, "constraint": 20}}, "utilization": {}}', "not valid JSON"),
        (raw_report(good(clk=-1.0)), "finite positive"),
        (raw_report(good(clk=0)), "finite positive"),
        (raw_report({"clk": {"achieved": 30.0}}), "constraint"),
        (raw_report(good(clk=30.0), util={}), "ICESTORM_LC"),
        (json.dumps({"fmax": {}}), "utilization"),
        (
            json.dumps(
                {
                    "fmax": {},
                    "utilization": UTIL,
                    "critical_paths": [{"from": "posedge clk", "to": "posedge clk", "path": []}],
                }
            ),
            "no clock",
        ),
        (json.dumps({"fmax": {}, "utilization": UTIL}), "critical_paths"),
        (raw_report({"clk": {"achieved": 30.0, "constraint": 12.0}}), "constrained to"),
        (raw_report({f"c{i}": {"achieved": 30.0, "constraint": 20.0} for i in range(65)}), "at most 64"),
    ],
)
def test_a_bad_report_is_never_ok(toolchain, monkeypatch, tmp_path, text, why):
    monkeypatch.setenv("FAKE_REPORT_RAW", text)
    _d, r = run(tmp_path)
    assert r.status == "pnr-fail", r
    assert why in r.reason
    assert r.bitstream is None and r.clocks == {}
    assert r.attempts[0]["status"] == "pnr-fail"


def test_a_good_report_is_ok_and_names_are_recorded_clean_and_capped(toolchain, monkeypatch, tmp_path):
    name = "a\nb\x1b[31m" + "z" * 300
    monkeypatch.setenv("FAKE_REPORT_RAW", raw_report(good(**{"clk$SB_IO_IN_$glb_clk": 30.0, name: 25.0})))
    d, r = run(tmp_path)
    assert r.status == "ok", r.reason
    assert all(len(n) <= 200 and "\n" not in n and "\x1b" not in n for n in r.clocks)
    on_disk = json.loads((paths.design_dir("build", d.id) / "build.json").read_text())
    assert list(on_disk["clocks"]) == list(r.clocks)


def test_a_failing_clock_with_an_awkward_name_is_a_timing_fail(toolchain, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "FAKE_REPORT_RAW", raw_report(good(**{"clk$SB_IO_IN_$glb_clk": 30.0, "cd'x_$glb_clk": 7.89}))
    )
    _d, r = run(tmp_path, max_attempts=1)
    assert r.status == "timing-fail" and "cd'x_$glb_clk" in r.reason and "7.89" in r.reason


def test_the_verdict_word_in_the_log_is_ignored(toolchain, monkeypatch, tmp_path):
    # the log (what a design can write into) says PASS for a clock the report says is too slow
    log = tmp_path / "pnr.log"
    log.write_text("Info: Max frequency for clock 'clk': 999.00 MHz (PASS at 20.00 MHz)\n")
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_REPORT_RAW", raw_report(good(clk=10.0)))
    _d, r = run(tmp_path, max_attempts=1)
    assert r.status == "timing-fail" and r.fmax_hz == 10_000_000


def test_an_injected_capacity_line_in_a_successful_run_is_not_a_no_fit(toolchain, monkeypatch, tmp_path):
    log = tmp_path / "pnr.log"
    log.write_text(
        (LOGS / "nextpnr.log").read_text()
        + "ERROR: Unable to place cell 'x', no BELs remaining to implement cell type 'ICESTORM_LC'\n"
        + "Info: \t ICESTORM_LC: 9000/ 5280 170%\n"
    )
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_REPORT_RAW", raw_report(good(clk=30.0)))
    _d, r = run(tmp_path)
    assert r.status == "ok", r.reason
    assert r.lut_percent == pytest.approx(1.9)  # from the report, not the forged log


def test_an_injected_pass_in_a_failed_run_is_not_ok(toolchain, monkeypatch, tmp_path):
    log = tmp_path / "pnr.log"
    log.write_text("Info: Max frequency for clock 'clk': 999.00 MHz (PASS at 20.00 MHz)\nERROR: boom\n")
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_PNR_EXIT", "1")
    monkeypatch.setenv("FAKE_REPORT_RAW", raw_report(good(clk=999.0)))  # even a report left behind
    _d, r = run(tmp_path)
    assert r.status == "pnr-fail" and "exit status 1" in r.reason and r.bitstream is None


def test_a_capacity_line_is_believed_only_after_a_non_zero_exit(toolchain, monkeypatch, tmp_path):
    log = tmp_path / "pnr.log"
    log.write_text(
        "ERROR: Unable to place cell 'x', no BELs remaining to implement cell type 'ICESTORM_LC'\n"
    )
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_PNR_EXIT", "255")
    _d, r = run(tmp_path)
    assert r.status == "no-fit"


@pytest.mark.parametrize(
    "bad", ["", "{", "[]", '{"design": {}}', '{"design": {"num_cells_by_type": {"SB_LUT4": -3}}}']
)
def test_bad_yosys_statistics_are_a_synth_fail(toolchain, monkeypatch, tmp_path, bad):
    monkeypatch.setenv("FAKE_STAT_BAD", bad or " ")
    _d, r = run(tmp_path)
    assert r.status == "synth-fail" and "statistics" in r.reason
    assert r.bitstream is None


def test_cells_come_from_the_stat_file_not_from_the_log(toolchain, monkeypatch, tmp_path):
    forged = tmp_path / "yosys.log"
    forged.write_text("=== design hierarchy ===\n   99999 SB_LUT4\n   88888 SB_DFF\n")
    monkeypatch.setenv(
        "FAKE_STAT_BAD", json.dumps({"design": {"num_cells_by_type": {"SB_LUT4": 7, "SB_DFFSR": 3}}})
    )
    monkeypatch.setenv("FAKE_YOSYS_LOG", str(forged))
    _d, r = run(tmp_path)
    assert r.status == "ok", r.reason
    assert r.cells["SB_LUT4"] == 7 and r.cells["SB_DFF"] == 3


def test_a_design_without_registers_needs_no_clock(toolchain, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_STAT_BAD", json.dumps({"design": {"num_cells_by_type": {"SB_LUT4": 7}}}))
    monkeypatch.setenv("FAKE_REPORT_RAW", raw_report({}))
    _d, r = run(tmp_path)
    assert r.status == "ok" and r.clocks == {} and r.fmax_hz is None


def test_the_stamp_was_bumped_for_the_new_source_of_figures():
    assert build._STAMP_VERSION >= 5
