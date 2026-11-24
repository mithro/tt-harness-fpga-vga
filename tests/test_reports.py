# SPDX-License-Identifier: Apache-2.0
"""The report parsers: nextpnr's --report JSON and yosys's stat -json are read as data; the log is consulted
only to name a capacity failure."""

import copy
import json
from pathlib import Path

import pytest

from tthfv import reports

LOGS = Path(__file__).parent / "data" / "logs"
REAL = json.loads((LOGS / "nextpnr-report-bars.json").read_text())  # a real report, 25.175 MHz, one clock
MAIN = "clk$SB_IO_IN_$glb_clk"


def report(**changes) -> dict:
    doc = copy.deepcopy(REAL)
    doc.update(changes)
    return doc


def parse(doc, clock_hz=25_175_000, has_registers=True, raw: bytes | None = None):
    data = raw if raw is not None else json.dumps(doc).encode()
    return reports.parse_report(data, clock_hz=clock_hz, has_registers=has_registers)


def clock(achieved, constraint=25.174999237060547):
    return {"achieved": achieved, "constraint": constraint}


def test_the_real_report_is_read():
    r = parse(REAL)
    assert r.fmax_hz == 35_860_291  # in Hz, as many digits as the report has
    assert r.clocks[MAIN] == reports.Clock(35_860_291, 25_174_999, True)
    assert r.utilisation["ICESTORM_LC"] == (150, 5280)
    assert r.met and r.failing == []


def test_pass_is_decided_here_from_achieved_and_constraint_not_from_a_word():
    doc = report(fmax={MAIN: {**clock(20.0), "verdict": "PASS"}, "d": {**clock(30.0), "pass": False}})
    r = parse(doc)
    assert r.clocks[MAIN].passed is False and r.clocks["d"].passed is True
    assert r.failing == [MAIN] and not r.met


def test_equal_to_the_constraint_passes():
    assert parse(report(fmax={MAIN: clock(25.174999237060547)})).met


@pytest.mark.parametrize("name", ["cd'x", 'a"b', "back\\slash", "new\nline", "esc\x1b[31m", "é"])
def test_a_clock_name_is_just_a_string(name):
    r = parse(report(fmax={MAIN: clock(30.0), name: clock(10.0)}))
    assert len(r.clocks) == 2 and r.met is False
    (bad,) = r.failing
    assert "\n" not in bad and "\x1b" not in bad


def test_names_are_capped_and_stay_distinct():
    long = "x" * 300
    r = parse(report(fmax={MAIN: clock(30.0), long + "a": clock(30.0), long + "b": clock(10.0)}))
    assert all(len(n) <= reports.MAX_NAME for n in r.clocks) and len(r.clocks) == 3


def test_names_that_clean_to_the_same_string_stay_distinct():
    r = parse(report(fmax={"ab": clock(30.0), "a\nb": clock(10.0)}))
    assert len(r.clocks) == 2


def test_more_than_64_clocks_is_refused():
    many = {f"c{i}": clock(30.0) for i in range(reports.MAX_CLOCKS + 1)}
    with pytest.raises(reports.ReportError, match="at most 64"):
        parse(report(fmax=many))
    parse(report(fmax={f"c{i}": clock(30.0) for i in range(reports.MAX_CLOCKS)}))


@pytest.mark.parametrize(
    "raw",
    [b"", b"{", json.dumps(REAL).encode()[:500], b"[]", b"null", b"\xff\xfe", b"[" * 100000],
    ids=["empty", "open-brace", "truncated", "list", "null", "not-utf8", "deeply-nested"],
)
def test_unreadable_reports_are_refused(raw):
    with pytest.raises(reports.ReportError, match="not valid JSON|no 'fmax'"):
        parse(None, raw=raw)


@pytest.mark.parametrize("missing", ["fmax", "utilization"])
def test_a_missing_object_is_refused(missing):
    doc = report()
    del doc[missing]
    with pytest.raises(reports.ReportError, match=missing):
        parse(doc)


def test_the_fmax_and_utilization_objects_must_be_objects():
    with pytest.raises(reports.ReportError):
        parse(report(fmax=[1, 2]))
    with pytest.raises(reports.ReportError):
        parse(report(utilization="x"))


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), -float("inf"), -5.0, 0, 0.0, "30", None, True, [30], {"a": 1}],
    ids=["nan", "inf", "-inf", "negative", "zero", "zero-float", "string", "null", "bool", "list", "dict"],
)
def test_a_value_that_is_not_a_finite_positive_number_is_refused(bad):
    for key in ("achieved", "constraint"):
        entry = clock(30.0)
        entry[key] = bad
        raw = json.dumps({"fmax": {MAIN: entry}, "utilization": REAL["utilization"]}).encode()
        with pytest.raises(reports.ReportError, match="finite positive number|not a number|not valid JSON"):
            parse(None, raw=raw)


