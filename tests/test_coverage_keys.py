# SPDX-License-Identifier: Apache-2.0
"""The coverage work (PDK cell models, the SystemVerilog retry) must not change any existing result: a design
that needs none of it gets the byte-identical yosys script, nextpnr arguments, build digest, reference key and
Verilator command line as before. The pinned values were computed on the code before that work (58c3c12)."""

import hashlib
import json
from pathlib import Path

from tthfv import build, fetch, reference
from tthfv.manifest import Design

VERSIONS = {"yosys": "Yosys 0.69", "nextpnr-ice40": "nextpnr-ice40 0.9", "icepack": "icepack (none)"}
LIMITS = {"memory": "8G", "tasks": 512, "cpu": "100%"}

# Computed with the code at 58c3c12 (see the docstring).
PINNED_SCRIPT = "e9d07ec5adc07bfaadd2d9aef619c7385d798dc8c4fc23abf431db90329af4cf"
PINNED_DIGEST = "996a855865ab636d9e2b20c89b6e09ee1a6ca5a9368a070ebe834285401b1e51"
PINNED_SIM_KEY = "33ac555e5960affda5b11b569bbc2d5914a2305ed24b99eedb4d2d7bc8f43d00"
PINNED_VERILATOR = "2058b5cd91afb179621806337175c6181c47f866950958a331b2d2448fc38966"


def design() -> Design:
    return Design(
        id="tt99/tt_um_x", shuttle="tt99", macro="tt_um_x", title="", author="", repo="https://e.x/r",
        commit="c" * 40, top_module="tt_um_x", source_files=("a.v", "b.sv", "inc.vh"), clock_hz=25_175_000,
        clock_declared=True, pinout={}, pmods=(), tiles="1x1", render_verdict=None,
        override={"verilator_flags": ["-DFOO=1"]}, patch=None, qspi=None, ui_in=3, uio_in=0, scripted=False,
        candidate=False,
    )  # fmt: skip


def fetched(root: Path) -> fetch.FetchResult:
    src = root / "repo" / "src"
    src.mkdir(parents=True)
    (src / "a.v").write_text("module tt_um_x(input clk, output y);\n  b u(.clk(clk), .y(y));\nendmodule\n")
    (src / "b.sv").write_text("module b(input logic clk, output logic y);\n  assign y = clk;\nendmodule\n")
    (src / "inc.vh").write_text("`define X 1\n")
    r = fetch.resolve(design(), root / "repo", root / "repo", root / "fetch.log")
    assert r.status == "ok", r.reason
    return r


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def relative(text: str, root: Path) -> str:
    return text.replace(str(root), "<root>")


def test_the_yosys_script_and_digest_of_a_design_without_pdk_cells_are_unchanged(tmp_path):
    f = fetched(tmp_path)
    out_json = tmp_path / "build" / "scratch" / "out-default.json"
    script = build._yosys_script(
        [tmp_path / "build" / "tt_fpga_top.v", *f.synth_sources], list(f.include_dirs), out_json, sv=True,
        synth="default", defs=("FOO=1",),
    )  # fmt: skip
    wrapper = (build.HARNESS / "tt_fpga_top.v").read_text().replace(build.PLACEHOLDER, "tt_um_x")
    digest = build._inputs_digest(design(), f, wrapper, VERSIONS, 8, LIMITS)
    print(json.dumps({"script": sha(relative(script, tmp_path)), "digest": digest}))
    assert sha(relative(script, tmp_path)) == PINNED_SCRIPT
    assert digest == PINNED_DIGEST


def test_the_reference_key_and_verilator_command_of_a_design_without_pdk_cells_are_unchanged(
    tmp_path, monkeypatch
):
    f = fetched(tmp_path)
    monkeypatch.setattr(reference, "_verilator_version", lambda: "Verilator 5.0")
    monkeypatch.setattr(reference.sandbox, "limits_record", lambda: LIMITS)
    key = reference.sim_key(design(), f, 1000, reference.IDEAL, 0xFF)
    seen: list[list[str]] = []

    def fake_run(args, log, cwd, timeout, box, track):
        seen.append(list(args))
        return 1, "%Error: stop here"

    monkeypatch.setattr(reference, "_run", fake_run)
    monkeypatch.setattr(reference.tools, "tool", lambda name: Path("/tools/bin") / name)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    failed = reference._verilate(
        design(), f, key, scratch, f.include_dirs[0], None, reference._Track(), tmp_path / "sim.log"
    )
    assert failed is not None
    argv = relative(json.dumps(seen), tmp_path).replace(str(reference.TB), "<tb>")
    print(json.dumps({"key": reference.key_sha256(key), "argv": sha(argv)}))
    assert reference.key_sha256(key) == PINNED_SIM_KEY
    assert sha(argv) == PINNED_VERILATOR
