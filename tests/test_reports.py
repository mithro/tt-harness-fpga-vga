# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

from tthfv import reports

LOGS = Path(__file__).parent / "data" / "logs"


def test_fmax_is_the_last_max_frequency_line():
    # The real log has a pre-route line (22.45 MHz, Info) and a final one (22.04 MHz, Warning).
    r = reports.parse_nextpnr((LOGS / "nextpnr.log").read_text())
    assert r.fmax_hz == 22_040_000
    assert r.clocks == {"clk$SB_IO_IN_$glb_clk": 22_040_000}


def test_utilisation_of_logic_cells():
    r = reports.parse_nextpnr((LOGS / "nextpnr.log").read_text())
    assert r.utilisation["ICESTORM_LC"] == (824, 5280)
    assert r.utilisation["ICESTORM_RAM"] == (0, 30)
    assert r.utilisation["ICESTORM_SPRAM"] == (0, 4)
    assert r.fit is True


def test_fmax_picks_the_top_level_clk_among_several_clocks():
    log = (
        "Info: Max frequency for clock 'clk$SB_IO_IN_$glb_clk': 40.00 MHz (PASS at 25.00 MHz)\n"
        "Info: Max frequency for clock 'divider_Q': 90.00 MHz (PASS at 12.00 MHz)\n"
    )
    r = reports.parse_nextpnr(log)
    assert r.fmax_hz == 40_000_000
    assert r.clocks["divider_Q"] == 90_000_000


def test_fmax_with_several_clocks_and_no_clk_is_the_slowest():
    log = (
        "Info: Max frequency for clock 'a': 40.00 MHz (PASS at 25.00 MHz)\n"
        "Info: Max frequency for clock 'b': 30.00 MHz (PASS at 12.00 MHz)\n"
    )
    assert reports.parse_nextpnr(log).fmax_hz == 30_000_000


def test_no_max_frequency_line_gives_none():
    assert reports.parse_nextpnr("Info: nothing here\n").fmax_hz is None


def test_unable_to_place_is_not_a_fit():
    log = "Info: Device utilisation:\nInfo: \t ICESTORM_LC: 100/ 5280 1%\nERROR: Unable to place cell 'x'\n"
    assert reports.parse_nextpnr(log).fit is False


def test_over_capacity_is_not_a_fit():
    log = "Info: Device utilisation:\nInfo: \t ICESTORM_LC: 6000/ 5280 113%\n"
    r = reports.parse_nextpnr(log)
    assert r.utilisation["ICESTORM_LC"] == (6000, 5280)
    assert r.fit is False


def test_yosys_cell_counts_from_final_statistics():
    cells = reports.parse_yosys((LOGS / "yosys.log").read_text())
    assert cells == {
        "SB_LUT4": 761,
        "SB_DFF": 6 + 10 + 28 + 7 + 16 + 2,
        "SB_RAM40_4K": 0,
        "SB_SPRAM256KA": 0,
        "SB_MAC16": 0,
    }


def test_yosys_dff_variants_are_summed_and_rams_counted():
    log = (
        "=== design hierarchy ===\n\n"
        "        5   SB_DFFE\n"
        "        2   SB_DFFSR\n"
        "        3   SB_RAM40_4K\n"
        "        1   SB_MAC16\n"
        "        9   SB_LUT4\n"
    )
    cells = reports.parse_yosys(log)
    assert cells["SB_DFF"] == 7
    assert cells["SB_RAM40_4K"] == 3
    assert cells["SB_MAC16"] == 1
    assert cells["SB_LUT4"] == 9


def test_no_fixture_contains_a_home_directory_path():
    offenders = [
        p for p in LOGS.parent.rglob("*") if p.is_file() and (b"/" + b"home" + b"/") in p.read_bytes()
    ]
    assert offenders == []
