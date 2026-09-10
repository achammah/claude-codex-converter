#!/usr/bin/env python3
"""Reviewable installation and rollback for a generated host setup.

plan STAGING PROJECT --plan FILE
apply --plan FILE --receipt FILE
rollback --receipt FILE

Plan lists every destination and conflict. Apply rejects source or target drift,
backs up replaced files, and does not modify Codex's project/hook trust database.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def record(path):
    if path.is_symlink():
        return {'kind': 'symlink', 'link': os.readlink(path)}
    if path.is_file():
        data = path.read_bytes()
        return {'kind': 'file', 'sha256': sha(data), 'mode': stat.S_IMODE(path.stat().st_mode)}
    if path.exists():
        raise ValueError(f'Expected a file; found another filesystem object: {path}')
    return {'kind': 'absent'}


def safe_parent(path, root):
    if not root.is_absolute() or root.resolve() != root or root.is_symlink():
        raise ValueError('Selected project root was replaced or contains a symlink.')
    if not path.is_relative_to(root) or '..' in path.relative_to(root).parts:
        raise ValueError('Destination escaped selected project.')
    p = path.parent
    while p != root:
        if p.is_symlink():
            raise ValueError(f'Refusing symlinked destination directory: {p}')
        p = p.parent


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2)
        f.write('\n')
    path.chmod(0o600)


def write_atomic(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.cue-install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def payload(source, stage, target):
    data = source.read_bytes()
    relative = source.relative_to(stage)
    if relative.parts[0] == '.cue-source-archive':
        return data
    if relative == Path('.cue/file-manifest.json'):
        rows = json.loads(data)
        for row in rows:
            if row.get('target'):
                item = stage / row['target']
                if not item.is_relative_to(stage) or '..' in Path(row['target']).parts:
                    raise ValueError('Manifest contains an unsafe target path.')
                if item == source:
                    raise ValueError('Manifest cannot attest its own final content recursively.')
                row['target_sha256'] = sha(payload(item, stage, target))
        data = (json.dumps(rows, indent=2, ensure_ascii=False) + '\n').encode()
    # Generated hook commands and project paths must refer to installation root.
    try:
        return data.decode().replace(str(stage), str(target)).encode()
    except UnicodeDecodeError:
        return data


def plan(stage, target, plan_path):
    if plan_path.exists():
        raise ValueError('Plan already exists; choose a new path.')
    if stage == target or stage.is_relative_to(target) and stage == target / '.claude':
        raise ValueError('Staging must be distinct from live source setup.')
    entries = []
    for source in sorted(stage.rglob('*')):
        if '__pycache__' in source.parts or not (source.is_file() or source.is_symlink()):
            continue
        rel = source.relative_to(stage)
        dest = target / rel
        safe_parent(dest, target)
        old, src = record(dest), record(source)
        if src['kind'] == 'symlink':
            link = src['link']
            if Path(link).is_absolute() or not (dest.parent / link).resolve().is_relative_to(target):
                raise ValueError(f'Generated link leaves destination project: {rel}')
            new = src
        else:
            new = {'kind': 'file', 'sha256': sha(payload(source, stage, target)), 'mode': src['mode']}
        if old != new:
            entries.append({'path': str(rel), 'source': src, 'before': old, 'after': new})
    result = {'version': 1, 'stage': str(stage), 'target': str(target), 'entries': entries}
    private(plan_path, result)
    print(json.dumps({'plan': str(plan_path), 'files': len(entries),
                      'replacements': [e['path'] for e in entries if e['before']['kind'] != 'absent']}, indent=2))


def apply(plan_path, receipt_path):
    if receipt_path.exists():
        raise ValueError('Receipt already exists; do not repeat an installation blindly.')
    p = json.loads(plan_path.read_text())
    stage, target = Path(p['stage']), Path(p['target'])
    if stage.resolve() != stage:
        raise ValueError('Staging root changed after review.')
    destinations = {target / e['path'] for e in p['entries']}
    if receipt_path.resolve() in {d.resolve() for d in destinations} or receipt_path.resolve().is_relative_to(stage):
        raise ValueError('Receipt must not overwrite an installation destination or staging file.')
    for e in p['entries']:
        dest = target / e['path']; safe_parent(dest, target)
        if Path(e['path']).is_absolute() or '..' in Path(e['path']).parts:
            raise ValueError('Plan contains an unsafe relative path.')
        if e['after']['kind'] == 'symlink':
            link = e['after']['link']
            if Path(link).is_absolute() or not (dest.parent / link).resolve().is_relative_to(target):
                raise ValueError('Planned symlink escapes the selected project.')
        if record(stage / e['path']) != e['source'] or record(dest) != e['before']:
            raise ValueError('Source or destination changed since review: ' + e['path'])
    receipt = {'version': 1, 'target': str(target), 'state': 'applying', 'changes': []}
    private(receipt_path, receipt)
    for e in p['entries']:
        dest, source = target / e['path'], stage / e['path']
        backup = dict(e)
        if e['before']['kind'] == 'file':
            backup['original_base64'] = base64.b64encode(dest.read_bytes()).decode()
        receipt['changes'].append(backup)
        private(receipt_path, receipt)  # Write recovery evidence before mutation.
        if e['after']['kind'] == 'symlink':
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.parent / ('.cue-link-' + next(tempfile._get_candidate_names()))
            tmp.symlink_to(e['after']['link'])
            os.replace(tmp, dest)
        else:
            write_atomic(dest, payload(source, stage, target), e['after']['mode'])
        if record(dest) != e['after']:
            raise RuntimeError('Installed bytes did not match plan: ' + e['path'])
    receipt['state'] = 'installed'
    private(receipt_path, receipt)
    print(json.dumps({'state': 'installed', 'files': len(receipt['changes']), 'receipt': str(receipt_path),
                      'trust': 'Project and exact hook trust still require the native host review.'}))


def rollback(receipt_path):
    receipt = json.loads(receipt_path.read_text())
    if receipt['state'] == 'rolled-back':
        raise ValueError('Already rolled back.')
    root = Path(receipt['target'])
    for e in receipt['changes']:
        dest = root / e['path']; safe_parent(dest, root)
        if record(dest) not in (e['after'], e['before']):
            raise ValueError('User changes detected; refusing to overwrite: ' + e['path'])
    for e in reversed(receipt['changes']):
        dest = root / e['path']
        if record(dest) == e['before']:
            continue
        if e['before']['kind'] == 'absent':
            dest.unlink()
        elif e['before']['kind'] == 'file':
            write_atomic(dest, base64.b64decode(e['original_base64']), e['before']['mode'])
        else:
            dest.unlink(); dest.symlink_to(e['before']['link'])
    receipt['state'] = 'rolled-back'
    private(receipt_path, receipt)
    print('Rolled back recorded changes; retained audit receipt and empty directories.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('plan'); p.add_argument('stage', type=Path); p.add_argument('target', type=Path); p.add_argument('--plan', type=Path, required=True)
    p = sub.add_parser('apply'); p.add_argument('--plan', type=Path, required=True); p.add_argument('--receipt', type=Path, required=True)
    p = sub.add_parser('rollback'); p.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == 'plan':
            plan(args.stage.resolve(), args.target.resolve(), args.plan)
        elif args.action == 'apply':
            apply(args.plan, args.receipt)
        else:
            rollback(args.receipt)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
