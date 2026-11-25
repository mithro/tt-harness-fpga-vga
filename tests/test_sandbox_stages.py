# SPDX-License-Identifier: Apache-2.0
"""Hostile and ordinary designs through the real build and reference stages with the real toolchain,
inside the real sandbox. Needs both; skipped, with the reason, where either is missing."""

import dataclasses
import json

import pytest

from tthfv import build, fetch, paths, reference, sandbox, tools
from tthfv.manifest import Design

try:
    tools.tool("verilator")
    tools.tool("yosys")
    tools.tool("nextpnr-ice40")
    tools.tool("icepack")
    _MISSING = "" if sandbox.available() else f"bwrap cannot run here: {sandbox.unavailable_reason()}"
except Exception as e:  # noqa: BLE001 - any failure to locate a tool means skip
    _MISSING = f"toolchain not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

PORTS = """(
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);"""

# Runs at time 0 in the simulation: tries to run a command and write files outside, in its own src
# directory, and (allowed) in the reference directory.
EVIL = """module tt_um_evil %(ports)s
  integer f;
  initial begin
    $system("touch %(outside)s/pwned-by-system");
    $system("touch %(src)s/pwned-in-src");
    f = $fopen("%(outside)s/pwned-by-fopen", "w");
    $display("EVIL fopen outside returned %%0d", f);
    f = $fopen("%(src)s/pwned-by-fopen-in-src", "w");
    $display("EVIL fopen in src returned %%0d", f);
    $system("echo allowed > %(ref)s/allowed.txt");
    $display("EVIL secret: %%0s", "done");
  end
  reg [7:0] count;
  always @(posedge clk) count <= rst_n ? count + 8'd1 : 8'd0;
  assign uo_out = count;
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""

# A ROM read from a file: a relative one next to the sources and an absolute one under the fake home.
ROM = """module tt_um_rom %(ports)s
  reg [7:0] mem [0:3];
  initial $readmemh("%(file)s", mem);
  reg [1:0] a;
  always @(posedge clk) a <= rst_n ? a + 2'd1 : 2'd0;
  assign uo_out = mem[a];
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
  initial $display("ROM word 0 is %%h", mem[0]);
