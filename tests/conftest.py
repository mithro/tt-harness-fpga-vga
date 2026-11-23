# SPDX-License-Identifier: Apache-2.0
import os

import pytest

from tthfv import gitenv, sandbox, tools


@pytest.fixture(autouse=True)
def _no_sandbox_by_default(monkeypatch):
    """The suite must not depend on bubblewrap (CI may not allow user namespaces): the sandbox is off
    unless a test turns it on with TTHFV_SANDBOX=bwrap."""
    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    monkeypatch.setenv("TTHFV_TEST_ALLOW_FILE_PROTOCOL", "1")  # the tests' repositories are local paths
    monkeypatch.setattr(sandbox, "_warned", True)  # one warning per process; not in every test


@pytest.fixture(autouse=True)
def _fake_tools_see_their_control_variables(monkeypatch):
    """tools.env() and gitenv.git_env() pass on only an allow-list of the host's variables. The fake tools
    and the fake git of the tests are steered by FAKE_* variables, so those are added here and nowhere else."""

    def fake_vars() -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k.startswith("FAKE_")}

    for module, name in ((tools, "env"), (gitenv, "git_env")):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, lambda real=real: {**real(), **fake_vars()})
