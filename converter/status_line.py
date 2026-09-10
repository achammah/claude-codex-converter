"""Session-bound status snapshots for external displays. Never runs source commands.

Codex's native footer accepts built-in item identifiers. This module supplies a
separate JSON/text read surface for hosts which can display custom status data.
Unknown values stay unknown; source hooks, credentials and conversation text are
never copied into the snapshot.
"""
import argparse
import ast
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time


SCHEMA = 1
NATIVE_STATUS_LINE_ITEMS = [
    'model-with-reasoning',
    'current-dir',
    'git-branch',
    'context-remaining',
    'task-progress',
]


def parse_state_roots(bindings):
    result = {}
    for binding in bindings:
        if not isinstance(binding, str) or '=' not in binding:
            raise ValueError('State binding must be OLD=RELATIVE_TARGET.')
        old, new = binding.split('=', 1)
        old = old.rstrip('/')
        parts = Path(new).parts
        if not old or not (old.startswith('~/') or Path(old).is_absolute()):
            raise ValueError('Legacy state root must be an absolute or ~/ path.')
        if old in result:
            raise ValueError('Duplicate legacy state root: ' + old)
        if any(old.startswith(prior + '/') or prior.startswith(old + '/') for prior in result):
            raise ValueError('Overlapping legacy state roots are ambiguous: ' + old)
        if ('\\' in new or Path(new).is_absolute() or '..' in parts or
                parts[:3] != ('.cue', 'state', 'codex') or len(parts) < 4):
            raise ValueError('State target must be a relative descendant of .cue/state/codex.')
        if any(c in old + new for c in ('\n', '\r', '\x00')):
            raise ValueError('State binding contains control characters.')
        result[old] = str(Path(new))
    return dict(sorted(result.items()))


def _recognized_state_roots(output):
    """Recognize the migrated cache helper's explicit environment/fallback pair."""
    helper = output / '.cue/hooks/lib/session_cache.py'
    if not helper.is_file() or not helper.resolve().is_relative_to(output.resolve()):
        return {}
    try:
        tree = ast.parse(helper.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}
    roots = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_root']
    if len(roots) != 1:
        return {}
    returns = [node.value for node in ast.walk(roots[0]) if isinstance(node, ast.Return)]
    env_return = 'os.path.join(os.environ[\'CUE_STATE_ROOT\'], \'sessions\')'
    if sum(ast.unparse(node) == env_return for node in returns) != 1:
        return {}
    fallback = []
    for node in returns:
        if not isinstance(node, ast.Call) or ast.unparse(node.func) != 'os.path.join' or len(node.args) < 2:
            continue
        if ast.unparse(node.args[0]) != "os.path.expanduser('~')":
            continue
        tail = node.args[1:]
        if all(isinstance(part, ast.Constant) and isinstance(part.value, str) for part in tail):
            fallback.append('~/' + '/'.join(part.value for part in tail))
    return {fallback[0]: '.cue/state/codex/sessions'} if len(fallback) == 1 else {}


def _syntax_check(path, content):
    if path.suffix == '.py':
        ast.parse(content, filename=str(path))
        return
    if path.suffix not in ('.sh', '.bash'):
        raise ValueError('No syntax verifier for this source language.')
    checked = subprocess.run(['bash', '-n'], input=content, text=True, capture_output=True)
    if checked.returncode:
        raise ValueError('Shell syntax verification failed.')
    # bash -n does not parse embedded Python, so check that body as well.
    pattern = r'''(?ms)^.*?\bpython(?:3)?\b[^\n]*<<["']?([A-Z][A-Z0-9_]*)["']?[^\n]*\n(.*?)^\1\s*$'''
    for match in re.finditer(pattern, content):
        ast.parse(match[2], filename=str(path) + ':' + match[1])


