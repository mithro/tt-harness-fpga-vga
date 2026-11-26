# SPDX-License-Identifier: Apache-2.0
import json
import shutil
from pathlib import Path

import pytest

from tthfv.manifest import Design, ManifestError, by_id, load

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


def _copy_mini(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(MINI, repo)
    return repo


def test_corrupt_result_json_names_the_design(tmp_path):
    repo = _copy_mini(tmp_path)
    (repo / "data" / "results" / "tt06" / "tt_um_script" / "result.json").write_text("{not json")
    with pytest.raises(ManifestError, match="tt06/tt_um_script"):
        load(videos_repo=repo, extras=None)


def test_corrupt_override_yaml_names_the_design(tmp_path):
    repo = _copy_mini(tmp_path)
    shutil.rmtree(repo / "data" / "overrides-compiled")
    result = repo / "data" / "results" / "tt06" / "tt_um_script" / "result.json"
    data = json.loads(result.read_text())
    data["override"] = {"inputs": "x.inputs"}
    result.write_text(json.dumps(data))
    (repo / "overrides" / "tt06" / "tt_um_script.yaml").write_text("inputs: [unclosed\n")
    with pytest.raises(ManifestError, match="tt06/tt_um_script"):
        load(videos_repo=repo, extras=None)


def test_target_missing_title_names_the_design(tmp_path):
    repo = _copy_mini(tmp_path)
    targets = repo / "data" / "targets.json"
    data = json.loads(targets.read_text())
    del data["targets"][1]["title"]
    targets.write_text(json.dumps(data))
    with pytest.raises(ManifestError, match="tt05/tt_um_fixed.*title"):
        load(videos_repo=repo, extras=None)


def _edit_target(tmp_path: Path, index: int, **changes) -> Path:
    repo = _copy_mini(tmp_path)
    targets = repo / "data" / "targets.json"
    data = json.loads(targets.read_text())
    data["targets"][index].update(changes)
    targets.write_text(json.dumps(data))
    return repo


def test_target_id_must_match_shape(tmp_path):
    repo = _edit_target(tmp_path, 1, id="tt05/bad-name", macro="bad-name")
    with pytest.raises(ManifestError, match="bad-name"):
        load(videos_repo=repo, extras=None)


def test_target_id_must_match_shuttle_and_macro(tmp_path):
    repo = _edit_target(tmp_path, 1, macro="tt_um_other_name")
    with pytest.raises(ManifestError, match="tt05/tt_um_fixed"):
        load(videos_repo=repo, extras=None)


def _extras(tmp_path: Path, **changes) -> Path:
    entry = {
        "id": "calibration/tt_um_c",
        "repo": "https://example.com/c",
        "commit": "c" * 40,
        "top_module": "tt_um_c",
        "source_files": ["c.v"],
        "clock_hz": 25175000,
    }
    entry.update(changes)
    path = tmp_path / "extra.json"
    path.write_text(json.dumps([entry]))
    return path


def test_extra_ok(tmp_path):
    assert "calibration/tt_um_c" in [d.id for d in load(videos_repo=MINI, extras=_extras(tmp_path))]


def test_extra_id_must_match_shape(tmp_path):
    with pytest.raises(ManifestError, match="no-slash"):
        load(videos_repo=MINI, extras=_extras(tmp_path, id="no-slash"))


def test_extra_id_must_end_in_top_module(tmp_path):
    with pytest.raises(ManifestError, match="calibration/tt_um_x"):
        load(videos_repo=MINI, extras=_extras(tmp_path, id="calibration/tt_um_x"))


def test_extra_missing_field_names_the_design(tmp_path):
    path = tmp_path / "extra.json"
    path.write_text(json.dumps([{"id": "calibration/tt_um_c", "top_module": "tt_um_c"}]))
    with pytest.raises(ManifestError, match="calibration/tt_um_c"):
        load(videos_repo=MINI, extras=path)


def test_duplicate_ids_are_rejected(tmp_path):
    with pytest.raises(ManifestError, match="duplicate.*tt05/tt_um_fixed"):
        load(videos_repo=MINI, extras=_extras(tmp_path, id="tt05/tt_um_fixed", top_module="tt_um_fixed"))


def test_extra_json_has_required_fields():
    entries = json.loads(EXTRA.read_text())
    assert len(entries) == 5
    for e in entries:
        for key in ("id", "repo", "commit", "top_module", "source_files", "clock_hz"):
            assert key in e, (e.get("id"), key)
        assert len(e["commit"]) == 40


def _with_testpatterns(tmp_path: Path) -> Path:
    repo = _copy_mini(tmp_path)
    targets = repo / "data" / "targets.json"
    data = json.loads(targets.read_text())
    tp = dict(
        data["targets"][0], id="testpatterns/tt_um_vgacal_x", shuttle="testpatterns", macro="tt_um_vgacal_x"
    )
    data["targets"].append(tp)
    targets.write_text(json.dumps(data))
    return repo


def test_testpatterns_targets_are_not_listed(tmp_path):
    ids = [d.id for d in load(videos_repo=_with_testpatterns(tmp_path), extras=None)]
    assert ids == [d.id for d in load(videos_repo=MINI, extras=None)]
    assert not any(i.startswith("testpatterns/") for i in ids)


def test_extra_design_is_unaffected_by_the_testpatterns_rule(tmp_path):
    repo = _with_testpatterns(tmp_path)
    extras = _extras(tmp_path)
    ids = [d.id for d in load(videos_repo=repo, extras=extras)]
    assert "calibration/tt_um_c" in ids
    assert not any(i.startswith("testpatterns/") for i in ids)
    # An extra is kept even when its own shuttle is named testpatterns: the rule filters the videos repo only.
    extras = _extras(tmp_path, id="testpatterns/tt_um_c")
    assert "testpatterns/tt_um_c" in [d.id for d in load(videos_repo=repo, extras=extras)]
