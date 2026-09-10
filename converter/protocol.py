"""Shared, host-neutral event conversion helpers. No project-specific hook routes."""

import copy

import fcntl
import fnmatch

import hashlib

import json

import os

from pathlib import Path

import re

import shlex

import subprocess

import sys

import tempfile

import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]

CUE = ROOT / '.cue'

STATE = Path(os.environ.get('CUE_STATE_ROOT', str(CUE / 'state/codex')))

def atom(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]

def health(data, code, detail):
    """Bounded metadata only; never copy commands or credentials into health logs."""
    row = {'at': time.time(), 'session': str(data.get('session_id', 'unknown')),
           'event': str(data.get('hook_event_name', 'unknown')),
           'actor': str(data.get('agent_id', 'main')), 'code': code, 'detail': detail}
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / 'adapter-health.jsonl').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(row) + '\n')
    except OSError as exc:
        print('Cue adapter health log unavailable: ' + type(exc).__name__, file=sys.stderr)

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f)
    os.replace(name, path)

def tool_name(name):
    name = str(name or '').split('.')[-1]
    return {'exec_command': 'Bash', 'shell_command': 'Bash', 'shell': 'Bash',
            'spawn_agent': 'Agent', 'request_user_input': 'AskUserQuestion',
            'request_user_input_async': 'AskUserQuestion',
            'mcp__cue_questions__AskUserQuestion': 'AskUserQuestion',
            'update_plan': 'TodoWrite'}.get(name, name)


def real_answer(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value) and all(isinstance(item, str) and item.strip() for item in value)
    if isinstance(value, dict) and set(value) == {'answers'}:
        return real_answer(value['answers'])
    return False


def response_text(response):
    """Flatten native text blocks without discarding the original response."""
    if not isinstance(response, dict):
        return response if isinstance(response, str) else ''
    output = response.get('output')
    if isinstance(output, str):
        return output
    content = response.get('content')
    if not isinstance(content, list):
        return ''
    texts = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict):
            for key in ('text', 'input_text', 'output_text', 'output'):
                value = block.get(key)
                if isinstance(value, str):
                    texts.append(value)
                    break
    return '\n'.join(texts)


