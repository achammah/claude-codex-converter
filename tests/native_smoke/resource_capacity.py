"""Drive native CLI startup from soft NOFILE 256; verify its status child's capacity."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import resource
import select
import struct
import subprocess
import sys
import termios
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    root = args.work.resolve()
    root.mkdir(parents=True, exist_ok=False)
    binary = args.package.resolve() / 'bin/codex'
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard < 1024:
        raise SystemExit('Capacity proof requires an existing hard limit of at least 1024; no limit is broadened beyond it.')
    home, project = root / 'home', root / 'project'
    home.mkdir()
    project.mkdir()
    provider = root / 'provider.py'
    provider.write_text('''import json, resource, sys
from pathlib import Path
sys.stdin.read()
handles = []
error = None
try:
    for _ in range(700):
        handles.append(open('/dev/null', 'rb'))
except OSError as exc:
    error = {'errno': exc.errno, 'message': str(exc)}
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
result = {'soft': soft, 'hard': hard, 'opened': len(handles), 'error': error}
for handle in handles:
    handle.close()
Path(sys.argv[1]).write_text(json.dumps(result))
print('CAPACITY_PROBE_STATUS')
''')
    (home / 'config.toml').write_text(
        'model="gpt-6-astra"\nmodel_provider="fixture"\ncheck_for_update_on_startup=false\n'
        '[model_providers.fixture]\nname="Local fixture"\nbase_url="http://127.0.0.1:9/v1"\n'
        'wire_api="responses"\nrequires_openai_auth=false\n[tui.status_provider]\ncommand='
        + json.dumps([sys.executable, str(provider), str(root / 'provider-result.json')])
        + '\nrefresh_interval_ms=1000\ntimeout_ms=3000\nmax_lines=1\n[projects.'
        + json.dumps(str(project)) + ']\ntrust_level="trusted"\n')
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 100, 0, 0))
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG') if k in os.environ}
    env.update(CODEX_HOME=str(home), TERM='xterm-256color')
    def low_limit():
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    proc = subprocess.Popen([str(binary), '--no-alt-screen', '-C', str(project)],
        stdin=slave, stdout=slave, stderr=slave, env=env, cwd=project,
        preexec_fn=low_limit, start_new_session=True)
    os.close(slave)
    raw = bytearray()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and proc.poll() is None:
            if select.select([master], [], [], .1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                raw.extend(data)
                for query, answer in ((b'\x1b[6n', b'\x1b[1;1R'), (b'\x1b[c', b'\x1b[?1;2c'),
                                      (b'\x1b[>c', b'\x1b[>0;0;0c'), (b'\x1b[?u', b'\x1b[?0u')):
                    if query in data:
                        os.write(master, answer)
            if (root / 'provider-result.json').exists() and b'CAPACITY_PROBE_STATUS' in raw:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        os.close(master)
    (root / 'terminal.raw').write_bytes(raw)
    result_path = root / 'provider-result.json'
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    passed = bool(result and result['opened'] == 700 and result['soft'] >= 1024
                  and result['hard'] == hard and result['error'] is None
                  and b'CAPACITY_PROBE_STATUS' in raw)
    report = {'passed': passed, 'initial_soft': 256, 'initial_hard': hard, 'provider': result,
              'status_rendered': b'CAPACITY_PROBE_STATUS' in raw,
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'scope': 'actual native CLI startup and status child; no model calls; hard limit unchanged'}
    (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