def test_nan_written_as_a_bare_word_is_refused():
    raw = b'{"fmax": {"clk": {"achieved": NaN, "constraint": 25.175}}, "utilization": {}}'
    with pytest.raises(reports.ReportError, match="not valid JSON"):
        parse(None, raw=raw)


def test_a_missing_value_is_refused():
    with pytest.raises(reports.ReportError):
        parse(report(fmax={MAIN: {"achieved": 30.0}}))
    with pytest.raises(reports.ReportError):
        parse(report(fmax={MAIN: 30.0}))


def path(src, dst, *delays):
    return {"from": src, "to": dst, "path": [{"delay": d, "type": "logic"} for d in delays]}


def test_no_clock_is_refused_for_a_design_with_registers_if_a_clock_path_exists():
    doc = report(fmax={}, critical_paths=[path("posedge clk", "posedge clk", 10.0)])
    with pytest.raises(reports.ReportError, match="no clock"):
        parse(doc, has_registers=True)
    r = parse(doc, has_registers=False)
    assert r.clocks == {} and r.fmax_hz is None and r.met


def test_no_clock_with_only_pin_paths_is_io_registers_and_is_noted():
    doc = report(
        fmax={}, critical_paths=[path("<async>", "posedge clk", 5.0), path("posedge clk", "<async>", 6.0)]
    )
    r = parse(doc, has_registers=True)
    assert r.clocks == {} and r.met and "no clock-to-clock path" in r.note
    with pytest.raises(reports.ReportError, match="critical_paths"):
        parse({k: v for k, v in report(fmax={}).items() if k != "critical_paths"}, has_registers=True)


def test_a_path_between_two_clocks_longer_than_one_period_fails_and_is_recorded():
    doc = report(
        fmax={MAIN: clock(80.0), "g": clock(32.0)},
        critical_paths=[
            path("posedge " + MAIN, "posedge g", 20.0, 22.0),  # 42 ns against 39.72
            path("posedge g", "posedge g", 30.0),  # same edge of one clock: fmax's business
            path("<async>", "posedge g", 90.0),  # a pin: not judged
            path("posedge g", "<async>", 90.0),
        ],
    )
    r = parse(doc)
    (c,) = r.cross_paths
    assert c.from_edge == "posedge " + MAIN and c.to_edge == "posedge g" and c.passed is False
    assert c.delay_ns == pytest.approx(42.0) and c.limit_ns == pytest.approx(39.72, abs=0.01)
    assert not r.met and r.failing_cross == [c] and r.failing == []
    assert c.record()["pass"] is False and c.record()["delay_ns"] == 42.0


def test_a_cross_path_within_one_period_passes():
    r = parse(
        report(
            fmax={MAIN: clock(80.0), "g": clock(32.0)}, critical_paths=[path("posedge a", "posedge b", 39.0)]
        )
    )
    assert r.met and r.cross_paths[0].passed


def test_opposite_edges_of_one_clock_get_half_a_period():
    r = parse(report(fmax={MAIN: clock(80.0)}, critical_paths=[path("posedge clk", "negedge clk", 20.0)]))
    (c,) = r.cross_paths
    assert c.limit_ns == pytest.approx(19.86, abs=0.01) and not c.passed and not r.met
    r = parse(report(fmax={MAIN: clock(80.0)}, critical_paths=[path("posedge clk", "negedge clk", 19.0)]))
    assert r.met


@pytest.mark.parametrize("bad", [float("nan"), -1.0, "5", None, True])
def test_a_bad_segment_delay_is_refused(bad):
    doc = report(critical_paths=[path("posedge a", "posedge b", 1.0)])
    doc["critical_paths"][0]["path"][0]["delay"] = bad
    with pytest.raises(reports.ReportError, match="delay|malformed|not valid JSON"):
        parse(None, raw=json.dumps(doc).encode())


def test_cross_path_names_are_cleaned_and_capped_and_there_are_at_most_64():
    long = "posedge " + "x" * 300 + "\n"
    r = parse(report(critical_paths=[path(long, "posedge b", 1.0)]))
    assert all(len(c.from_edge) <= reports.MAX_NAME and "\n" not in c.from_edge for c in r.cross_paths)
    many = [path(f"posedge a{i}", "posedge b", 1.0) for i in range(reports.MAX_CROSS + 1)]
    with pytest.raises(reports.ReportError, match="more than 64"):
        parse(report(critical_paths=many))
    parse(report(critical_paths=many[:-1]))


