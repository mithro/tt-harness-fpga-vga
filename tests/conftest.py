# SPDX-License-Identifier: Apache-2.0
import pytest

from tthfv import sandbox


@pytest.fixture(autouse=True)
def _no_sandbox_by_default(monkeypatch):
    """The suite must not depend on bubblewrap (CI may not allow user namespaces): the sandbox is off
    unless a test turns it on with TTHFV_SANDBOX=bwrap."""
    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    monkeypatch.setattr(sandbox, "_warned", True)  # one warning per process; not in every test
