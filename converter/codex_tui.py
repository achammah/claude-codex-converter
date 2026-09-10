#!/usr/bin/env python3
"""Run Codex in a PTY with two project status rows reserved below its TUI."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unicodedata


STATUS_ROWS = 2
MIN_CHILD_ROWS = 4
THREAD_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
OSC_RE = re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)')
CSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def clean_lines(value, limit=STATUS_ROWS):
    """Return terminal-safe, single-row strings from untrusted status output."""
    if not isinstance(value, str):
        return []
    value = OSC_RE.sub('', value)
    value = CSI_RE.sub('', value)
    rows = []
    for raw in value.splitlines():
        row = ''.join(char for char in raw if ord(char) >= 32 and ord(char) != 127)
        row = ' '.join(row.split())
        if row:
            rows.append(row)
        if len(rows) == limit:
            break
    return rows


def cell_width(value):
    width = 0
    for char in value:
        if unicodedata.combining(char) or char in ('\ufe0e', '\ufe0f'):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return width


def fit_cells(value, columns):
    """Fit one safe row inside the terminal without wrapping into Codex's area."""
    if columns <= 0:
        return ''
    suffix = '…'
    if cell_width(value) <= columns:
        return value
    room = max(0, columns - cell_width(suffix))
    out, used = [], 0
    for char in value:
        width = cell_width(char)
        if used + width > room:
            break
        out.append(char)
        used += width
    return ''.join(out) + suffix if columns >= cell_width(suffix) else ''


