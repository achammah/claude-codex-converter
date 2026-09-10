#!/usr/bin/env python3
"""Normalize only local workspace versions in a release-tag Cargo.lock."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib


def normalize(source):
    root = Path(source).resolve() / 'codex-rs'
    workspace = tomllib.loads((root / 'Cargo.toml').read_text())['workspace']
    version = workspace['package']['version']
    members = set()
    # Cargo also includes local path dependencies absent from explicit members.
    tracked = subprocess.check_output(['git', '-C', str(root.parent), 'ls-files', '-z', 'codex-rs/**/Cargo.toml']).decode().split('\0')
    for relative in filter(None, tracked):
        path = root.parent / relative
        package = tomllib.loads(path.read_text()).get('package', {})
        if package.get('version') != {'workspace': True}:
            continue
        owner = path.parent
        while owner != root:
            manifest = owner / 'Cargo.toml'
            if manifest.exists() and 'workspace' in tomllib.loads(manifest.read_text()):
                break
            owner = owner.parent
        if owner == root:
            members.add(package['name'])
    lock = root / 'Cargo.lock'
    original = lock.read_text()
    chunks = original.split('[[package]]')
    seen = set()
    changed = []
    for index, chunk in enumerate(chunks[1:], 1):
        record = tomllib.loads('[[package]]' + chunk)['package'][0]
        name = record['name']
        if name not in members or 'source' in record:
            continue
        if name in seen:
            raise ValueError('duplicate local workspace package: ' + name)
        seen.add(name)
        previous = record['version']
        if previous == version:
            continue
        if previous != '0.0.0':
            raise ValueError('unexpected local workspace version: ' + name + '=' + previous)
        replacement, count = re.subn(r'(?m)^version = "0\.0\.0"$', 'version = ' + json.dumps(version), chunk)
        if count != 1:
            raise ValueError('noncanonical lock version entry: ' + name)
        chunks[index] = replacement
        changed.append(name)
    if members - seen:
        raise ValueError('workspace packages absent from lock: ' + ', '.join(sorted(members - seen)))
    updated = '[[package]]'.join(chunks)
    if updated != original:
        lock.write_text(updated)
    return {'version': version, 'changed': changed, 'count': len(changed)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    args = parser.parse_args()
    print(json.dumps(normalize(args.source), sort_keys=True))