def _bind_staged_state(output, roots):
    """Only translated runtime source copies are eligible; archives stay untouched."""
    records = []
    for path in sorted((output / '.cue').rglob('*')):
        relative = path.relative_to(output)
        if (not path.is_file() or path.is_symlink() or path.suffix not in ('.py', '.sh', '.bash') or
                any(part in ('tests', 'state', '.converter-runtime') for part in relative.parts) or
                not path.resolve().is_relative_to((output / '.cue').resolve())):
            continue
        try:
            original = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        for old, destination in roots.items():
            if old not in original:
                continue
            # Match a complete prefix, never a partial sibling such as
            # /store-other. Every occurrence moves together so executable code,
            # comments, and embedded help cannot describe different stores.
            pattern = re.compile(re.escape(old) + r'(?=/|[\'"\s])')
            matches = list(pattern.finditer(original))
            record = {'path': str(relative), 'original_literal': old,
                      'target': str(output / destination),
                      'hash_scope': 'conversion-stage',
                      'before_sha256': hashlib.sha256(original.encode()).hexdigest()}
            records.append(record)
            changed = pattern.sub(lambda _: str(output / destination), original)
            try:
                _syntax_check(path, changed)
            except (OSError, SyntaxError, ValueError) as exc:
                record.update(status='manual', reason='Syntax verification failed: ' + type(exc).__name__)
                continue
            path.write_text(changed, encoding='utf-8')
            record.update(status='rebound', matches=len(matches),
                          after_sha256=hashlib.sha256(changed.encode()).hexdigest())
            original = changed
    for old in roots:
        if not any(row['original_literal'] == old for row in records):
            records.append({'original_literal': old, 'target': str(output / roots[old]),
                            'status': 'manual', 'reason': 'No staged literal root was found.'})
    return records


def build_manifest(source, *, translate, project, output, legacy_state_roots=None):
    """Describe the preserved source command without executing or reading home files.

    Static dependencies are candidates, not a claim to understand arbitrary shell
    code. Source JSON (including unfamiliar fields) remains available verbatim.
    """
    project, output = Path(project), Path(output)
    roots = _recognized_state_roots(output)
    roots.update(legacy_state_roots or {})
    # The function API obeys the same boundary as CLI-supplied bindings.
    roots = parse_state_roots([old + '=' + new for old, new in roots.items()])
    original = json.loads(json.dumps(source))
    command = source.get('command') if isinstance(source, dict) else None
    kind = source.get('type') if isinstance(source, dict) else None
    supported = kind == 'command' and isinstance(command, str) and bool(command.strip())
    manifest = {
        'schema': SCHEMA, 'source': original,
        'command': translate(command) if supported else None,
        'bridge_supported': supported,
        'native_footer_supported': True,
        'native_footer_mode': 'builtin-items',
        'native_footer_items': list(NATIVE_STATUS_LINE_ITEMS),
        'source_command_in_native_footer': False,
        'activation': 'generated-cue-codex-launcher-or-explicit-reader',
        'unmapped_fields': sorted(set(source) - {'type', 'command'}) if isinstance(source, dict) else [],
        'payload_fields': ['session_id', 'cwd', 'workspace.current_dir', 'model', 'reasoning_effort', 'effort.level'],
        'unavailable_unless_supplied': ['cost', 'context_window', 'rate_limits', 'session_name'],
        'state_environment': {'CUE_STATE_ROOT': str(output / '.cue/state/codex'),
                              'CUE_PROJECT_DIR': str(output)},
        'dependencies': [],
        'legacy_state_roots': roots,
        'state_rebindings': [],
        'limitations': [
            'Codex renders only configured built-in footer items. The generated cue-codex launcher reserves two terminal rows for the converted source output.',
            'Arbitrary source programs can depend on host APIs, dynamic paths and external state. Static candidates are not a complete dependency graph.',
            'No source command runs during conversion or ordinary status reads. Launching cue-codex or passing --run-source explicitly executes the preserved command with the invoking process permissions.',
            'Claude cost/context fields are not fabricated from missing Codex telemetry.',
        ],
    }
    if not supported:
        manifest['limitations'].append('Unsupported source statusLine shape is preserved; no executable bridge is inferred.')
        return manifest
    manifest['state_rebindings'] = _bind_staged_state(output, roots)
    try:
        tokens = shlex.split(command)
    except ValueError:
        manifest['limitations'].append('Shell command could not be statically tokenized; it is retained verbatim for review.')
        return manifest
    pending = [(token, str(project)) for token in tokens if '/' in token and not token.startswith('-')]
    seen = set()
    while pending:
        token, base = pending.pop(0)
        key = (token, base)
        if key in seen:
            continue
        seen.add(key)
        translated = translate(token)
        row = {'reference': token, 'translated': translated, 'from': base}
        manifest['dependencies'].append(row)
        if any(mark in token for mark in ('$', '`', '~')):
            row['status'] = 'dynamic-or-home-path-unverified'
            continue
        target = Path(translated)
        target = target if target.is_absolute() else output / target
        # Only inspect files already staged by the converter. Never descend into
        # a host home, symlink, arbitrary checkout, config or credential file.
        resolved = target.resolve()
        if not resolved.is_relative_to(output.resolve()) or target.is_symlink():
            row['status'] = 'external-unverified'
            continue
        if not target.is_file():
            row['status'] = 'missing-or-dynamic'
            continue
        row['status'] = 'staged'
        raw = target.read_bytes()
        row['sha256'] = hashlib.sha256(raw).hexdigest()
        if target.suffix not in ('.py', '.sh', '.bash', '.zsh', '.js'):
            continue
        try:
            body = raw.decode('utf-8')
        except UnicodeDecodeError:
            row['status'] = 'staged-binary'
            continue
        if 'cue-session' in body and 'CUE_STATE_ROOT' not in body:
            row['state_binding'] = 'hard-coded cue-session store; does not honor CUE_STATE_ROOT'
        elif 'CUE_STATE_ROOT' in body:
            row['state_binding'] = 'CUE_STATE_ROOT reference present; semantic use needs runtime verification'
        # Quoted relative paths are relative to a program's own semantics, which
        # can differ from its file directory. Retain the reference as evidence.
        for literal in re.findall(r'''["']([^"'\n]+\.(?:py|sh|bash|zsh|js))["']''', body):
            if '/' in literal:
                pending.append((literal, str(target.relative_to(output))))
    manifest['dependencies'].sort(key=lambda row: (row['reference'], row['from']))
    return manifest


