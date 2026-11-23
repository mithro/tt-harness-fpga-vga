# SPDX-License-Identifier: Apache-2.0
"""Pinned external inputs."""

VIDEOS_REPO_URL = "https://github.com/mithro/tinytapeout-vga-videos"
VIDEOS_REPO_COMMIT = "148ccf537b5ab86bdddccf9993a914e9dcaf2622"

OSS_CAD_SUITE_VERSION = "2026-09-14"
OSS_CAD_SUITE_URL = (
    "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/"
    "2026-09-14/oss-cad-suite-linux-x64-20260914.tgz"
)
# SHA-256 of that tarball (about 740 MB); `tools install` refuses a download that differs.
OSS_CAD_SUITE_SHA256 = "441b08c30788665be6f7e46d82211dc4849d69e0db613884fef69e7f5df39fb0"
