# SPDX-License-Identifier: Apache-2.0
"""PDK cell models (tthfv/harness/cells) and the rule that picks them (tthfv.pdkcells). The truth-table tests run
each model under the pinned iverilog and are skipped when it is absent."""

import subprocess
from pathlib import Path

import pytest

from tthfv import pdkcells, tools

# Cells whose models must exist: the ones the 445 designs instantiate and fail without (see docs/coverage.md).
NEEDED = {
    "sky130_fd_sc_hd__and2_1", "sky130_fd_sc_hd__buf_1", "sky130_fd_sc_hd__buf_12", "sky130_fd_sc_hd__clkbuf_8",
    "sky130_fd_sc_hd__clkbuf_16", "sky130_fd_sc_hd__clkdlybuf4s25_1", "sky130_fd_sc_hd__dlclkp_1",
    "sky130_fd_sc_hd__dlclkp_2", "sky130_fd_sc_hd__dlclkp_4", "sky130_fd_sc_hd__dlxtn_1",
    "sky130_fd_sc_hd__dlxtp_1", "sky130_fd_sc_hd__dlygate4sd3_1", "sky130_fd_sc_hd__fa_1",
    "sky130_fd_sc_hd__inv_1", "sky130_fd_sc_hd__mux4_1", "sky130_fd_sc_hd__nand4_1",
    "gf180mcu_fd_sc_mcu7t5v0__clkbuf_8", "gf180mcu_fd_sc_mcu7t5v0__inv_1", "gf180mcu_fd_sc_mcu7t5v0__inv_2",
    "gf180mcu_fd_sc_mcu7t5v0__inv_4", "sg13g2_inv_1", "sg13cmos5l_buf_8", "sg13cmos5l_inv_1",
    "sg13cmos5l_inv_2", "sg13cmos5l_inv_4",
}  # fmt: skip

LATCHES = {
    "sky130_fd_sc_hd__dlclkp_1", "sky130_fd_sc_hd__dlclkp_2", "sky130_fd_sc_hd__dlclkp_4",
    "sky130_fd_sc_hd__dlxtn_1", "sky130_fd_sc_hd__dlxtp_1",
}  # fmt: skip
DELAYS = {"sky130_fd_sc_hd__clkdlybuf4s25_1", "sky130_fd_sc_hd__dlygate4sd3_1"}


def test_the_library_has_exactly_the_needed_cells_with_their_kinds():
    lib = pdkcells.library()
    assert set(lib) == NEEDED
    assert {n for n, c in lib.items() if c.kind == "latch"} == LATCHES
    assert {n for n, c in lib.items() if c.kind == "delay"} == DELAYS


def test_every_model_file_has_the_apache_header_and_names_its_source():
    for path in sorted(pdkcells.CELLS_DIR.glob("*.v")):
        text = path.read_text()
        assert text.startswith("// SPDX-License-Identifier: Apache-2.0\n"), path.name
        assert "https://" in text.split("// @cell", 1)[0], path.name


def test_missing_cell_reads_yosys_and_verilator_messages():
    yosys = "ERROR: Module `\\sky130_fd_sc_hd__dlxtp_1' referenced in module `\\latch_reg' in cell `\\x' is not part of the design."
    verilator = (
        "%Error-MODMISSING: a.v:39:5: Cannot find file containing module: 'sky130_fd_sc_hd___05Finv_1'"
    )
    assert pdkcells.missing_cell(yosys) == "sky130_fd_sc_hd__dlxtp_1"
    assert pdkcells.missing_cell(verilator) == "sky130_fd_sc_hd__inv_1"
    assert pdkcells.missing_cell("Cannot find file containing module: 'sg13g2_inv_1'") == "sg13g2_inv_1"
    assert (
        pdkcells.missing_cell(
            "ERROR: Module `\\controller' referenced in module `\\t' in cell `\\c' is not part of the design."
        )
        == ""
    )
    assert pdkcells.missing_cell("Cannot find file containing module: 'segdac'") == ""


def test_referenced_ignores_comments_and_cells_the_design_declares(tmp_path):
    a = tmp_path / "a.v"
    a.write_text(
        "module top(input x, output y, output z);\n"
        "  // sky130_fd_sc_hd__buf_1 in a comment\n"
        "  /* sky130_fd_sc_hd__dlxtp_1 in a block comment */\n"
        "  sky130_fd_sc_hd__inv_1 i (.A(x), .Y(y));\n"
        "  sky130_fd_sc_hd__and2_1 a (.A(x), .B(x), .X(z));\n"
        "  sky130_fd_sc_hd__weird_9 w ();\n"
        "endmodule\n"
        "module sky130_fd_sc_hd__and2_1(input A, B, output X); assign X = A & B; endmodule\n"
    )
    assert pdkcells.referenced([a]) == ["sky130_fd_sc_hd__inv_1", "sky130_fd_sc_hd__weird_9"]
    plan = pdkcells.plan_for([a])
    assert plan.cells == ("sky130_fd_sc_hd__inv_1",) and plan.unmodelled == ("sky130_fd_sc_hd__weird_9",)
    assert not plan.latches and plan.note() == ""
    assert plan.key_fields() == {"pdk_cells": ["sky130_fd_sc_hd__inv_1"], "pdk_cells_sha256": plan.sha256}
    assert "module sky130_fd_sc_hd__inv_1" in plan.text and "and2_1" not in plan.text


