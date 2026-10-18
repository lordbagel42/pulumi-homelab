"""Allow release workspace version reconciliation without dependency updates."""

import sys
from pathlib import Path

import tomllib


def dependencies(path):
    with Path(path).open("rb") as source:
        packages = tomllib.load(source)["package"]
    return {
        (package["name"], package["version"], package["source"]): package
        for package in packages
        if "source" in package
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: verify-codex-lock.py ORIGINAL RESOLVED")
    if dependencies(sys.argv[1]) != dependencies(sys.argv[2]):
        sys.exit("Codex non-workspace dependencies changed; refusing an unpinned build")
    print("Release workspace reconciled; all external dependency pins are unchanged")