def normalize(data):
    d = copy.deepcopy(data)
    d['cue_original_tool_name'] = str(d.get('tool_name') or '').split('.')[-1]
    d['tool_name'] = tool_name(d.get('tool_name'))
    ti = d.get('tool_input')
    if isinstance(ti, str):
        ti = {'command': ti}
    if not isinstance(ti, dict):
        ti = {}
    if d['tool_name'] == 'Bash':
        ti['command'] = ti.get('command', ti.get('cmd', ''))
        if ti.get('workdir'):
            d['cwd'] = ti['workdir']
    elif d['tool_name'] == 'Agent':
        ti['prompt'] = ti.get('prompt', ti.get('message', ''))
        ti['subagent_type'] = ti.get('subagent_type', ti.get('agent_type', 'default'))
        ti['description'] = ti.get('description', ti.get('task_name', ti['subagent_type']))
    if str(data.get('tool_name', '')).split('.')[-1] == 'request_user_input_async':
        # Native async cards use title + string options. Preserve the raw fields
        # while providing the source question shape to transcript/guard readers.
        questions = ti.get('questions')
        if isinstance(questions, list):
            for question in questions:
                if not isinstance(question, dict):
                    continue
                if isinstance(question.get('title'), str):
                    question.setdefault('question', question['title'])
                if isinstance(question.get('options'), list):
                    question['options'] = [{'label': option} if isinstance(option, str) else option
                                           for option in question['options']]
    d['tool_input'] = ti
    response = d.get('tool_response')
    if d['tool_name'] == 'AskUserQuestion' and d.get('hook_event_name') == 'PostToolUse':
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                response = {}
        envelope = response if isinstance(response, dict) else {}
        if isinstance(envelope.get('structuredContent'), dict):
            response = envelope['structuredContent']
        elif isinstance(envelope.get('content'), list):
            response = {}
            for block in envelope['content']:
                if isinstance(block, dict) and block.get('type') == 'text':
                    try:
                        candidate = json.loads(block.get('text', ''))
                        if isinstance(candidate, dict) and 'action' in candidate:
                            response = candidate
                            break
                    except (ValueError, TypeError):
                        pass
        response = response if isinstance(response, dict) else {}
        # A submitted async question and a declined form are not answer evidence.
        answers = response.get('answers')
        answered = (not envelope.get('isError') and response.get('action', 'accept') == 'accept'
                    and isinstance(answers, dict) and bool(answers)
                    and all(real_answer(v) for v in answers.values()))
        if str(data.get('tool_name', '')).split('.')[-1] == 'mcp__cue_questions__AskUserQuestion':
            expected = [q.get('question') for q in ti.get('questions', []) if isinstance(q, dict)]
            answered = (answered and response.get('action') == 'accept' and bool(expected)
                        and all(isinstance(q, str) and q.strip() for q in expected)
                        and set(answers) == set(expected)
                        and all(isinstance(v, str) and bool(v.strip()) for v in answers.values()))
        elif str(data.get('tool_name', '')).split('.')[-1] == 'request_user_input':
            expected = [q.get('id') for q in ti.get('questions', []) if isinstance(q, dict)]
            answered = (answered and bool(expected)
                        and all(isinstance(q, str) and q for q in expected)
                        and set(answers) == set(expected))
        if str(data.get('tool_name', '')).split('.')[-1] == 'request_user_input_async':
            # {accepted:true} acknowledges submission only. Answers arrive as a
            # later user message, not as this tool's result. Never mint answer
            # evidence from an invented answers/action field on that receipt.
            answered = False
            d['cue_question_submitted'] = response.get('accepted') is True and not envelope.get('isError')
        d['tool_response'] = response
        d['cue_question_unanswered'] = not answered
        if not answered:
            d['error'] = 'User question ' + str(response.get('action') or 'has no verified answer')
            d['is_interrupt'] = response.get('action') == 'cancel'
    if d['tool_name'] == 'Bash':
        raw_text = response if isinstance(response, str) else None
        if isinstance(response, str):
            try:
                parsed = json.loads(response)
                response = parsed if isinstance(parsed, dict) else {'stdout': response}
            except ValueError:
                response = {'stdout': response}
        if isinstance(response, dict):
            response = dict(response)
            response.setdefault('stdout', raw_text if raw_text is not None else response_text(response))
            response.setdefault('stderr', '')
            response.setdefault('interrupted', False)
            d['tool_response'] = response
    return d

def patch_events(data):
    """Expand a patch into Write/Edit views without executing or rewriting it."""
    patch = data['tool_input'].get('command', data['tool_input'].get('input', ''))
    if not isinstance(patch, str) or not patch.startswith('*** Begin Patch\n'):
        raise ValueError('Unsupported patch envelope')
    lines = patch.splitlines()
    events, i = [], 1
    cwd = Path(data.get('cwd') or ROOT)
    while i < len(lines):
        line = lines[i]
        if line == '*** End Patch':
            break
        match = re.fullmatch(r'\*\*\* (Add|Update|Delete) File: (.+)', line)
        if not match:
            raise ValueError('Unsupported patch file header')
        action, path = match.groups()
        absolute = str((cwd / path).resolve())
        i += 1
        move = None
        move_spelling = None
        if i < len(lines) and lines[i].startswith('*** Move to: '):
            move_spelling = os.path.abspath(cwd / lines[i][13:])
            move = str((cwd / lines[i][13:]).resolve())
            i += 1
        content = []
        while i < len(lines) and not re.match(r'\*\*\* (?:Add|Update|Delete) File: |\*\*\* End Patch$', lines[i]):
            content.append(lines[i]); i += 1
        d = copy.deepcopy(data)
        d['tool_name'] = 'Write' if action in ('Add', 'Delete') else 'Edit'
        ti = {'file_path': absolute, 'cue_patch_action': action,
              'cue_permission_path': os.path.abspath(cwd / path)}
        if action == 'Add':
            if any(not ln.startswith('+') for ln in content):
                raise ValueError('Invalid addition line')
            ti['content'] = '\n'.join(ln[1:] for ln in content) + ('\n' if content else '')
        elif action == 'Delete':
            ti['content'] = ''
        else:
            ti['old_string'] = '\n'.join(ln[1:] for ln in content if ln.startswith((' ', '-')))
            ti['new_string'] = '\n'.join(ln[1:] for ln in content if ln.startswith((' ', '+')))
            ti['cue_patch_hunks'] = content
        d['tool_input'] = ti
        events.append(d)
        if move:
            moved = copy.deepcopy(d)
            moved['tool_name'] = 'Write'
            moved['tool_input'] = {'file_path': move, 'content': ti.get('new_string', ti.get('content', '')),
                                   'cue_permission_path': move_spelling,
                                   'cue_move_source': absolute}
            events.append(moved)
    if i >= len(lines) or lines[i] != '*** End Patch' or i != len(lines) - 1:
        raise ValueError('Incomplete patch envelope')
    return events

