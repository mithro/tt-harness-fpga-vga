# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""A repository can make src/ (or the project subdirectory) a symlink to anywhere: the fetch stage must refuse,
not walk and hash what it points at."""

import os

from test_fetch import TOP, commit_files, design, git, project, work  # noqa: F401 - fixtures

from tthfv import fetch


def commit_symlink(repo, link: str, target: str) -> str:
    os.makedirs((repo / link).parent, exist_ok=True)
    os.symlink(target, repo / link)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    return git(repo, "rev-parse", "HEAD")


def test_src_as_an_absolute_symlink_is_refused(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.v").write_text(TOP)
    c = commit_symlink(project, "src", str(outside))
    r = fetch.fetch(design(project, c))
    assert r.status == "fetch-fail" and "symbolic link" in r.reason, r.reason


def test_src_as_a_relative_symlink_above_the_checkout_is_refused(project, tmp_path, work):
    c = commit_symlink(project, "src", "../../../../..")
    r = fetch.fetch(design(project, c))
    assert r.status == "fetch-fail" and "symbolic link" in r.reason, r.reason
    assert not (work / "src" / "tt99" / "tt_um_x" / "fetch.json").exists()


def test_a_symlinked_project_subdirectory_is_refused(project, tmp_path):
    commit_files(project, {"real/src/a.v": TOP})
    c = commit_symlink(project, "proj", "real")
    r = fetch.fetch(design(project, c, subdir="proj"))
    assert r.status == "fetch-fail" and "symbolic link" in r.reason, r.reason


def test_a_symlinked_src_inside_the_checkout_is_refused_too(project):
    commit_files(project, {"elsewhere/a.v": TOP})
    c = commit_symlink(project, "src", "elsewhere")
    r = fetch.fetch(design(project, c))
    assert r.status == "fetch-fail" and "symbolic link" in r.reason, r.reason


def test_the_digest_walk_refuses_a_symlinked_top_and_a_tree_nested_too_deeply(tmp_path):
    from tthfv import safefile

    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    try:
        fetch.sources_sha256(tmp_path / "link")
    except safefile.NotPlainFile:
        pass
    else:
        raise AssertionError("a symlinked src was walked")
    deep = tmp_path / "deep"
    deep.mkdir()
    fd = os.open(deep, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(300):
        os.mkdir("d", dir_fd=fd)
        nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = nxt
    os.close(fd)
    try:
        fetch.sources_sha256(deep)
    except safefile.TreeTooDeep:
        pass
    else:
        raise AssertionError("a tree 300 deep was walked")
    safefile.remove_tree(deep)
