"""Pick two released images for the customer-installer rehearsal (plan R8.7).

    python3 scripts/customer/ci_released_images.py <base-commit>   # prints OLD_IMAGE=… and NEW_IMAGE=…

Walks the first-parent history of <base-commit> (main) and returns the two newest commits whose
image CI pushed to GHCR, as digest references: NEW is the newest, OLD the one before. The PR's
own image is not pushed until it merges; the rehearsal tests the PR's installer (compose file
and scripts) against released images. Standard library only; the registry is read anonymously.
"""

from __future__ import annotations

import json
# subprocess: one fixed `git rev-list` argv, no shell.
import subprocess  # nosec B404
import sys
import urllib.error
import urllib.request

REPOSITORY = "phamxuankhiem/ceph-ai"
REGISTRY = "https://ghcr.io"
_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
_MAX_COMMITS = 15


def _token() -> str:
    # REGISTRY is the constant https://ghcr.io, never user input.
    with urllib.request.urlopen(f"{REGISTRY}/token?scope=repository:{REPOSITORY}:pull", timeout=20) as response:  # nosec B310
        return json.load(response)["token"]


def digest_of(tag: str, token: str) -> str | None:
    request = urllib.request.Request(f"{REGISTRY}/v2/{REPOSITORY}/manifests/{tag}", method="HEAD",
                                     headers={"Authorization": f"Bearer {token}", "Accept": _ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310 - https://ghcr.io only
            return response.headers.get("Docker-Content-Digest")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def first_parent_commits(base: str) -> list[str]:
    output = subprocess.run(  # nosec B603 B607 - fixed git argv; base is a commit name from CI
        ["git", "rev-list", "--first-parent", f"--max-count={_MAX_COMMITS}", base],
        check=True, capture_output=True, text=True).stdout
    return output.split()


def released_images(commits: list[str], digest_lookup) -> list[str]:
    found = []
    for commit in commits:
        digest = digest_lookup(commit)
        if digest:
            found.append(f"ghcr.io/{REPOSITORY}@{digest}")
            if len(found) == 2:
                break
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    token = _token()
    images = released_images(first_parent_commits(argv[1]), lambda commit: digest_of(commit, token))
    if len(images) < 2:
        print(f"need two released images in the last {_MAX_COMMITS} commits of {argv[1]}, found {len(images)}",
              file=sys.stderr)
        return 1
    print(f"NEW_IMAGE={images[0]}")
    print(f"OLD_IMAGE={images[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
