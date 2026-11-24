# SPDX-License-Identifier: Apache-2.0
"""Hostile simulations through the real reference stage with the real toolchain, the sandbox and cgroup limits
all on: a design whose simulation allocates without bound, forks without bound or fills its scratch directory
fails with a truthful reason and the host is unaffected."""

import json

import pytest
from test_sandbox_stages import PORTS, make

from tthfv import limits, paths, reference, safefile, sandbox, tools

try:
    for _t in ("verilator", "yosys", "nextpnr-ice40", "icepack"):
        tools.tool(_t)
    if not sandbox.available():
        _MISSING = f"bwrap cannot run here: {sandbox.unavailable_reason()}"
    elif limits.unavailable_reason():
        _MISSING = f"no systemd user scope with limits here: {limits.unavailable_reason()}"
    else:
        _MISSING = ""
except Exception as e:  # noqa: BLE001
    _MISSING = f"toolchain not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

HOSTILE = """module tt_um_hostile %(ports)s
  initial begin
    $system("%(command)s");
  end
  reg [7:0] count;
  always @(posedge clk) count <= rst_n ? count + 8'd1 : 8'd0;
  assign uo_out = count;
  assign uio_out = 8'd0;
  assign uio_oe = 8'd0;
endmodule
"""


@pytest.fixture(autouse=True)
def real_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.delenv("TTHFV_LIMITS", raising=False)
    monkeypatch.setenv("TTHFV_LIMIT_MEMORY", "1G")
    monkeypatch.setenv("TTHFV_LIMIT_TASKS", "256")
    return tmp_path


def hostile(tmp_path, command):
    return make(tmp_path, "tt_um_hostile", HOSTILE % {"ports": PORTS, "command": command})


def test_an_ordinary_simulation_runs_inside_the_limits_and_records_them(tmp_path):
    d, fr = hostile(tmp_path, "true")
    sim = reference.simulate(d, fr, clocks=100)
    assert sim.status == "ok", sim.log.read_text()
    assert sim.limits == {"memory": "1G", "tasks": 256, "cpu": "400%"} and sim.limited is None
    assert sim.peak["memory"] > 0 and sim.peak["tasks"] > 0
    stamp = json.loads((paths.design_dir("reference", d.id) / "sim.json").read_text())
    assert stamp["limits"] == sim.limits


def test_a_simulation_that_allocates_without_bound_is_killed_and_says_why(tmp_path):
    d, fr = hostile(tmp_path, "dd if=/dev/zero bs=1500M count=1 | wc -c")
    sim = reference.simulate(d, fr, clocks=100)
    assert sim.status == "sim-fail", sim.log.read_text()
    assert "memory limit (1G) exceeded" in sim.reason and sim.limited == "memory"
    assert not (paths.design_dir("reference", d.id) / "scratch").exists()


def test_a_simulation_that_forks_without_bound_is_bounded_and_reported(tmp_path):
    d, fr = hostile(tmp_path, "for i in $(seq 3000); do sleep 4 & done; wait")
    sim = reference.simulate(d, fr, clocks=100)
    assert sim.status == "sim-fail", sim.log.read_text()
    assert "task limit (256) reached" in sim.reason and sim.limited == "tasks"
    assert sim.peak["tasks"] <= 256


def test_raising_a_limit_redoes_a_result_that_hit_it(tmp_path, monkeypatch):
    d, fr = hostile(tmp_path, "dd if=/dev/zero bs=1500M count=1 | wc -c")
    assert reference.simulate(d, fr, clocks=100).limited == "memory"
    assert reference.sim_key(d, fr, 100)["limits"]["memory"] == "1G"
    monkeypatch.setenv("TTHFV_LIMIT_MEMORY", "4G")
    assert reference.sim_key(d, fr, 100)["limits"]["memory"] == "4G"
    again = reference.simulate(d, fr, clocks=100)
    assert again.status == "ok" and again.limits["memory"] == "4G"


def test_a_simulation_that_fills_its_scratch_directory_fails_the_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 50 << 20)  # a small cap so the test writes only 80 MiB
    scratch = tmp_path / "work" / "reference" / "tt99" / "tt_um_hostile" / "scratch"
    d, fr = hostile(tmp_path, f"head -c 80000000 /dev/zero > {scratch}/big.bin")
    sim = reference.simulate(d, fr, clocks=100)
    assert sim.status in ("sim-fail", "verilate-fail"), sim.log.read_text()
    assert "scratch" in sim.reason and "filled" in sim.reason
    assert not scratch.exists()


def test_a_simulation_that_makes_a_million_tiny_files_fails_the_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(safefile, "MAX_ENTRIES", 5000)  # a small cap so the test makes only 6000 files
    scratch = tmp_path / "work" / "reference" / "tt99" / "tt_um_hostile" / "scratch"
    d, fr = hostile(tmp_path, f"cd {scratch}; seq 6000 | xargs touch")
    sim = reference.simulate(d, fr, clocks=100)
    assert sim.status in ("sim-fail", "verilate-fail"), sim.log.read_text()
    assert "scratch" in sim.reason and "entries" in sim.reason