def test_a_plan_with_latch_and_delay_cells_says_so():
    plan = pdkcells.plan_named(["sky130_fd_sc_hd__dlxtp_1", "sky130_fd_sc_hd__dlygate4sd3_1"])
    assert plan is not None and plan.latches
    assert "LUT loops" in plan.note() and "dlxtp_1" in plan.note() and "dlygate4sd3_1" in plan.note()
    assert pdkcells.plan_named(["sky130_fd_sc_hd__nope_1"]) is None
    assert pdkcells.plan_named(["sky130_fd_sc_hd__inv_1", "sky130_fd_sc_hd__dlxtp_1"]) is None  # not sorted


def test_retry_reason():
    plan = pdkcells.plan_named(["sky130_fd_sc_hd__inv_1"])
    assert (
        pdkcells.retry_reason("Cannot find file containing module: 'sky130_fd_sc_hd___05Finv_1'", plan) == ""
    )
    assert pdkcells.retry_reason(
        "Cannot find file containing module: 'sky130_fd_sc_hd___05Fo21a_1'", plan
    ) == ("needs PDK cell model sky130_fd_sc_hd__o21a_1")
    assert pdkcells.retry_reason("syntax error", plan) == "no missing PDK cell"


# ---- truth tables, simulated

try:
    _IVERILOG, _VVP = tools.bin_dir() / "iverilog", tools.bin_dir() / "vvp"
    _MISSING = (
        "" if _IVERILOG.is_file() and _VVP.is_file() else f"iverilog not available in {tools.bin_dir()}"
    )
except Exception as e:  # noqa: BLE001 - any failure to locate a tool means skip
    _MISSING = f"iverilog not available: {e}"


