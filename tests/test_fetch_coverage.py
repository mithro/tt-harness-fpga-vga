# SPDX-License-Identifier: Apache-2.0
"""The fetch stage's coverage rules (local git repositories, no network): the IHP re-harden's sky130_polyfill.v,
which the shuttle lists and the project repository does not have, and a repository that is gone."""
# ruff: noqa: F811 - the fixtures are imported from test_fetch

import json

from test_fetch import TOP, commit_files, design, project, work  # noqa: F401 - fixtures

from tthfv import fetch


def test_a_listed_polyfill_absent_from_the_repository_is_left_to_the_pdk_models(project, work):
    c = commit_files(project, {"src/a.v": TOP})
    r = fetch.fetch(design(project, c, source_files=("a.v", "sky130_polyfill.v")))
    assert r.status == "ok", r.reason
    assert [p.name for p in r.synth_sources] == ["a.v"]
    assert r.polyfill_absent == ("sky130_polyfill.v",)
    rec = json.loads((work / "src" / "tt99" / "tt_um_x" / "fetch.json").read_text())
    assert rec["polyfill_absent"] == ["sky130_polyfill.v"]
    assert rec["synth_sources"] == ["src/a.v"]
    again = fetch.fetch(design(project, c, source_files=("a.v", "sky130_polyfill.v")))
    assert again.status == "ok" and again.polyfill_absent == ("sky130_polyfill.v",)


def test_a_polyfill_the_repository_has_is_compiled_as_listed(project, work):
    c = commit_files(project, {"src/a.v": TOP, "src/sky130_polyfill.v": "module p;\nendmodule\n"})
    r = fetch.fetch(design(project, c, source_files=("a.v", "sky130_polyfill.v")))
    assert r.status == "ok" and [p.name for p in r.synth_sources] == ["a.v", "sky130_polyfill.v"]
    assert r.polyfill_absent == ()
    rec = json.loads((work / "src" / "tt99" / "tt_um_x" / "fetch.json").read_text())
    assert "polyfill_absent" not in rec  # unchanged for every design that does not need the rule


def test_another_missing_source_still_fails_naming_it(project):
    c = commit_files(project, {"src/a.v": TOP})
    r = fetch.fetch(design(project, c, source_files=("a.v", "sky130_polyfill.v", "gone.v")))
    assert r.status == "fetch-fail" and r.reason == "missing sources: gone.v"


def test_a_polyfill_outside_src_is_not_the_rule(project):
    c = commit_files(project, {"src/a.v": TOP})
    r = fetch.fetch(design(project, c, source_files=("a.v", "lib/sky130_polyfill.v")))
    assert r.status == "fetch-fail" and "lib/sky130_polyfill.v" in r.reason


def test_a_wokwi_project_without_sources_needs_its_generated_verilog(project):
    c = commit_files(project, {"info.yaml": "project:\n  wokwi_id: 1\n"})
    d = design(
        project, c, id="tt99/tt_um_wokwi_123", macro="tt_um_wokwi_123", top_module="tt_um_wokwi_123",
        source_files=(),
    )  # fmt: skip
    r = fetch.fetch(d)
    assert r.status == "fetch-fail"
    assert r.reason.startswith("needs generated source tt_um_wokwi_123.v: "), r.reason


def test_a_repository_that_is_gone_says_so():
    out = "remote: Repository not found.\nfatal: Authentication failed for 'https://github.com/o/r/'\n"
    why = fetch._why(["git", "fetch", "-q"], 128, out)
    assert why.startswith("repository not available: git fetch failed: ")
    assert fetch._why(["git", "fetch"], 128, "fatal: unable to access: timeout").startswith(
        "git fetch failed"
    )