endmodule
"""


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    return tmp_path / "work"


def make(tmp_path, name, text, extra=None):
    """A design whose checkout is tmp_path/<name>/repo, the way a fetched one is laid out."""
    root = tmp_path / "work" / "src" / "tt99" / name
    src = root / "repo" / "src"
    src.mkdir(parents=True)
    (src / f"{name}.v").write_text(text)
    for rel, body in (extra or {}).items():
        (src / rel).write_text(body)
    d = Design(
        id=f"tt99/{name}", shuttle="tt99", macro=name, title="", author="", repo="", commit="",
        top_module=name, source_files=(f"{name}.v",), clock_hz=25_175_000, clock_declared=True,
        pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None, qspi=None,
        ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    fr = fetch.resolve(d, root / "repo", root / "repo", root / "fetch.log")
    assert fr.status == "ok", fr.reason
    return d, fr


def test_a_design_cannot_run_commands_or_write_files_outside_the_reference_directory(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    ref = tmp_path / "work" / "reference" / "tt99" / "tt_um_evil" / "ideal" / "scratch"
    monkeypatch.setenv("TTHFV_KEEP_SCRATCH", "1")
    src = tmp_path / "work" / "src" / "tt99" / "tt_um_evil" / "repo" / "src"
    text = EVIL % {"ports": PORTS, "outside": outside, "src": src, "ref": ref}
    d, fr = make(tmp_path, "tt_um_evil", text)
    sim = reference.simulate(d, fr, clocks=100)
    log = sim.log.read_text()
    assert sim.status == "ok", log
    assert sim.sandbox == "bwrap"
    assert list(outside.iterdir()) == []  # nothing created by $system or $fopen
    assert not (src / "pwned-in-src").exists() and not (src / "pwned-by-fopen-in-src").exists()
    assert "touch:" in log  # the command ran, inside, and failed
    assert "EVIL fopen outside returned 0" in log and "EVIL fopen in src returned 0" in log
    assert (ref / "allowed.txt").read_text() == "allowed\n"  # its scratch directory is writable
    assert json.loads((ref.parent / "sim.json").read_text())["sandbox"] == "bwrap"


def test_the_same_design_unsandboxed_does_write_outside(tmp_path, monkeypatch):
    """The control: without the sandbox the hostile design does what it says, so the test above tests something."""
    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    outside = tmp_path / "outside"
    outside.mkdir()
    ref = tmp_path / "work" / "reference" / "tt99" / "tt_um_evil" / "ideal" / "scratch"
    src = tmp_path / "work" / "src" / "tt99" / "tt_um_evil" / "repo" / "src"
    d, fr = make(tmp_path, "tt_um_evil", EVIL % {"ports": PORTS, "outside": outside, "src": src, "ref": ref})
    sim = reference.simulate(d, fr, clocks=100)
    assert sim.status == "ok", sim.log.read_text()
    assert (outside / "pwned-by-system").exists() and (outside / "pwned-by-fopen").exists()


def test_readmemh_of_an_absolute_path_outside_the_allowed_paths_fails_in_verilator_and_yosys(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    (home / "secret.hex").write_text("de\nad\nbe\nef\n")
    d, fr = make(tmp_path, "tt_um_rom", ROM % {"ports": PORTS, "file": home / "secret.hex"})

    sim = reference.simulate(d, fr, clocks=50)
    assert sim.status == "ok", sim.log.read_text()
    assert "ROM word 0 is de" not in sim.log.read_text()  # not readable from inside
    assert sim.dump.read_bytes()[10:14] == bytes(4)  # the memory stayed zero

    b = build.build(d, fr)
    assert b.status == "synth-fail" and b.sandbox == "bwrap", b.reason
    assert "secret.hex" in (paths.design_dir("build", d.id) / "yosys-default.log").read_text()

    monkeypatch.setenv("TTHFV_SANDBOX", "none")  # the control: both read it when unsandboxed
    sim = reference.simulate(d, fr, clocks=50, force=True)
    assert "ROM word 0 is de" in sim.log.read_text()
    assert build.build(d, fr, force=True).status != "synth-fail"


def test_a_relative_hex_file_next_to_the_sources_is_read_and_gives_the_same_results(tmp_path, monkeypatch):
    d, fr = make(
        tmp_path,
        "tt_um_rom",
        ROM % {"ports": PORTS, "file": "rom.hex"},
        extra={"rom.hex": "de\nad\nbe\nef\n"},
    )
    sim = reference.simulate(d, fr, clocks=50)
    assert sim.status == "ok", sim.log.read_text()
    assert "ROM word 0 is de" in sim.log.read_text()
    boxed = build.build(d, fr)
    assert boxed.status in ("ok", "timing-fail") and boxed.sandbox == "bwrap", boxed.reason
    dump = sim.dump.read_bytes()

    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    plain = build.build(d, fr, force=True)
    assert plain.sandbox == "none"
    assert plain.bitstream_sha256 == boxed.bitstream_sha256
    assert reference.simulate(d, fr, clocks=50, force=True).dump.read_bytes() == dump


def test_the_build_json_records_the_sandbox(tmp_path):
    d, fr = make(tmp_path, "tt_um_rom", ROM % {"ports": PORTS, "file": "rom.hex"}, extra={"rom.hex": "00\n"})
    b = build.build(d, fr)
    record = json.loads((paths.design_dir("build", d.id) / "build.json").read_text())
    assert b.sandbox == record["sandbox"] == "bwrap"
    assert dataclasses.replace(b, sandbox="none").inputs_sha256 == b.inputs_sha256  # not part of the digest
