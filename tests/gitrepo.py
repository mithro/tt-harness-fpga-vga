# SPDX-License-Identifier: Apache-2.0
"""Helpers to make small local git repositories for tests."""

import os
import subprocess
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **IDENTITY)
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


def init(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    return repo


def commit_files(repo: Path, files: dict[str, str], msg: str = "c") -> str:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return git(repo, "rev-parse", "HEAD")
