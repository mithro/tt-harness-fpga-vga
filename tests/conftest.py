# SPDX-License-Identifier: Apache-2.0
import os

import pytest

from tthfv import gitenv, limits, sandbox, tools


def pytest_addoption(parser):
    parser.addoption(
        "--with-limits",
        action="store_true",
        default=False,
        help="leave the cgroup resource limits (systemd scopes) ON for the tests that run in the sandbox: "
        "by default the suite sets TTHFV_LIMITS=none; tests/test_limits*.py and test_orphans.py always use them",
    )


@pytest.fixture(autouse=True)
def _no_sandbox_by_default(monkeypatch, request):
    """The suite must not depend on bubblewrap (CI may not allow user namespaces): the sandbox is off
    unless a test turns it on with TTHFV_SANDBOX=bwrap."""
    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    monkeypatch.setenv("TTHFV_TEST_ALLOW_FILE_PROTOCOL", "1")  # the tests' repositories are local paths
    monkeypatch.setattr(sandbox, "_warned", True)  # one warning per process; not in every test
    if not request.config.getoption("--with-limits"):
        monkeypatch.setenv(
            "TTHFV_LIMITS", "none"
        )  # no systemd scopes either; tests/test_limits.py turns them on
    monkeypatch.setattr(limits, "_warned", True)


@pytest.fixture(autouse=True)
def _fake_tools_see_their_control_variables(monkeypatch):
    """tools.env() and gitenv.git_env() pass on only an allow-list of the host's variables. The fake tools
    and the fake git of the tests are steered by FAKE_* variables, so those are added here and nowhere else."""

    def fake_vars() -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k.startswith("FAKE_")}

    for module, name in ((tools, "env"), (gitenv, "git_env")):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, lambda real=real: {**real(), **fake_vars()})


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch):
    """The free-space guards (tthfv.paths.require_free) look at the real disk; the suite must not depend on how
    much is free on the machine that runs it. tests/test_disk_guards.py injects its own readers."""
    from tthfv import paths

    monkeypatch.setattr(paths, "free_bytes", lambda path: 1 << 50)