@pytest.mark.parametrize("hz,constraint", [(25_175_000, 12.0), (25_175_000, 26.0), (12_000_000, 25.175)])
def test_a_constraint_that_is_not_the_designs_clock_is_refused(hz, constraint):
    with pytest.raises(reports.ReportError, match="constrained to"):
        parse(report(fmax={MAIN: clock(30.0, constraint)}), clock_hz=hz)


def test_a_side_clock_with_another_constraint_is_refused_too():
    with pytest.raises(reports.ReportError, match="constrained to"):
        parse(report(fmax={MAIN: clock(30.0), "d": clock(30.0, 1.0)}))


def test_a_constraint_within_half_a_percent_is_accepted():
    parse(report(fmax={MAIN: clock(30.0, 25.175 * 1.004)}))
    with pytest.raises(reports.ReportError):
        parse(report(fmax={MAIN: clock(30.0, 25.175 * 1.006)}))


@pytest.mark.parametrize(
    "bad",
    [
        {"used": -1, "available": 5280},
        {"used": 1.5, "available": 5280},
        {"used": 6000, "available": 5280},
        {},
    ],
)
def test_bad_utilisation_is_refused(bad):
    util = copy.deepcopy(REAL["utilization"])
    util["ICESTORM_LC"] = bad
    with pytest.raises(reports.ReportError):
        parse(report(utilization=util))


def test_the_logic_cell_figure_must_be_there():
    util = copy.deepcopy(REAL["utilization"])
    del util["ICESTORM_LC"]
    with pytest.raises(reports.ReportError, match="ICESTORM_LC"):
        parse(report(utilization=util))


def test_the_main_clock_is_clk_else_the_slowest():
    assert parse(report(fmax={"a": clock(40.0), MAIN: clock(30.0)})).fmax_hz == 30_000_000
    assert parse(report(fmax={"a": clock(40.0), "b": clock(30.0)})).fmax_hz == 30_000_000


# ---- the log is used only to name a capacity failure


def test_a_real_over_capacity_log_is_a_capacity_failure():
    log = (LOGS / "nextpnr-over-capacity.log").read_text()
    assert reports.capacity_failure(log) is True
    assert reports.log_lc_percent(log) == pytest.approx(277.0, abs=0.1)


def test_a_real_bad_pcf_log_is_not_a_capacity_failure():
    assert reports.capacity_failure((LOGS / "nextpnr-bad-pcf.log").read_text()) is False


def test_no_bels_remaining_is_a_capacity_failure():
    log = "ERROR: Unable to place cell 'x', no BELs remaining to implement cell type 'ICESTORM_LC'\n"
    assert reports.capacity_failure(log) is True


@pytest.mark.parametrize(
    "line",
    [
        "ERROR: package does not have a pin named '9999' (on line 4)",
        "ERROR: Loading PCF failed.",
        "ERROR: Unable to place cell 'q' at bel 'X1/Y1/io0': constraint cannot be satisfied",
        "ERROR: IO constraint cannot be applied: Insufficient information in pcf",
    ],
)
def test_pcf_and_constraint_errors_are_not_capacity(line):
    assert reports.capacity_failure(line + "\n") is False


# ---- yosys statistics


def test_cells_from_the_real_stat_json():
    stat = (LOGS / "yosys-stat-bars.json").read_bytes()
    assert reports.parse_stat(stat) == {
        "SB_LUT4": 74, "SB_DFF": 22, "SB_RAM40_4K": 0, "SB_SPRAM256KA": 0, "SB_MAC16": 0,
    }  # fmt: skip


def test_dff_variants_are_summed_and_a_single_module_is_enough():
    doc = {
        "modules": {
            "\\top": {"num_cells_by_type": {"SB_DFF": 1, "SB_DFFESR": 2, "SB_DFFSS": 3, "SB_RAM40_4K": 4}}
        }
    }
    cells = reports.parse_stat(json.dumps(doc).encode())
    assert cells["SB_DFF"] == 6 and cells["SB_RAM40_4K"] == 4


@pytest.mark.parametrize(
    "raw",
    [b"", b"{", b"[]", b'{"design": {}}', b'{"modules": {}}', b'{"design": {"num_cells_by_type": {"SB_LUT4": -1}}}',
     b'{"design": {"num_cells_by_type": {"SB_LUT4": "x"}}}', b'{"design": {"num_cells_by_type": []}}'],
)  # fmt: skip
def test_bad_statistics_are_refused(raw):
    with pytest.raises(reports.ReportError):
        reports.parse_stat(raw)


def test_has_registers():
    assert reports.has_registers({"SB_LUT4": 5, "SB_DFF": 1})
    assert reports.has_registers({"SB_RAM40_4K": 1})
    assert not reports.has_registers({"SB_LUT4": 5})
