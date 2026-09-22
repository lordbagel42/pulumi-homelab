#!/usr/bin/env python3
"""Refresh the deployable source snapshot, excluding credentials and local state."""
import argparse
from pathlib import Path
import shutil

FILES = (
    'README.md', 'recovery/README.md',
    'docker-compose.yml', 'docker-compose.huddle.yml',
    'pbx/Dockerfile', 'pbx/app_audiosocket.c', 'tftp/Dockerfile',
    'asterisk/sccp.conf', 'asterisk/extensions.conf', 'asterisk/extensions_huddle.conf', 'asterisk/rtp.conf',
    'huddle-phone/Dockerfile', 'huddle-phone/.dockerignore', 'huddle-phone/.gitignore',
    'huddle-phone/.env.example', 'huddle-phone/configure.py', 'huddle-phone/README.md',
    'huddle-phone/package.json', 'huddle-phone/package-lock.json',
    'huddle-phone/requirements.in', 'huddle-phone/requirements.txt',
    'huddle-phone/web/relay.html', 'huddle-phone/web/relay.js', 'huddle-phone/web/pcm-worklet.js',
)
PATTERNS = ('huddle-phone/huddle_phone/*.py', 'huddle-phone/tests/*.py',
            'huddle-phone/tests/*.test.js', 'tftp/files/*.cnf.xml',
            'tftp/files/*.loads', 'tftp/files/*.sbn')


def sync(source: Path, destination: Path):
    source = source.resolve()
    paths = [source / name for name in FILES]
    paths += [file for pattern in PATTERNS for file in sorted(source.glob(pattern))]
    for file in paths:
        if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(source):
            raise ValueError(f'Not a regular source file: {file}')
    selected = {file.relative_to(source) for file in paths}
    if destination.exists():
        for file in destination.rglob('*'):
            if file.is_file() and file.relative_to(destination) not in selected:
                raise ValueError(f'Unexpected snapshot file; review before removing: {file}')
    for file in paths:
        target = destination / file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
    print(f'Synchronized {len(paths)} source files; no credentials, build output or runtime state copied.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    sync(args.source, Path(__file__).resolve().parent / 'project')
