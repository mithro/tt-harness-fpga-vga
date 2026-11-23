# SPDX-License-Identifier: Apache-2.0
"""Strings from the manifest reach git only after validation, and never as options."""

import dataclasses
import json
import shutil
from pathlib import Path

import pytest
from gitrepo import commit_files, init
from test_fetch import TOP, design, work  # noqa: F401 - fixtures

from tthfv import fetch
from tthfv.manifest import ManifestError, load

DATA = Path(__file__).parent / "data"
MINI = DATA / "videos-mini"
EXTRA = [
    {
        "id": "calibration/tt_um_x", "top_module": "tt_um_x", "repo": "https://example.com/x",
        "commit": "a" * 40, "source_files": ["a.v"], "clock_hz": 0,
    }
]  # fmt: skip

BAD_COMMITS = [
    "--upload-pack=touch x",
    "-h",
    "abc",  # short
    "A" * 40,  # upper case
    "a" * 39,
    "a" * 41,
    "a" * 39 + "g",
    "a" * 40 + "\n",
    "",
    None,
]
BAD_REPOS = [
    "-oProxyCommand=touch x",
    "--upload-pack=x",
    "file:///etc",
    "http://example.com/a",
    "ssh://git@example.com/a",
    "git@github.com:a/b",
    "/etc",
    "https://example.com/a b",
    "https://example.com/a;b",
    "https://example.com/a?b",
    "https://user@example.com/a",
    "https://example.com/a\n",
    "",
    None,
]


@pytest.fixture
def videos(tmp_path):
    repo = tmp_path / "videos"
    shutil.copytree(MINI, repo)
    # the mini data's commits are short placeholders; make them real-looking
    path = repo / "data" / "targets.json"
    data = json.loads(path.read_text())
    for t in data["targets"]:
        t["commit"] = "a" * 40
    path.write_text(json.dumps(data))
    return repo


def edit_first_target(videos_dir: Path, **fields):
    path = videos_dir / "data" / "targets.json"
    data = json.loads(path.read_text())
    data["targets"][0].update(fields)
    path.write_text(json.dumps(data))
    return data["targets"][0]["id"]


def test_good_records_load(videos):
    assert len(load(videos_repo=videos, extras=None)) == 3


@pytest.mark.parametrize("commit", BAD_COMMITS)
def test_a_bad_commit_in_a_target_names_the_design(videos, commit):
    design_id = edit_first_target(videos, commit=commit)
    with pytest.raises(ManifestError, match="commit") as e:
        load(videos_repo=videos, extras=None)
    assert design_id in str(e.value)


@pytest.mark.parametrize("repo", BAD_REPOS)
def test_a_bad_repo_in_a_target_names_the_design(videos, repo):
    design_id = edit_first_target(videos, repo=repo)
    with pytest.raises(ManifestError, match="repo") as e:
        load(videos_repo=videos, extras=None)
    assert design_id in str(e.value)


@pytest.mark.parametrize("commit", BAD_COMMITS[:6])
def test_a_bad_commit_in_an_extra_is_rejected(videos, tmp_path, commit):
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps([{**EXTRA[0], "commit": commit}]))
    with pytest.raises(ManifestError, match="calibration/tt_um_x"):
        load(videos_repo=videos, extras=extra)


@pytest.mark.parametrize("repo", BAD_REPOS[:6])
def test_a_bad_repo_in_an_extra_is_rejected(videos, tmp_path, repo):
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps([{**EXTRA[0], "repo": repo}]))
    with pytest.raises(ManifestError, match="calibration/tt_um_x"):
        load(videos_repo=videos, extras=extra)


def test_a_good_extra_loads(videos, tmp_path):
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps(EXTRA))
    assert any(d.id == "calibration/tt_um_x" for d in load(videos_repo=videos, extras=extra))


def test_real_extras_file_loads(videos):
    assert len([d for d in load(videos_repo=videos, extras=fetch.manifest.EXTRAS_DEFAULT) if d.shuttle == "calibration"]) == 5


def test_a_commit_that_looks_like_an_option_is_not_run_by_git(tmp_path, work):  # noqa: F811
    marker = tmp_path / "pwned"
    repo = init(tmp_path / "proj")
    commit_files(repo, {"src/a.v": TOP})
    d = design(repo, f"--upload-pack=touch {marker}")
    r = fetch.fetch(d)
    assert r.status == "fetch-fail"
    assert not marker.exists()


def test_a_repo_that_looks_like_an_option_is_not_taken_as_one(tmp_path, work):  # noqa: F811
    marker = tmp_path / "pwned"
    d = dataclasses.replace(design(tmp_path, "0" * 40), repo=f"--upload-pack=touch {marker}")
    r = fetch.fetch(d)
    assert r.status == "fetch-fail"
    assert not marker.exists()
    log = r.log.read_text()
    assert "remote add origin" not in log.replace("remote add -- origin", "")


def test_every_manifest_derived_git_argument_follows_a_double_dash(tmp_path, work):  # noqa: F811
    repo = init(tmp_path / "proj")
    c = commit_files(repo, {"src/a.v": TOP})
    r = fetch.fetch(design(repo, c))
    assert r.status == "ok"
    lines = [ln for ln in r.log.read_text().splitlines() if ln.startswith("$ git ")]
    add = next(ln for ln in lines if " remote add " in ln)
    fetch_line = next(ln for ln in lines if " fetch " in ln)
    assert add.split(" -- ")[1].startswith("origin ")
    assert fetch_line.split(" -- ")[1] == f"origin {c}"
