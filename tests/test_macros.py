# SPDX-License-Identifier: Apache-2.0
"""tthfv.macros: a missing module that exists only as layout or schematic is named as an analog/hard macro."""

from tthfv import fetch, macros
from tthfv.manifest import Design

DESIGN = Design(
    id="tt99/tt_um_x", shuttle="tt99", macro="tt_um_x", title="", author="", repo="", commit="",
    top_module="tt_um_x", source_files=("project.v",), clock_hz=25_175_000, clock_declared=True, pinout={},
    pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None, qspi=None, ui_in=0, uio_in=0,
    scripted=False, candidate=False,
)  # fmt: skip
YOSYS = "yosys exit status 1: ERROR: Module `\\segdac' referenced in module `\\tt_um_x' in cell `\\d' is not part of the design."


def fetched(tmp_path, project: str, files: dict[str, str]):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "project.v").write_text(project)
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    r = fetch.resolve(DESIGN, repo, repo, tmp_path / "fetch.log")
    assert r.status == "ok", r.reason
    return r


PROJECT = "module tt_um_x;\n  segdac d ();\n  controller c ();\n  // inverter only in a comment\nendmodule\n"


def test_a_missing_module_with_only_layout_is_an_analog_macro(tmp_path):
    f = fetched(tmp_path, PROJECT, {"mag/segdac.mag": "", "xschem/segdac.sch": "", "mag/inverter.mag": "",
                                     "verilog/controller.v": "module controller;\nendmodule\n"})  # fmt: skip
    assert macros.missing(f) == ["segdac"]
    assert macros.restate(YOSYS, YOSYS, f) == f"needs analog/hard macro segdac: {YOSYS}"


def test_a_hard_macro_with_gds_and_rtl_elsewhere_is_named_too(tmp_path):
    f = fetched(
        tmp_path, PROJECT, {"mag/segdac.mag": "", "gds/controller.gds": "", "verilog/controller.v": ""}
    )
    assert macros.missing(f) == ["controller", "segdac"]


def test_nothing_is_restated_without_a_missing_module_or_without_layout(tmp_path):
    f = fetched(tmp_path, PROJECT, {"mag/segdac.mag": ""})
    assert macros.restate("yosys exit status 1: ERROR: syntax error", "ERROR: syntax error", f) == (
        "yosys exit status 1: ERROR: syntax error"
    )
    g = fetched(tmp_path / "g", PROJECT, {})
    assert macros.restate(YOSYS, YOSYS, g) == YOSYS


def test_a_module_the_sources_declare_is_not_a_macro(tmp_path):
    f = fetched(tmp_path, PROJECT + "module segdac;\nendmodule\n", {"mag/segdac.mag": ""})
    assert macros.missing(f) == []
