#!/usr/bin/env python3
"""Generic command-hook runtime for generated Claude-to-Codex projects."""
import copy
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.converter-runtime'))
from protocol import ROOT, CUE, STATE, atom, atomic_json, fold, health, normalize, patch_events, permitted_exact, permission_match, restricted_command, transcript_view
from hook_timeouts import source_timeout


def active_scopes(data):
    path = STATE / 'active-skills' / (atom(data.get('session_id')) + '.json')
    try:
        return set(json.loads(path.read_text()))
    except (OSError, ValueError):
        return set()


def activate(data):
    if data['hook_event_name'] != 'PostToolUse' or data['tool_name'] != 'Bash':
        return
    response = data.get('tool_response')
    if not isinstance(response, dict) or response.get('exit_code', 0) != 0:
        return
    try:
        parts = shlex.split(data['tool_input'].get('command', ''))
    except ValueError:
        return
    if len(parts) != 3 or not Path(parts[0]).name.startswith('python'):
        return
    expected = (Path(data.get('cwd') or ROOT) / parts[1]).resolve()
    if expected != CUE / 'scripts/activate_skill.py' or not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', parts[2]):
        return
    skills = active_scopes(data)
    skills.add(parts[2])
    atomic_json(STATE / 'active-skills' / (atom(data.get('session_id')) + '.json'), sorted(skills))


def matches(route, data):
    scope = route.get('scope') or {}
    if scope.get('agent') and data.get('agent_type') != scope['agent']:
        return False
    if scope.get('skill') and scope['skill'] not in active_scopes(data):
        return False
    event = data['hook_event_name']
    matcher = route.get('matcher')
    if not matcher or matcher in ('*', '.*'):
        return True
    subject = data.get('tool_name', '')
    if event in ('UserPromptSubmit', 'Stop', 'Interrupt'):
        return True
    if event == 'SessionStart':
        subject = data.get('source', '')
    elif event == 'SessionEnd':
        subject = data.get('reason', '')
    elif event in ('SubagentStart', 'SubagentStop'):
        subject = data.get('agent_type', '')
    elif event in ('PreCompact', 'PostCompact'):
        subject = data.get('trigger', '')
    return bool(re.search(matcher, subject))


def invoke(route, data):
    handler = route['handler']
    env = dict(os.environ, CUE_PROJECT_DIR=str(ROOT), CLAUDE_PROJECT_DIR=str(ROOT),
               CUE_STATE_ROOT=str(STATE), CUE_SESSION_ID=str(data.get('session_id') or ''),
               CLAUDE_CODE_SESSION_ID=str(data.get('session_id') or ''))
    try:
        result = subprocess.run(handler['command'], shell=True, input=json.dumps(data), text=True,
                                capture_output=True, cwd=data.get('cwd') or ROOT, env=env,
                                timeout=source_timeout(handler, data['hook_event_name']))
    except (OSError, subprocess.TimeoutExpired) as exc:
        health(data, 'source-hook-error', {'source': route['source'], 'error': type(exc).__name__})
        return {'systemMessage': 'Converted hook could not run: ' + route['source']}
    if result.returncode == 2:
        return {'decision': 'block', 'reason': result.stderr.strip() or 'Source hook blocked this operation.'}
    if result.returncode != 0:
        health(data, 'source-hook-error', {'source': route['source'], 'exit_code': result.returncode})
        return {'systemMessage': 'Converted hook failed: ' + route['source']}
    if not result.stdout.strip():
        return {}
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value
    except ValueError:
        if data['hook_event_name'] in ('SessionStart', 'UserPromptSubmit', 'SubagentStart'):
            return {'hookSpecificOutput': {'additionalContext': result.stdout}}
        health(data, 'invalid-source-output', {'source': route['source']})
        return {'systemMessage': 'Converted hook returned unsupported non-JSON output: ' + route['source']}


def dispatch(data, routes):
    event = data['hook_event_name']
    views = patch_events(data) if data['tool_name'] == 'apply_patch' else [data]
    out = []
    for view in views:
        for route in routes.get(event, []):
            if matches(route, view):
                out.append(invoke(route, view))
    return out


def permission(data, settings, routes, rules=None):
    if permission_match(data, settings, 'deny', ROOT, rules):
        return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {
            'behavior': 'deny', 'message': 'Denied by source permission settings.'}}}
    # Preserve source PermissionRequest vetoes before considering source grants.
    outputs = dispatch(data, routes)
    decisions = []
    for output in outputs:
        if output.get('decision') == 'block' or output.get('continue') is False:
            decisions.append({'behavior': 'deny', 'message': output.get('reason') or output.get('stopReason') or 'Source hook denied permission.'})
        hs = output.get('hookSpecificOutput') or {}
        decision = hs.get('decision') or {}
        if isinstance(decision, dict) and decision.get('behavior') in ('allow', 'deny'):
            if any(key in decision for key in ('updatedInput', 'updatedPermissions', 'interrupt')):
                decisions.append({'behavior': 'deny', 'message': 'Source permission hook requests a rewrite that this host cannot enforce.'})
            else:
                decisions.append({key: decision[key] for key in ('behavior', 'message') if key in decision})
        if hs.get('permissionDecision') in ('deny', 'ask'):
            decisions.append({'behavior': 'deny', 'message': hs.get('permissionDecisionReason') or 'Source hook requires approval.'})
    denies = [d for d in decisions if d['behavior'] == 'deny']
    if denies:
        return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': denies[0]}}
    if permission_match(data, settings, 'ask', ROOT, rules):
        # No hook grant or source allow can stand in for the actual host prompt.
        return {}
    # No arbitrary source hook approval can silently overrule an explicit source deny.
    command = data['tool_input'].get('command', '')
    perms = settings.get('permissions', {})
    if data['tool_name'] == 'Bash':
        if restricted_command(command, perms.get('deny', [])):
            return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'deny', 'message': 'Denied by the source permission settings.'}}}
        if restricted_command(command, perms.get('ask', [])):
            return {}
        if permitted_exact(command, perms.get('allow', [])):
            return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}
    if decisions:
        return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': decisions[0]}}
    return fold('PermissionRequest', outputs)