def simulate(tmp_path: Path, cell: str, body: str) -> list[str]:
    tb = tmp_path / "tb.v"
    tb.write_text(f"module tb;\n{body}\nendmodule\n")
    model = tmp_path / "model.v"
    model.write_text(pdkcells.cells_text([cell]))
    exe = tmp_path / "tb.vvp"
    for argv in (
        [str(_IVERILOG), "-g2012", "-o", str(exe), str(tb), str(model)],
        [str(_VVP), "-n", str(exe)],
    ):
        done = subprocess.run(
            argv, check=False, cwd=tmp_path, env=tools.env(), capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, done.stdout + done.stderr
    return [ln for ln in done.stdout.splitlines() if ln.startswith("@")]


# cell -> (inputs, outputs, function of the inputs dict giving the outputs dict)
COMBINATIONAL = {
    "sky130_fd_sc_hd__and2_1": ("A B", "X", lambda v: {"X": v["A"] & v["B"]}),
    "sky130_fd_sc_hd__nand4_1": ("A B C D", "Y", lambda v: {"Y": 1 - (v["A"] & v["B"] & v["C"] & v["D"])}),
    "sky130_fd_sc_hd__inv_1": ("A", "Y", lambda v: {"Y": 1 - v["A"]}),
    "sky130_fd_sc_hd__mux4_1": (
        "A0 A1 A2 A3 S0 S1", "X", lambda v: {"X": v[f"A{v['S1'] * 2 + v['S0']}"]},
    ),
    "sky130_fd_sc_hd__fa_1": (
        "A B CIN", "COUT SUM",
        lambda v: {"SUM": (v["A"] + v["B"] + v["CIN"]) & 1, "COUT": int(v["A"] + v["B"] + v["CIN"] >= 2)},
    ),
    **{
        c: ("A", "X", lambda v: {"X": v["A"]})
        for c in ("sky130_fd_sc_hd__buf_1", "sky130_fd_sc_hd__buf_12", "sky130_fd_sc_hd__clkbuf_8",
                  "sky130_fd_sc_hd__clkbuf_16", "sky130_fd_sc_hd__clkdlybuf4s25_1",
                  "sky130_fd_sc_hd__dlygate4sd3_1", "sg13cmos5l_buf_8")
    },
    **{
        c: ("I", "ZN", lambda v: {"ZN": 1 - v["I"]})
        for c in ("gf180mcu_fd_sc_mcu7t5v0__inv_1", "gf180mcu_fd_sc_mcu7t5v0__inv_2", "gf180mcu_fd_sc_mcu7t5v0__inv_4")
    },
    "gf180mcu_fd_sc_mcu7t5v0__clkbuf_8": ("I", "Z", lambda v: {"Z": v["I"]}),
    **{
        c: ("A", "Y", lambda v: {"Y": 1 - v["A"]})
        for c in ("sg13g2_inv_1", "sg13cmos5l_inv_1", "sg13cmos5l_inv_2", "sg13cmos5l_inv_4")
    },
}  # fmt: skip


def test_every_logic_and_delay_cell_has_a_truth_table_test():
    lib = pdkcells.library()
    assert {n for n, c in lib.items() if c.kind != "latch"} == set(COMBINATIONAL)


@pytest.mark.parametrize("cell", sorted(COMBINATIONAL))
def test_combinational_truth_table(tmp_path, cell):
    if _MISSING:
        pytest.skip(_MISSING)
    ins, outs, fn = COMBINATIONAL[cell]
    ins, outs = ins.split(), outs.split()
    n = len(ins)
    body = (
        "".join(f"  reg {i};\n" for i in ins)
        + "".join(f"  wire {o};\n" for o in outs)
        + f"  {cell} dut ({', '.join(f'.{p}({p})' for p in ins + outs)});\n"
        + f"  integer k;\n  initial begin\n    for (k = 0; k < {1 << n}; k = k + 1) begin\n"
        + f'      {{{", ".join(ins)}}} = k;\n      #1 $display("@ %0d{" %b" * len(outs)}", k, {", ".join(outs)});\n'
        + "    end\n  end\n"
    )
    got = simulate(tmp_path, cell, body)
    assert len(got) == 1 << n
    for line in got:
        k, *vals = line.split()[1:]
        k = int(k)
        v = {name: (k >> (n - 1 - i)) & 1 for i, name in enumerate(ins)}
        want = fn(v)
        assert {o: int(x) for o, x in zip(outs, vals, strict=True)} == want, (cell, v)


# (cell, the enable port and its active level, the data port, the output port)
DATA_LATCHES = [
    ("sky130_fd_sc_hd__dlxtp_1", "GATE", 1, "D", "Q"),
    ("sky130_fd_sc_hd__dlxtn_1", "GATE_N", 0, "D", "Q"),
]


@pytest.mark.parametrize(("cell", "en", "active", "d", "q"), DATA_LATCHES)
def test_a_data_latch_is_transparent_at_its_active_level_and_holds_otherwise(
    tmp_path, cell, en, active, d, q
):
    if _MISSING:
        pytest.skip(_MISSING)
    on, off = active, 1 - active
    # steps: (enable, data) -> the expected output after it
    steps = [
        (on, 0, 0),
        (on, 1, 1),
        (off, 1, 1),
        (off, 0, 1),
        (on, 0, 0),
        (off, 0, 0),
        (off, 1, 0),
        (on, 1, 1),
    ]
    body = (
        f"  reg e, dd;\n  wire qq;\n  {cell} dut (.{en}(e), .{d}(dd), .{q}(qq));\n  initial begin\n"
        + "".join(f'    e = {e}; dd = {x}; #1 $display("@ %b", qq);\n' for e, x, _ in steps)
        + "  end\n"
    )
    got = [line.split()[1] for line in simulate(tmp_path, cell, body)]
    assert got == [str(w) for _, _, w in steps]


@pytest.mark.parametrize("cell", sorted(c for c in LATCHES if "dlclkp" in c))
def test_a_clock_gate_latches_gate_while_clk_is_low(tmp_path, cell):
    if _MISSING:
        pytest.skip(_MISSING)
    # (CLK, GATE) -> GCLK. GATE is captured while CLK is low and held while it is high.
    steps = [
        (0, 1, 0), (1, 1, 1), (1, 0, 1),  # GATE falls while CLK is high: GCLK stays high
        (0, 0, 0), (1, 0, 0), (1, 1, 0),  # GATE rises while CLK is high: GCLK stays low
        (0, 1, 0), (1, 1, 1), (0, 1, 0),
    ]  # fmt: skip
    body = (
        f"  reg c, g;\n  wire gc;\n  {cell} dut (.CLK(c), .GATE(g), .GCLK(gc));\n  initial begin\n"
        + "".join(f'    c = {c}; g = {g}; #1 $display("@ %b", gc);\n' for c, g, _ in steps)
        + "  end\n"
    )
    got = [line.split()[1] for line in simulate(tmp_path, cell, body)]
    assert got == [str(w) for _, _, w in steps]
