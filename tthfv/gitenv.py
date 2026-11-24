# SPDX-License-Identifier: Apache-2.0
"""The environment every git command of this project runs in.

git runs outside the sandbox (it needs the network) on repositories, commits and submodule URLs chosen by
third parties. So nothing the owner configured for git may apply, because it can name programs that git then
runs on repository-controlled input: filters (git-lfs), insteadOf rewrites, sshCommand, credential helpers,
aliases. The environment is built from nothing, not copied:

- no system or global git configuration (GIT_CONFIG_NOSYSTEM, GIT_CONFIG_GLOBAL=/dev/null), an empty HOME;
- only https is allowed (GIT_ALLOW_PROTOCOL, and protocol.allow=never with https allowed in the injected
  configuration): no ssh (a hostile submodule URL cannot make git run ssh with the owner's keys), no ext::,
  no file::, no git://; ssh itself is also replaced by /bin/false;
- no hooks, no filesystem monitor, no git-lfs smudge, no prompts, no askpass.

The injected configuration goes through GIT_CONFIG_COUNT, which reaches git's own children (submodule
update) the way -c does, and keeps `git <subcommand>` at the same place in every command line."""

import os
from pathlib import Path

from tthfv import paths

# Set to 1 by the test suite, whose repositories are local paths. Never set by the tool itself.
ALLOW_FILE_ENV = "TTHFV_TEST_ALLOW_FILE_PROTOCOL"
# Network settings, not git settings: how to reach https hosts through a proxy and which CAs to trust.
_NETWORK = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY",
    "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
)  # fmt: skip


def home_dir() -> Path:
    """An empty directory for HOME, under the work directory."""
    d = paths.WORK / ".git-home"
    d.mkdir(parents=True, exist_ok=True)
    return d


def git_env() -> dict[str, str]:
    allow_file = os.environ.get(ALLOW_FILE_ENV) == "1"
    config = [
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("protocol.ext.allow", "never"),
        ("protocol.file.allow", "always" if allow_file else "never"),
    ]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir()),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
        "GIT_ALLOW_PROTOCOL": "https:file" if allow_file else "https",
        "GIT_SSH_COMMAND": "/bin/false",
        "GIT_CONFIG_COUNT": str(len(config)),
    }
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    env.update({k: os.environ[k] for k in _NETWORK if k in os.environ})
    return env
