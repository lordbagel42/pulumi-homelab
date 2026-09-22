#!/usr/bin/env python3
"""Copy only public voice source; runtime state and credentials travel separately."""
import argparse
from pathlib import Path
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("project", type=Path)
args = parser.parse_args()
source = args.project.resolve() / "voice"
destination = Path(__file__).resolve().parent / "source"
files = list(source.glob("*.py")) + list(source.glob("tests/*.py"))
files += [source / name for name in ("requirements.txt", "README.md", "HOME_ASSISTANT.md")]
files += list(source.glob("amp_*.ts")) + list(source.glob("AMP*.md"))
selected = {file.relative_to(source) for file in files}
if destination.exists():
    unexpected = [p for p in destination.rglob("*") if p.is_file() and p.relative_to(destination) not in selected]
    if unexpected:
        raise ValueError(f"Review unexpected source files before removing: {unexpected}")
for file in files:
    if file.is_symlink() or not file.resolve().is_relative_to(source):
        raise ValueError(f"Unsafe source path: {file}")
    target = destination / file.relative_to(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(file, target)
print(f"Copied {len(files)} public source files; excluded credentials, models, and session state.")