def terminal_size(fd):
    try:
        rows, columns, _, _ = struct.unpack('HHHH', fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\0' * 8))
    except OSError:
        rows, columns = 24, 80
    return max(rows, MIN_CHILD_ROWS + 1), max(columns, 1)


def set_child_size(fd, rows, columns, reserved=STATUS_ROWS):
    child_rows = max(MIN_CHILD_ROWS, rows - min(reserved, max(0, rows - MIN_CHILD_ROWS)))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', child_rows, columns, 0, 0))
    return rows - child_rows


def channel_path(project, pid):
    root = Path(os.environ.get('CUE_STATE_ROOT') or project / '.cue/state/codex').resolve()
    path = root / 'launcher' / ('%d-%d.json' % (pid, time.time_ns()))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_session(channel):
    try:
        value = json.loads(channel.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    sid = value.get('session_id') if isinstance(value, dict) else None
    return sid.strip() if isinstance(sid, str) and sid.strip() else None


def session_snapshot(codex_home):
    root = Path(codex_home).expanduser() / 'sessions'
    try:
        return {path: path.stat().st_mtime_ns for path in root.rglob('*.jsonl')}
    except OSError:
        return {}


def session_from_args(arguments):
    """A literal `resume <uuid>` is authoritative and needs no file inference."""
    try:
        start = arguments.index('resume') + 1
    except ValueError:
        return None
    for value in arguments[start:]:
        if value == '--':
            break
        if THREAD_ID_RE.fullmatch(value):
            return value
    return None


def discover_session(codex_home, project, baseline):
    """Find one changed Codex transcript for this exact project from metadata only."""
    candidates = {}
    for path, stamp in session_snapshot(codex_home).items():
        if baseline.get(path) == stamp:
            continue
        try:
            with path.open(encoding='utf-8') as handle:
                record = json.loads(handle.readline())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get('payload')
        if record.get('type') != 'session_meta' or not isinstance(payload, dict):
            continue
        sid = payload.get('id') or payload.get('session_id')
        cwd = payload.get('cwd')
        if isinstance(sid, str) and THREAD_ID_RE.fullmatch(sid) and isinstance(cwd, str):
            try:
                same_project = Path(cwd).resolve() == project.resolve()
            except OSError:
                same_project = False
            if same_project:
                candidates[sid] = stamp
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def status_command(args, session_id):
    if args.status_program:
        return [sys.executable, str(args.status_program), '--session', session_id]
    command = [sys.executable, str(args.project / '.cue/scripts/status_line.py'),
               str(args.project), '--session', session_id]
    if args.run_source_status:
        command.extend(['--run-source', '--timeout', str(args.status_timeout)])
    return command


class StatusProcess:
    def __init__(self):
        self.process = None
        self.stdout = None
        self.stderr = None
        self.started = 0.0

    def start(self, command, env):
        self.stdout = tempfile.TemporaryFile()
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(command, stdout=self.stdout, stderr=self.stderr,
                                        cwd=env['CUE_PROJECT_DIR'], env=env,
                                        start_new_session=True)
        self.started = time.monotonic()

    def collect(self, timeout):
        if not self.process:
            return None
        if self.process.poll() is None:
            if time.monotonic() - self.started <= timeout:
                return None
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait()
            self.close()
            return ('timeout', [])
        self.stdout.seek(0)
        output = self.stdout.read(65536).decode('utf-8', errors='replace')
        code = self.process.returncode
        self.close()
        return ('ok', clean_lines(output)) if code == 0 else ('error', [])

    def close(self):
        for stream in (self.stdout, self.stderr):
            if stream:
                stream.close()
        self.process = self.stdout = self.stderr = None

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.close()


def paint(fd, rows, columns, reserved, lines):
    if not reserved:
        return
    visible = list(lines[:reserved])
    visible.extend([''] * (reserved - len(visible)))
    chunks = ['\x1b[s']
    for index, value in enumerate(visible):
        row = rows - reserved + index + 1
        chunks.append('\x1b[%d;1H\x1b[2K%s' % (row, fit_cells(value, columns)))
    chunks.append('\x1b[u')
    os.write(fd, ''.join(chunks).encode('utf-8'))


def clear_rows(fd, rows, reserved):
    if reserved:
        os.write(fd, ('\x1b[s' + ''.join('\x1b[%d;1H\x1b[2K' % row
                 for row in range(rows - reserved + 1, rows + 1)) + '\x1b[u').encode())


def resolve_codex(value):
    candidate = value or os.environ.get('CUE_CODEX_REAL_BINARY') or shutil.which('codex')
    if not candidate:
        raise ValueError('Codex executable was not found on PATH.')
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('Codex executable is not runnable: ' + str(path))
    return str(path)


def run(args):
    codex = resolve_codex(args.codex)
    command = [codex] + list(args.codex_args)
    project = args.project.resolve()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return subprocess.call(command, cwd=project, env=os.environ)

    state_root = Path(os.environ.get('CUE_STATE_ROOT') or project / '.cue/state/codex').resolve()
    channel = channel_path(project, os.getpid())
    env = dict(os.environ, CUE_PROJECT_DIR=str(project), CLAUDE_PROJECT_DIR=str(project),
               CUE_STATE_ROOT=str(state_root),
               CUE_CODEX_STATUS_CHANNEL=str(channel))
    codex_home = Path(env.get('CODEX_HOME') or '~/.codex').expanduser()
    baseline = session_snapshot(codex_home)
    master, slave = os.openpty()
    rows, columns = terminal_size(sys.stdout.fileno())
    reserved = set_child_size(slave, rows, columns)
    child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                             cwd=project, env=env, start_new_session=True, close_fds=True)
    os.close(slave)
    original = termios.tcgetattr(sys.stdin.fileno())
    renderer = StatusProcess()
    session_id = session_from_args(args.codex_args)
    lines = ['Cue status: waiting for Codex session']
    next_refresh = 0.0
    repaint = True

    def resized(_signal, _frame):
        nonlocal rows, columns, reserved, repaint
        rows, columns = terminal_size(sys.stdout.fileno())
        reserved = set_child_size(master, rows, columns)
        try:
            os.killpg(child.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass
        repaint = True

    previous_winch = signal.signal(signal.SIGWINCH, resized)
    try:
        tty.setraw(sys.stdin.fileno())
        while child.poll() is None:
            ready, _, _ = select.select([sys.stdin.fileno(), master], [], [], 0.1)
            if sys.stdin.fileno() in ready:
                data = os.read(sys.stdin.fileno(), 65536)
                if data:
                    os.write(master, data)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b''
                if data:
                    os.write(sys.stdout.fileno(), data)
                    repaint = True
            found = read_session(channel)
            if not found and not session_id:
                found = discover_session(codex_home, project, baseline)
            if found and found != session_id:
                session_id, next_refresh = found, 0.0
            now = time.monotonic()
            result = renderer.collect(args.status_timeout + 1)
            if result:
                state, rendered = result
                if state == 'ok' and rendered:
                    lines = rendered
                elif state == 'timeout':
                    lines = ['Cue status unavailable: render timed out']
                else:
                    lines = ['Cue status unavailable: render failed']
                next_refresh = now + args.refresh_seconds
                repaint = True
            if session_id and renderer.process is None and now >= next_refresh:
                status_env = dict(env, CUE_SESSION_ID=session_id, CODEX_THREAD_ID=session_id)
                renderer.start(status_command(args, session_id), status_env)
                next_refresh = float('inf')
            if repaint:
                paint(sys.stdout.fileno(), rows, columns, reserved, lines)
                repaint = False
    finally:
        renderer.stop()
        signal.signal(signal.SIGWINCH, previous_winch)
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original)
        clear_rows(sys.stdout.fileno(), rows, reserved)
        try:
            channel.unlink()
        except FileNotFoundError:
            pass
        os.close(master)
    code = child.wait()
    return 128 - code if code < 0 else code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--codex')
    parser.add_argument('--status-program', type=Path)
    parser.add_argument('--run-source-status', action='store_true')
    parser.add_argument('--refresh-seconds', type=float, default=2.0)
    parser.add_argument('--status-timeout', type=float, default=5.0)
    parser.add_argument('codex_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.codex_args[:1] == ['--']:
        args.codex_args = args.codex_args[1:]
    if args.refresh_seconds <= 0 or args.status_timeout <= 0:
        parser.error('refresh and timeout values must be positive')
    try:
        return run(args)
    except (OSError, ValueError) as exc:
        print('Cue Codex launcher failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
