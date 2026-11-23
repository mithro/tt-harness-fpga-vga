# SPDX-License-Identifier: Apache-2.0
import os

import pytest

from tthfv import sandbox, tools


@pytest.fixture(autouse=True)
def _no_sandbox_by_default(monkeypatch):
    """The suite must not depend on bubblewrap (CI may not allow user namespaces): the sandbox is off
    unless a test turns it on with TTHFV_SANDBOX=bwrap."""
    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    monkeypatch.setenv("TTHFV_TEST_ALLOW_FILE_PROTOCOL", "1")  # the tests' repositories are local paths
    monkeypatch.setattr(sandbox, "_warned", True)  # one warning per process; not in every test


@pytest.fixture(autouse=True)
def _fake_tools_see_their_control_variables(monkeypatch):
    """tools.env() passes on only an allow-list of the host's variables. The fake tools of the build
    tests are steered by FAKE_* variables, so those are added here and nowhere else."""
    real = tools.env
    monkeypatch.setattr(
        tools, "env", lambda: {**real(), **{k: v for k, v in os.environ.items() if k.startswith("FAKE_")}}
    )