def pre_permission(data, settings, rules=None):
    """Deny wins; unsupported ask mechanisms stop instead of silently running.

    A tool argument requesting escalation is not proof of user approval: saved
    host grants can approve an escalation without a prompt. Native prompt-rule
    coverage must be separately verified before an ask can bypass this guard.
    """
    if permission_match(data, settings, 'deny', ROOT, rules):
        return fold('PreToolUse', [{'decision': 'block', 'reason': 'Denied by source permission settings.'}])
    asked = permission_match(data, settings, 'ask', ROOT, rules)
    if asked:
        return fold('PreToolUse', [{'decision': 'block', 'reason':
            'Source permission settings require user approval. This converted rule has no verified native approval route; '
            'the call remains blocked. Review the permission compatibility finding before retrying.'}])
    return {}


def process(data):
    if not isinstance(data, dict) or not data.get('hook_event_name'):
        return {'systemMessage': 'Invalid converted-hook event; no checks were certified.'}
    settings = json.loads((CUE / 'effective-settings.json').read_text())
    if settings.get('disableAllHooks') is not True and (CUE / 'status-line.json').exists() and (data.get('session_id') or data.get('thread_id')):
        try:
            from status_line import capture_event
            capture_event(data, STATE)
        except (OSError, ValueError, ImportError) as exc:
            health(data, 'status-capture-error', {'error': type(exc).__name__})
    data = normalize(data)
    routes = json.loads((CUE / 'hook-routes.json').read_text())
    rule_path = CUE / 'permission-rules.json'
    rules = json.loads(rule_path.read_text()) if rule_path.exists() else None
    for key in ('transcript_path', 'agent_transcript_path'):
        if data.get(key):
            data[key] = transcript_view(data[key], data)
    event = data['hook_event_name']
    activate(data)
    if event == 'PermissionRequest':
        return permission(data, settings, routes, rules)
    if event == 'PostToolUse' and data.get('cue_question_unanswered'):
        return fold(event, dispatch(dict(data, hook_event_name='PostToolUseFailure'), routes))
    if event == 'PreToolUse':
        restricted = pre_permission(data, settings, rules)
        if restricted:
            return restricted
    if event == 'PostToolUse' and data['tool_name'] == 'Bash':
        response = data.get('tool_response') or {}
        if isinstance(response, dict) and response.get('exit_code', 0) not in (None, 0):
            failure = dict(data, hook_event_name='PostToolUseFailure',
                           error=str(response.get('stderr') or response.get('stdout') or ''),
                           is_interrupt=bool(response.get('interrupted')))
            return fold(event, dispatch(failure, routes))
    outputs = dispatch(data, routes)
    if event == 'PreCompact':
        atomic_json(STATE / 'compaction' / (atom(data.get('session_id')) + '.json'), fold('SessionStart', outputs))
        return {}
    if event == 'SessionStart' and data.get('source') == 'compact':
        path = STATE / 'compaction' / (atom(data.get('session_id')) + '.json')
        if path.exists():
            outputs.append(json.loads(path.read_text()))
    result = fold(event, outputs)
    rewritten = result.get('hookSpecificOutput', {}).get('updatedInput')
    if event == 'PreToolUse' and rewritten is not None:
        if data['tool_name'] in ('Bash', 'apply_patch') and not isinstance(rewritten.get('command'), str):
            return fold(event, [{'decision': 'block', 'reason': 'Source hook rewrote file-tool fields that cannot be applied to this Codex command. Adapt the rewrite before retrying.'}])
        rewritten_event = dict(data, tool_input=rewritten)
        restricted = pre_permission(rewritten_event, settings, rules)
        if restricted:
            return restricted
    return result


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    try:
        output = process(data)
    except Exception as exc:
        health(data if isinstance(data, dict) else {}, 'runtime-error', {'error': type(exc).__name__})
        output = {'systemMessage': 'Converted hook runtime failed (' + type(exc).__name__ + '); inspect adapter-health.jsonl.'}
        event = data.get('hook_event_name') if isinstance(data, dict) else None
        if event == 'PreToolUse':
            output.update(fold(event, [{'decision': 'block', 'reason': 'Converted permission checks failed; this call is blocked until the adapter is repaired.'}]))
        elif event == 'PermissionRequest':
            output['hookSpecificOutput'] = {'hookEventName': event, 'decision': {
                'behavior': 'deny', 'message': 'Converted permission checks failed; approval is blocked until the adapter is repaired.'}}
    print(json.dumps(output))


if __name__ == '__main__':
    main()
