# SPDX-License-Identifier: Apache-2.0
import json
import shutil
from pathlib import Path

import pytest

from tthfv.manifest import Design, by_id, load

MINI = Path(__file__).parent / "data" / "videos-mini"
EXTRA = Path(__file__).resolve().parent.parent / "designs" / "extra.json"


@pytest.fixture
def mini_repo() -> Path:
    return MINI


def test_load_mini(mini_repo):
    ds = load(videos_repo=mini_repo, extras=None)
    assert [d.id for d in ds] == sorted(d.id for d in ds)
    assert len(ds) == 3


def test_zero_clock_becomes_25175000(mini_repo):
    d = by_id(load(videos_repo=mini_repo, extras=None), "tt05/tt_um_noclock")
    assert (d.clock_hz, d.clock_declared) == (25_175_000, False)


def test_pinout_grouped_by_port(mini_repo):
    d = by_id(load(videos_repo=mini_repo, extras=None), "tt05/tt_um_noclock")
    assert d.pinout["uo"][3] == "vsync"
    assert [len(d.pinout[k]) for k in ("ui", "uo", "uio")] == [8, 8, 8]


def test_candidate_only_when_render_ok(mini_repo):
    ds = {d.id: d for d in load(videos_repo=mini_repo, extras=None)}
    assert (ds["tt06/tt_um_script"].render_verdict, ds["tt06/tt_um_script"].candidate) == ("ok", True)
    assert (ds["tt05/tt_um_fixed"].render_verdict, ds["tt05/tt_um_fixed"].candidate) == ("static", False)
    # No result file at all.
    assert (ds["tt05/tt_um_noclock"].render_verdict, ds["tt05/tt_um_noclock"].candidate) == (None, False)


def test_override_replaces_sources_and_top(mini_repo):
    d = by_id(load(videos_repo=mini_repo, extras=None), "tt05/tt_um_fixed")
    assert d.source_files == ("x.v", "y.v")
    assert d.top_module == "tt_um_other"
    assert (d.clock_hz, d.clock_declared) == (50_000_000, True)
    assert d.override["ui_in"] == 5
    assert d.patch is not None and d.patch.name == "fix.diff" and d.patch.exists()


def test_stimulus_from_override_and_script(mini_repo):
    ds = {d.id: d for d in load(videos_repo=mini_repo, extras=None)}
    assert (ds["tt05/tt_um_fixed"].ui_in, ds["tt05/tt_um_fixed"].uio_in) == (5, 0)
    assert (ds["tt05/tt_um_fixed"].scripted) is False
    s = ds["tt06/tt_um_script"]
    assert (s.ui_in, s.uio_in, s.scripted) == (2, 128, True)
    n = ds["tt05/tt_um_noclock"]
    assert (n.ui_in, n.uio_in, n.scripted, n.override, n.patch) == (0, 0, False, {}, None)


def test_qspi_from_result(mini_repo):
    ds = {d.id: d for d in load(videos_repo=mini_repo, extras=None)}
    assert ds["tt06/tt_um_script"].qspi == "cs0=4,sck=5,sd0=6,sd1=3"
    assert ds["tt05/tt_um_fixed"].qspi is None


def test_by_id_names_close_matches(mini_repo):
    ds = load(videos_repo=mini_repo, extras=None)
    with pytest.raises(KeyError, match="tt_um_script"):
        by_id(ds, "tt06/tt_um_scrip")


def test_design_is_frozen(mini_repo):
    d = load(videos_repo=mini_repo, extras=None)[0]
    assert isinstance(d, Design)
    with pytest.raises(AttributeError):
        d.id = "x"  # type: ignore[misc]


def test_extras_load_as_non_candidates(mini_repo):
    ds = load(videos_repo=mini_repo, extras=EXTRA)
    cal = [d for d in ds if d.shuttle == "calibration"]
    assert len(cal) == 5
    assert all(d.render_verdict is None and not d.candidate for d in cal)
    assert all(d.id == f"calibration/{d.top_module}" for d in cal)
    assert [d.id for d in ds] == sorted(d.id for d in ds)


def test_override_falls_back_to_render_result(tmp_path):
    """The pinned videos repo commit has no overrides-compiled/; result.json records the override."""
    repo = tmp_path / "repo"
    shutil.copytree(MINI, repo)
    shutil.rmtree(repo / "data" / "overrides-compiled")
    result = repo / "data" / "results" / "tt05" / "tt_um_fixed" / "result.json"
    data = json.loads(result.read_text())
    data["override"] = {"ui_in": 5, "inputs": "tt_um_fixed.inputs", "sources": ["x.v"]}
    result.write_text(json.dumps(data))
    (repo / "overrides" / "tt05").mkdir(parents=True)
    (repo / "overrides" / "tt05" / "tt_um_fixed.yaml").write_text("inputs:\n  - at: 0\n")
    d = by_id(load(videos_repo=repo, extras=None), "tt05/tt_um_fixed")
    assert (d.ui_in, d.source_files, d.scripted) == (5, ("x.v",), True)


def test_first_input_event_from_source_yaml(tmp_path):
    """With no compiled .inputs (pinned clone), the first YAML event gives the fixed inputs."""
    repo = tmp_path / "repo"
    shutil.copytree(MINI, repo)
    shutil.rmtree(repo / "data" / "overrides-compiled")
    result = repo / "data" / "results" / "tt06" / "tt_um_script" / "result.json"
    data = json.loads(result.read_text())
    data["override"] = {"inputs": "tt_um_script.inputs"}
    result.write_text(json.dumps(data))
    d = by_id(load(videos_repo=repo, extras=None), "tt06/tt_um_script")
    assert (d.ui_in, d.uio_in, d.scripted) == (2, 128, True)


def test_yaml_first_event_defaults_carry_forward():
    from tthfv.stimulus import first_event_inputs

    assert first_event_inputs([{"at": 0, "ui_in": 48}]) == (48, 0)
    assert first_event_inputs([{"at": 0, "uio_in": 3}, {"at": 5, "ui_in": 1}]) == (0, 3)
    assert first_event_inputs([]) is None


def test_extra_json_has_required_fields():
    entries = json.loads(EXTRA.read_text())
    assert len(entries) == 5
    for e in entries:
        for key in ("id", "repo", "commit", "top_module", "source_files", "clock_hz"):
            assert key in e, (e.get("id"), key)
        assert len(e["commit"]) == 40
