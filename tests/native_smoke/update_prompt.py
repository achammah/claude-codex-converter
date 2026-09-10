"""Drive real same-upstream update dismissal in a disposable package and home."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import time

import pyte

parser = argparse.ArgumentParser()
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--work', type=Path, required=True)
args = parser.parse_args()
work = args.work.resolve()
work.mkdir(parents=True, exist_ok=False)
package = work / 'package'

def copy_asset(source, target):
    if Path(source).name in {'codex', 'codex-code-mode-host', 'rg', 'codex-linux-sandbox'}:
        return os.link(source, target)
    return shutil.copy2(source, target)

shutil.copytree(args.package, package, copy_function=copy_asset)
manifest = package / 'codex-package.json'
metadata = json.loads(manifest.read_text())
cue = metadata['cueUpdate']
# Use only the disposable bundled descriptor, never a prepublication remote feed.
cue.pop('feedUrl', None)
descriptor_path = package / cue['descriptorFile']
descriptor = json.loads(descriptor_path.read_text())
release = descriptor['releases'][0]
release['sequence'] = cue['sequence'] + 1
release['releaseId'] = 'fixture-next-patch'
descriptor_path.write_text(json.dumps(descriptor))
cue['descriptorSha256'] = hashlib.sha256(descriptor_path.read_bytes()).hexdigest()
manifest.write_text(json.dumps(metadata))
home, project = work / 'home', work / 'project'
home.mkdir()
project.mkdir()
(home / 'config.toml').write_text(
    'model="gpt-6-astra"\nmodel_provider="fixture"\n'
    'check_for_update_on_startup=true\n'
    '[model_providers.fixture]\nname="Local fixture"\n'
    'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
    '[projects.' + json.dumps(str(project)) + ']\ntrust_level="trusted"\n')
env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG') if k in os.environ}
env.update(CODEX_HOME=str(home), TERM='xterm-256color')

def drive(name, dismiss=False, warm=False):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
    process = subprocess.Popen([str(package / 'bin/codex'), '--no-alt-screen', '-C', str(project)],
                               stdin=slave, stdout=slave, stderr=slave, env=env, cwd=project,
                               start_new_session=True)
    os.close(slave)
    screen = pyte.Screen(110, 32)
    stream = pyte.Stream(screen)
    raw = bytearray()
    visible = ''
    saw_prompt = False
    sent_dismissal = False
    prompt_seen_at = None
    started_at = time.monotonic()
    deadline = time.monotonic() + 20
    try:
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                raw.extend(data)
                for query, reply in [(b'\x1b[6n', b'\x1b[1;1R'), (b'\x1b[c', b'\x1b[?1;2c'),
                                     (b'\x1b[>c', b'\x1b[>0;0;0c'), (b'\x1b[?u', b'\x1b[?0u')]:
                    if query in data:
                        os.write(master, reply)
                stream.feed(data.decode(errors='replace'))
                visible = '\n'.join(screen.display)
                if 'Skip until next version' in visible:
                    saw_prompt = True
                    (work / (name + '-prompt.txt')).write_text(visible)
                    if prompt_seen_at is None:
                        prompt_seen_at = time.monotonic()
                    if not dismiss:
                        break
            if dismiss and not sent_dismissal and prompt_seen_at is not None and time.monotonic() - prompt_seen_at >= .3:
                os.write(master, b'3')
                sent_dismissal = True
            cache_path = home / 'cue-version.json'
            if warm and cache_path.exists():
                break
            if sent_dismissal and cache_path.exists():
                if json.loads(cache_path.read_text()).get('dismissed') == 'fixture-next-patch':
                    break
            if not warm and not dismiss and time.monotonic() - started_at >= 3 and 'Ask Codex' in visible and 'loading' not in visible:
                break
    finally:
        (work / (name + '-screen.txt')).write_text(visible)
        (work / (name + '.raw')).write_bytes(raw)
        if process.poll() is None:
            os.write(master, b'\x03')
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
        os.close(master)
    return saw_prompt

drive('warm', warm=True)
first = drive('offered', dismiss=True)
cache = json.loads((home / 'cue-version.json').read_text())
second = drive('dismissed')
prompt = (work / 'offered-prompt.txt').read_text() if first else ''
passed = (first and not second and cache.get('dismissed') == 'fixture-next-patch'
          and 'fixture-next-patch' in prompt and 'brew upgrade' not in prompt)
report = {'passed': passed, 'same_upstream_version': metadata['version'],
          'offered_patch_release': first, 'dismissed_release_id': cache.get('dismissed'),
          'offered_again_after_dismissal': second, 'scope': 'actual native TUI with disposable future-release descriptor; no model calls or update application'}
(work / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report))
raise SystemExit(0 if passed else 1)
