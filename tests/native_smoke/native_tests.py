#!/usr/bin/env python3
"""Run focused tests importing the candidate's real native source modules."""
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--cargo', default='cargo')
    parser.add_argument('--target-dir')
    args = parser.parse_args()
    source = Path(args.source).resolve() / 'codex-rs'
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((source / 'Cargo.toml').read_text())['workspace']['package']['version']
    quote = json.dumps
    manifest = '''[package]
name="cue-updater-focused"
version="0.1.0"
edition="2024"
[lib]
path="lib.rs"
[dependencies]
'''
    for name, path in [('codex-utils-absolute-path', 'utils/absolute-path'), ('codex-utils-home-dir', 'utils/home-dir')]:
        manifest += name + '={path=' + quote(str(source / path)) + '}\n'
    manifest += '''semver={version="1",features=["serde"]}
serde={version="1",features=["derive"]}
serde_json="1"
sha2="0.10"
shlex="1"
pretty_assertions="1"
tempfile="3"
libc="0.2"
'''
    (work / 'Cargo.toml').write_text(manifest)
    (work / 'lib.rs').write_text('extern crate self as codex_install_context;\n#[path=' + quote(str(source / 'install-context/src/lib.rs')) + ']\nmod actual_install_context;\npub use actual_install_context::*;\n#[path=' + quote(str(source / 'tui/src/update_action.rs')) + ']\nmod actual_update_action;\n')
    lock = (Path(__file__).parent / 'harness-Cargo.lock').read_text()
    chunks = lock.split('[[package]]')
    for index, chunk in enumerate(chunks[1:], 1):
        item = tomllib.loads('[[package]]' + chunk)['package'][0]
        if item['name'] in ('codex-utils-absolute-path', 'codex-utils-home-dir') and 'source' not in item:
            chunks[index] = chunk.replace('version = ' + quote(item['version']), 'version = ' + quote(version), 1)
    (work / 'Cargo.lock').write_text('[[package]]'.join(chunks))
    env = dict(os.environ)
    env['CODEX_HOME'] = str(work / 'codex-home')
    if args.target_dir:
        env['CARGO_TARGET_DIR'] = str(Path(args.target_dir).resolve())
    command = [args.cargo, 'test', '--manifest-path', str(work / 'Cargo.toml'), '--locked', '-j', '1']
    with (work / 'tests.log').open('w') as output:
        result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT)
    summaries = re.findall(r'^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed;', (work / 'tests.log').read_text(), re.MULTILINE)
    passed_count = sum(int(row[1]) for row in summaries)
    failed_count = sum(int(row[2]) for row in summaries)
    passed = result.returncode == 0 and passed_count > 0 and failed_count == 0 and all(row[0] == 'ok' for row in summaries)
    report = {'passed': passed, 'passedTests': passed_count, 'failedTests': failed_count, 'source': str(source.parent), 'version': version, 'command': command, 'exitCode': result.returncode, 'log': str(work / 'tests.log'), 'scope': 'actual install-context and update-action modules; no behavior stubs; separate pinned harness dependencies'}
    (work / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return 0 if passed else (result.returncode or 1)


if __name__ == '__main__':
    raise SystemExit(main())
