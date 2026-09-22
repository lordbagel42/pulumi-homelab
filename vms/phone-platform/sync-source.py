#!/usr/bin/env python3
"""Snapshot only reviewed public application files for the phone VM deployment."""
import argparse
from pathlib import Path
import shutil
import tempfile

ALLOWED = {"phone_platform", "web", "deploy", "bin", "requirements.txt", "README.md", "SPEECH-ENGINE.md"}
EXCLUDED = {"node_modules", ".venv", ".git", "__pycache__", "state", "models", ".env", "admin-token"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", type=Path, help="Path to the phone-platform directory")
    args = parser.parse_args()
    source = args.platform.resolve()
    target = Path(__file__).resolve().parent / "source"
    if not (source / "phone_platform/__main__.py").is_file():
        raise ValueError("Expected a complete phone-platform source directory")

    def ignore(directory, names):
        ignored = []
        for name in names:
            file = Path(directory) / name
            if name in EXCLUDED or file.is_symlink() or name.endswith((".pyc", ".sqlite", ".sqlite3")):
                ignored.append(name)
        return ignored

    with tempfile.TemporaryDirectory(prefix="phone-platform-source-", dir=target.parent) as temp:
        staged = Path(temp) / "source"
        staged.mkdir()
        for name in sorted(ALLOWED):
            item = source / name
            if item.is_symlink():
                raise ValueError("Deployment entries cannot be symlinks")
            if item.is_dir():
                shutil.copytree(item, staged / name, ignore=ignore)
            elif item.is_file():
                shutil.copy2(item, staged / name)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(staged), target)
    print("Public platform source snapshot updated.")


if __name__ == "__main__":
    main()