def session_key(session_id):
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError('A real session ID is required; no latest-session fallback is used.')
    return hashlib.sha256(session_id.encode()).hexdigest()[:24]


def status_path(state_root, session_id):
    return Path(state_root) / 'status' / (session_key(session_id) + '.json')


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _text(value):
    if not isinstance(value, str):
        return ''
    # No OSC/CSI, newlines, or control characters can reach a terminal title/pane.
    value = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', value)
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ' '.join(''.join(c for c in value if ord(c) >= 32 and ord(c) != 127).split())


def capture_event(data, state_root, *, host='codex', now=None):
    """Save a whitelist of observed host metadata, separately for every session."""
    sid = data.get('session_id') or data.get('thread_id')
    path = status_path(state_root, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_json(path)
        value = previous if previous.get('session_id') == sid else {}
        value.update(schema=SCHEMA, host=host, session_id=sid,
                     observed_at=time.time() if now is None else now)
        for source, target in [('cwd', 'cwd'), ('hook_event_name', 'event')]:
            if isinstance(data.get(source), str):
                value[target] = _text(data[source])
        model = data.get('model')
        if isinstance(model, dict):
            model = model.get('display_name') or model.get('id')
        if isinstance(model, str) and model:
            value['model'] = _text(model)
        effort = data.get('reasoning_effort') or data.get('effortLevel')
        if isinstance(data.get('effort'), dict):
            effort = data['effort'].get('level') or effort
        if isinstance(effort, str) and effort:
            value['reasoning_effort'] = _text(effort)
        event = value.get('event')
        if event == 'SessionEnd':
            value['state'] = 'ended'
        elif event == 'Stop':
            value['state'] = 'idle'
        elif event in ('PermissionRequest',):
            value['state'] = 'approval'
        elif event in ('UserPromptSubmit', 'SessionStart', 'PreToolUse', 'PostToolUse'):
            value['state'] = 'working'
        write_json(path, value)
        channel = os.environ.get('CUE_CODEX_STATUS_CHANNEL')
        if channel:
            launcher_root = Path(state_root).resolve() / 'launcher'
            destination = Path(channel).expanduser().resolve()
            if destination.is_relative_to(launcher_root):
                write_json(destination, {'schema': SCHEMA, 'session_id': sid,
                                         'observed_at': value['observed_at']})
        return value


def read_status(state_root, session_id):
    value = read_json(status_path(state_root, session_id))
    if value and value.get('session_id') != session_id:
        raise ValueError('Status snapshot belongs to a different session.')
    return value or {'schema': SCHEMA, 'session_id': session_id, 'state': 'unobserved'}


def merge_context(value, context):
    """Join a provider snapshot without guessing its session or organization.

    Context is read for this rendering, not cached indefinitely. A provider can
    atomically replace its own JSON file whenever its board changes.
    """
    allowed = {'schema', 'session_id', 'organisation', 'board', 'board_error'}
    if not isinstance(context, dict) or set(context) - allowed:
        raise ValueError('Provider context contains unsupported fields.')
    if context.get('session_id') != value.get('session_id'):
        raise ValueError('Provider context must name this exact session_id.')
    if context.get('schema', SCHEMA) != SCHEMA:
        raise ValueError('Unsupported provider context schema.')
    result = dict(value)
    organisation = context.get('organisation')
    if not isinstance(organisation, dict) or set(organisation) - {'profile', 'source'}:
        raise ValueError('Provider organisation requires profile and optional source fields.')
    if not isinstance(organisation.get('profile'), str) or not organisation['profile'].strip():
        raise ValueError('Provider organisation.profile must be a nonempty string.')
    if 'source' in organisation and not isinstance(organisation['source'], str):
        raise ValueError('Provider organisation.source must be a string.')
    result['organisation'] = dict(organisation)
    result.pop('board', None)
    result.pop('board_error', None)
    board = context.get('board')
    if board is not None:
        keys = {'id', 'org', 'title', 'short', 'status', 'done', 'total', 'at', 'created', 'owner', 'url'}
        if not isinstance(board, dict) or set(board) - keys:
            raise ValueError('Provider board contains unsupported fields.')
        if board.get('org') != organisation['profile']:
            raise ValueError('Provider board.org must match organisation.profile.')
        if not isinstance(board.get('id'), str) or not board['id'].strip():
            raise ValueError('Provider board.id must be a nonempty string.')
        for field in keys - {'done', 'total', 'at'}:
            if field in board and not isinstance(board[field], str):
                raise ValueError('Provider board.' + field + ' must be a string.')
        done, total = board.get('done'), board.get('total')
        if (done is None) != (total is None):
            raise ValueError('Provider board counts must both be present or unknown.')
        if done is not None and (type(done) is not int or type(total) is not int or not 0 <= done <= total):
            raise ValueError('Provider board counts require integers with 0 <= done <= total.')
        stamp = board.get('at')
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            raise ValueError('Provider board.at requires an observed Unix timestamp.')
        result['board'] = dict(board)
    if context.get('board_error') is not None:
        if not isinstance(context['board_error'], str):
            raise ValueError('Provider board_error must be a string.')
        result['board_error'] = context['board_error']
    return result


def render(value, *, now=None):
    """Plain terminal text. Missing cost/progress never becomes a zero."""
    parts = []
    organisation = value.get('organisation') or {}
    if isinstance(organisation, dict) and organisation.get('profile'):
        label = 'organisation: ' + _text(organisation['profile'])
        if organisation.get('source'):
            label += ' (' + _text(organisation['source']) + ')'
        parts.append(label)
    if value.get('model'):
        label = _text(value['model'])
        if value.get('reasoning_effort'):
            label += ' (' + _text(value['reasoning_effort']) + ')'
        parts.append(label)
    if not parts:
        parts.append('Session status: ' + _text(value.get('state') or 'unobserved'))
    rows = ['  ·  '.join(parts)]
    board = value.get('board')
    if isinstance(board, dict) and board.get('id'):
        line = _text(board.get('short') or board.get('title') or 'Board')
        done, total = board.get('done'), board.get('total')
        if type(done) is int and type(total) is int and 0 <= done <= total:
            if total:
                line += '  ·  %d/%d (%d%%)' % (done, total, round(100 * done / total))
            else:
                line += '  ·  no rows'
        else:
            line += '  ·  progress unavailable'
        stamp = board.get('at')
        if isinstance(stamp, (int, float)) and math.isfinite(stamp):
            if (time.time() if now is None else now) - stamp > 300:
                line += '  ·  stale'
        rows.append(line)
    elif value.get('board_error'):
        rows.append('Board status unavailable: ' + _text(value['board_error']))
    else:
        rows.append('No board selected for this session')
    return '\n'.join(rows)


def source_payload(value, supplied=None):
    """Observed fields expressed in Claude statusLine's stdin shape."""
    payload = dict(supplied or {})
    sid = value.get('session_id')
    if payload.get('session_id') not in (None, sid):
        raise ValueError('Supplied status payload belongs to a different session.')
    payload['session_id'] = sid
    if value.get('cwd'):
        payload.setdefault('cwd', value['cwd'])
        workspace = payload.setdefault('workspace', {})
        if isinstance(workspace, dict):
            workspace.setdefault('current_dir', value['cwd'])
    if value.get('model'):
        payload.setdefault('model', {'id': value['model'], 'display_name': value['model']})
    if value.get('reasoning_effort'):
        payload.setdefault('reasoning_effort', value['reasoning_effort'])
        payload.setdefault('effort', {'level': value['reasoning_effort']})
    for key in ('organisation', 'board', 'board_error'):
        if key in value:
            payload.setdefault(key, value[key])
    return payload


def run_source(project, value, *, state_root=None, supplied=None, timeout=5):
    """Explicit invocation only; no automatic side effects during conversion."""
    project = Path(project).resolve()
    manifest = read_json(project / '.cue/status-line.json')
    if not manifest.get('bridge_supported') or not isinstance(manifest.get('command'), str):
        raise ValueError('No supported converted source statusLine command is available.')
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Source command timeout must be a positive finite number.')
    payload = source_payload(value, supplied)
    sid = value['session_id']
    session_key(sid)
    env = dict(os.environ, CUE_HOST='codex', CUE_PROJECT_DIR=str(project),
               CLAUDE_PROJECT_DIR=str(project), CUE_SESSION_ID=sid,
               CLAUDE_CODE_SESSION_ID=sid,
               CUE_STATE_ROOT=str(state_root or project / '.cue/state/codex'))
    cwd = (supplied or {}).get('cwd') or value.get('cwd') or str(project)
    if not isinstance(cwd, str):
        raise ValueError('Status payload cwd must be a path string.')
    process = subprocess.Popen(manifest['command'], shell=True, cwd=cwd, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(json.dumps(payload), timeout=timeout)
        return subprocess.CompletedProcess(manifest['command'], process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        # Own process group only: source shell descendants must not survive a
        # timed-out rendering operation and accumulate on every repaint.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(manifest['command'], 124, stdout,
                                           stderr + '\nConverted statusLine exceeded its render timeout.\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--session')
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--context', type=Path, help='Session-bound provider JSON supplying observed organisation and board data')
    parser.add_argument('--run-source', action='store_true', help='Explicitly execute the preserved source command; this can perform its original side effects')
    parser.add_argument('--payload', type=Path, help='Additional actual source-compatible payload fields; never invent telemetry')
    parser.add_argument('--timeout', type=float, default=5, help='Source command render deadline in seconds (default: 5)')
    parser.add_argument('--native-stdin', action='store_true', help='Receive actual session context from the patched native Codex status provider')
    args = parser.parse_args(argv)
    native_payload = None
    if args.native_stdin:
        try:
            raw = sys.stdin.read(65537)
            if len(raw) > 65536:
                raise ValueError('Native status payload exceeds 64 KiB.')
            native_payload = json.loads(raw)
            if not isinstance(native_payload, dict):
                raise ValueError('Native status payload must be an object.')
            sid = native_payload.get('session_id')
            session_key(sid)
            if args.session and args.session != sid:
                raise ValueError('Native status session conflicts with the supplied session.')
            args.session = sid
            if args.payload:
                raise ValueError('--payload and --native-stdin are mutually exclusive.')
        except (ValueError, TypeError) as exc:
            parser.error(str(exc))
    if not args.native_stdin and not args.session:
        args.session = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CUE_SESSION_ID')
    if not args.session:
        parser.error('--session is required outside an active session')
    if args.json and args.run_source:
        parser.error('--json and --run-source are separate output modes')
    if args.payload and not args.run_source:
        parser.error('--payload requires --run-source')
    try:
        state_root = args.state_root or args.project / '.cue/state/codex'
        value = read_status(state_root, args.session)
        if args.context:
            value = merge_context(value, json.loads(args.context.read_text(encoding='utf-8')))
        if args.run_source:
            supplied = native_payload if args.native_stdin else (json.loads(args.payload.read_text(encoding='utf-8')) if args.payload else None)
            if supplied is not None and not isinstance(supplied, dict):
                raise ValueError('Supplied status payload must be a JSON object.')
            result = run_source(args.project, value, state_root=state_root,
                                supplied=supplied, timeout=args.timeout)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            return result.returncode if result.returncode >= 0 else 128 - result.returncode
        print(json.dumps(value, ensure_ascii=False, sort_keys=True) if args.json else render(value))
        return 0
    except (OSError, ValueError) as exc:
        print('Status read failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