def transcript_view(path, data):
    """Convert observed rollout record families; do not call a guessed path empty."""
    if not path:
        return None
    source = Path(path)
    try:
        stream = source.open()
    except OSError:
        health(data, 'transcript-unreadable', {'path_hash': atom(path)})
        return None
    # Keep only normalized records, never a second complete raw transcript.
    # Per-line splitlines retains the previous handling of Unicode separators.
    try:
        with stream:
            lines = (part for raw in stream for part in raw.splitlines())
            records = []
            unknown = set()
            saw_response_user = False
            event_users = []
            line_count = 0
            for line in lines:
                line_count += 1
                try:
                    row = json.loads(line)
                except ValueError:
                    unknown.add('invalid-json'); continue
                if not isinstance(row, dict):
                    unknown.add('non-object'); continue
                kind = row.get('type')
                if kind in ('user', 'assistant') and isinstance(row.get('message'), dict):
                    records.append(row); continue
                payload = row.get('payload')
                if not isinstance(payload, dict):
                    unknown.add(str(kind)); continue
                timestamp = row.get('timestamp')
                base = {'timestamp': timestamp}
                if kind == 'response_item':
                    typ = payload.get('type')
                    if typ == 'message':
                        role = payload.get('role')
                        if role not in ('user', 'assistant'):
                            continue
                        saw_response_user |= role == 'user'
                        blocks = []
                        for b in payload.get('content', []):
                            if isinstance(b, dict) and b.get('type') in ('input_text', 'output_text', 'text'):
                                blocks.append({'type': 'text', 'text': b.get('text', '')})
                        records.append({**base, 'type': role, 'message': {'role': role, 'content': blocks}})
                    elif typ in ('function_call', 'custom_tool_call'):
                        args = payload.get('arguments', payload.get('input', {}))
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {'command': args}
                        d = normalize({'tool_name': payload.get('name'), 'tool_input': args})
                        records.append({**base, 'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                            {'type': 'tool_use', 'id': payload.get('call_id'), 'name': d['tool_name'], 'input': d['tool_input']}]}})
                    elif typ in ('function_call_output', 'custom_tool_call_output'):
                        records.append({**base, 'type': 'user', 'message': {'role': 'user', 'content': [
                            {'type': 'tool_result', 'tool_use_id': payload.get('call_id'), 'content': payload.get('output', '')}]}})
                    elif typ not in ('reasoning', 'web_search_call', 'compaction'):
                        unknown.add('response_item/' + str(typ))
                elif kind == 'event_msg':
                    if payload.get('type') == 'user_message':
                        event_users.append({**base, 'type': 'user', 'message': {'role': 'user', 'content': payload.get('message', '')}})
                elif kind not in ('session_meta', 'turn_context', 'compacted'):
                    unknown.add(str(kind))
    except OSError:
        health(data, 'transcript-unreadable', {'path_hash': atom(path)})
        return None
    if not saw_response_user and event_users:
        records.extend(event_users)
        records.sort(key=lambda r: str(r.get('timestamp') or ''))
    if unknown:
        health(data, 'transcript-unknown-records', {'types': sorted(unknown), 'count': line_count})
    if not records:
        return None
    folder = STATE / 'transcripts' / atom(data.get('session_id'))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (atom(path) + '.jsonl')
    fd, temp = tempfile.mkstemp(dir=folder, prefix='.view-')
    with os.fdopen(fd, 'w') as f:
        for record in records:
            f.write(json.dumps(record) + '\n')
    os.replace(temp, target)
    return str(target)

def fold(event, outputs):
    """Keep every denial/advisory, emit only fields Codex supports for this event."""
    contexts, messages, denies, rewrites = [], [], [], []
    stopped = False
    stop_reasons = []
    for output in outputs:
        h = output.get('hookSpecificOutput') or {}
        if not isinstance(h, dict):
            h = {}
        if h.get('additionalContext'):
            contexts.append(str(h['additionalContext']))
        if output.get('systemMessage'):
            messages.append(str(output['systemMessage']))
        if h.get('permissionDecision') == 'deny':
            denies.append(str(h.get('permissionDecisionReason') or 'Cue guard denied the operation.'))
        if output.get('decision') == 'block':
            denies.append(str(output.get('reason') or 'Cue guard requested another pass.'))
        if output.get('continue') is False:
            reason = str(output.get('stopReason') or 'Source hook stopped this operation.')
            if event in ('PreToolUse', 'PermissionRequest'):
                denies.append(reason)
            else:
                stopped = True
                stop_reasons.append(reason)
        if h.get('updatedInput') is not None:
            if h.get('permissionDecision') == 'allow':
                rewrites.append(h['updatedInput'])
            else:
                denies.append(str(h.get('permissionDecisionReason') or 'Source hook requested an unapproved input rewrite. Resolve its approval requirement before retrying.'))
        elif h.get('permissionDecision') == 'ask':
            denies.append(str(h.get('permissionDecisionReason') or 'Source hook requires approval that this event cannot express in Codex. Use its native permission adapter before retrying.'))
    result = {}
    h = {'hookEventName': event}
    if event in ('Stop', 'SubagentStop'):
        # Codex's Stop JSON contract does not document additionalContext. Preserve
        # advisory visibility without turning a source advisory into a continuation.
        messages.extend(contexts)
    elif contexts:
        h['additionalContext'] = '\n\n'.join(dict.fromkeys(contexts))
    if denies:
        reason = '\n\n'.join(dict.fromkeys(denies))
        if event == 'PreToolUse':
            h.update(permissionDecision='deny', permissionDecisionReason=reason)
        else:
            result.update(decision='block', reason=reason)
    elif rewrites and event == 'PreToolUse':
        unique = {json.dumps(r, sort_keys=True) for r in rewrites}
        if len(unique) == 1:
            h.update(permissionDecision='allow', updatedInput=rewrites[0])
        else:
            h.update(permissionDecision='deny', permissionDecisionReason='Source hooks proposed conflicting input rewrites; resolve the conflict before retrying.')
    if stopped:
        result.update({'continue': False, 'stopReason': '\n'.join(stop_reasons)})
    if len(h) > 1:
        result['hookSpecificOutput'] = h
    if messages:
        result['systemMessage'] = '\n\n'.join(dict.fromkeys(messages))
    return result

def permitted_exact(command, rules):
    """Only exact source grants and explicit :* executable-prefix grants.

    More complex source wildcards are retained, but require the native prompt;
    approximating them with fnmatch can authorize shell control operators.
    """
    for rule in rules:
        if not rule.startswith('Bash(') or not rule.endswith(')'):
            continue
        expected = rule[5:-1]
        if expected.endswith(':*'):
            prefix = expected[:-2]
            if command == prefix or command.startswith(prefix + ' '):
                # Compound grants are left to the host's segment-aware rules.
                if not re.search(r'[;|&\n`<>]|\$\(', command):
                    return True
        elif '*' not in expected and command == expected:
            return True
    return False


def permission_path_pattern(pattern, project_root, cwd=None, source_root=None, action='deny'):
    """Resolve Claude path anchors without reading files or interpreting shell text.

    ``/`` belongs to the settings source; ``//`` belongs to the filesystem.
    Relative restrictions retain gitignore's filename/single-directory depth.
    """
    project_root = Path(project_root)
    cwd = Path(cwd or project_root)
    source_root = Path(source_root or project_root)
    anchored = True
    if pattern.startswith('//'):
        base, suffix = Path('/'), pattern[2:]
    elif pattern.startswith('~/'):
        base, suffix = Path.home(), pattern[2:]
    elif pattern.startswith('/'):
        base, suffix = source_root, pattern[1:]
    else:
        base, suffix = cwd, pattern.removeprefix('./')
        anchored = pattern.startswith('./')
    if not anchored and ('/' not in suffix or (action != 'allow' and suffix.count('/') == 1 and suffix.endswith('/**'))):
        suffix = '**/' + suffix
    return str(base / suffix)


def _path_glob_matches(path, pattern):
    """Match the supported gitignore glob syntax, keeping * inside one segment."""
    output, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if char == '\\' and i + 1 < len(pattern):
            i += 1
            output.append(re.escape(pattern[i]))
        elif pattern[i:i + 3] == '**/':
            output.append('(?:.*/)?'); i += 2
        elif pattern[i:i + 2] == '**':
            output.append('.*'); i += 1
        elif char == '*':
            output.append('[^/]*')
        elif char == '?':
            output.append('[^/]')
        elif char == '[':
            end = pattern.find(']', i + 1)
            if end == -1:
                output.append(r'\[')
            else:
                group = pattern[i + 1:end]
                if group.startswith('!'):
                    group = '^' + group[1:]
                output.append('[' + group.replace('\\', '\\\\') + ']'); i = end
        else:
            output.append(re.escape(char))
        i += 1
    try:
        return bool(re.fullmatch(''.join(output), path)) or (pattern.endswith('/**') and path == pattern[:-3])
    except re.error:
        return path == pattern


def permission_rule_matches(data, rule, project_root=ROOT, source_root=None, action='deny'):
    """Match one source rule against an observed tool call, never grant authority.

    Hosted web tools and indirect subprocess file accesses may bypass host hooks;
    callers must keep native filesystem/network controls and report that boundary.
    """
    if not isinstance(rule, str):
        return False
    match = re.fullmatch(r'([^()]+)(?:\((.*)\))?', rule, re.S)
    if not match:
        return False
    named, spec = match.groups()
    name = tool_name(data.get('tool_name'))
    original = data.get('cue_original_tool_name', str(data.get('tool_name') or '').split('.')[-1])
    names = {name, original}
    if name == 'Agent':
        names.add('Task')
    if spec is not None:
        if name in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit'):
            names.add('Edit')
        if name in ('Read', 'Grep', 'Glob', 'Write', 'Edit', 'MultiEdit'):
            names.add('Read')
    if not any(fnmatch.fnmatchcase(n, named) or (named.startswith('mcp__') and n.startswith(named + '__')) for n in names):
        return False
    if spec in (None, '*'):
        return True
    ti = data.get('tool_input') or {}
    if not isinstance(ti, dict):
        return False
    # Scalar parameter rules apply to restrictions, excluding primary content fields.
    param = re.fullmatch(r'([A-Za-z_]\w*)\s*:\s*(.*)', spec, re.S)
    primary = {'command', 'file_path', 'path', 'notebook_path', 'url'}
    if param and param[1] not in primary and param[1] in ti:
        value = ti[param[1]]
        if isinstance(value, (str, int, float, bool)):
            rendered = json.dumps(value) if not isinstance(value, str) else value
            return fnmatch.fnmatchcase(rendered, param[2])
    if named == 'Bash':
        return bool(restricted_command(ti.get('command', ti.get('cmd', '')), [rule]))
    if named in ('Read', 'Edit'):
        path = ti.get('cue_permission_path', ti.get('file_path', ti.get('notebook_path', ti.get('path'))))
        if not isinstance(path, str):
            return False
        cwd = data.get('cwd') or project_root
        spelling = os.path.abspath(os.path.join(str(cwd), os.path.expanduser(path)))
        paths = {spelling, str(Path(spelling).resolve())}
        pattern = permission_path_pattern(spec, project_root, cwd, source_root, action)
        patterns = {pattern, str(Path(pattern).resolve())}
        return any(_path_glob_matches(p, candidate) for p in paths for candidate in patterns)
    if named in ('Agent', 'Task'):
        return fnmatch.fnmatchcase(str(ti.get('subagent_type', ti.get('agent_type', ''))), spec)
    if named == 'WebFetch' and spec.startswith('domain:'):
        hostname = urlsplit(str(ti.get('url', ''))).hostname or ''
        return fnmatch.fnmatchcase(hostname.lower().rstrip('.'), spec[7:].lower().rstrip('.'))
    return False


def permission_match(data, settings, action, project_root=ROOT, rules=None):
    """Find the first matching restriction, considering every patch destination."""
    rows = rules if rules is not None else [
        {'action': action, 'rule': rule} for rule in settings.get('permissions', {}).get(action, [])]
    views = patch_events(data) if data.get('tool_name') == 'apply_patch' else [data]
    for row in rows:
        if row.get('action') != action:
            continue
        # The native patch tool itself can also be explicitly denied.
        for view in [data, *views] if data.get('tool_name') == 'apply_patch' else views:
            if permission_rule_matches(view, row.get('rule'), project_root, row.get('source_root'), action):
                return row
    return None


def restricted_command(command, rules):
    """Match restrictive rules independently of grant matching.

    Source glob denies/asks must never disappear because an allow-only parser
    deliberately rejects complex syntax. Examine both the full spelling and
    shell segments; grant matching remains deliberately narrower.
    """
    if not isinstance(command, str):
        return None
    candidates = [command.strip()]
    # Preserve quote information while finding separators. shlex strips that
    # information, making a literal ';' look like an executed command boundary.
    segments, start, quote, escaped = [], 0, None, False
    for i, char in enumerate(command):
        if escaped:
            escaped = False
        elif char == '\\' and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char in ';|&\n':
            segments.append(command[start:i]); start = i + 1
    segments.append(command[start:])
    for segment in segments:
        try:
            words = shlex.split(segment, comments=True)
            if words:
                candidates.extend(_restricted_shell_words(words))
        except ValueError:
            continue
    for body in _nested_shell_bodies(command):
        restricted = restricted_command(body, rules)
        if restricted:
            return restricted
    for rule in rules:
        if rule in ('Bash', '*', 'Bash(*)'):
            return rule
        if not isinstance(rule, str) or not rule.startswith('Bash(') or not rule.endswith(')'):
            continue
        pattern = rule[5:-1]
        for candidate in candidates:
            if pattern.endswith(':*'):
                prefix = pattern[:-2]
                if candidate == prefix or candidate.startswith(prefix + ' '):
                    return rule
            elif fnmatch.fnmatchcase(candidate, pattern) or (pattern.endswith(' *') and pattern.count('*') == 1 and candidate == pattern[:-2]):
                return rule
    return None


def _restricted_shell_words(words):
    """Strip documented command wrappers for restrictions only, never for grants."""
    candidates = [' '.join(words)]
    while words:
        first = words[0]
        if re.fullmatch(r'[A-Za-z_]\w*=.*', first, re.S):
            words = words[1:]
        elif first in ('then', 'do', 'else', '!', '{'):
            words = words[1:]
        elif first in ('command', 'builtin', 'noglob', 'nohup', 'time', 'nice', 'timeout', 'stdbuf', 'xargs'):
            if first == 'command' and len(words) > 1 and words[1] in ('-v', '-V'):
                break
            if first == 'xargs' and len(words) > 1 and words[1].startswith('-'):
                break
            words = words[1:]
            while words and words[0].startswith('-'):
                flag, words = words[0], words[1:]
                if flag in ('-n', '--adjustment', '-s', '--signal', '-k', '--kill-after', '-f', '-o', '-e', '-i') and words:
                    words = words[1:]
                if flag == '--':
                    break
            if first == 'timeout' and words:
                words = words[1:]
        else:
            break
        if words:
            candidates.append(' '.join(words))
    return candidates


def _nested_shell_bodies(command):
    """Expose literal subshell/substitution bodies; do not evaluate any shell code."""
    bodies, i, quote = [], 0, None
    while i < len(command):
        char = command[i]
        if char == '\\' and quote != "'":
            i += 2; continue
        if char == "'" and quote in (None, "'"):
            quote = None if quote else "'"; i += 1; continue
        if char == '"' and quote in (None, '"'):
            quote = None if quote else '"'; i += 1; continue
        if quote != "'" and char == '`':
            end = i + 1
            while end < len(command):
                if command[end] == '\\':
                    end += 2; continue
                if command[end] == '`':
                    break
                end += 1
            if end < len(command):
                bodies.append(command[i + 1:end]); i = end + 1; continue
        substitution = quote != "'" and command[i:i + 2] in ('$(', '<(', '>(')
        if substitution or (char == '(' and quote is None):
            start = i + (2 if substitution else 1)
            depth, end, inner_quote = 1, start, None
            while end < len(command):
                c = command[end]
                if c == '\\' and inner_quote != "'":
                    end += 2; continue
                if c in ('"', "'") and inner_quote in (None, c):
                    inner_quote = None if inner_quote else c
                elif inner_quote is None and c == '(':
                    depth += 1
                elif inner_quote is None and c == ')':
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            if depth == 0:
                bodies.append(command[start:end]); i = end + 1; continue
        i += 1
    return bodies
