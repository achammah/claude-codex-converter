#!/usr/bin/env python3
"""Inspect converted target setup without running hooks, MCP servers, or models.

Offline by default. --native-mcp reads `codex mcp list --json` metadata only;
authentication and connectivity remain distinct from preserved source files.
"""
import argparse
import datetime
import re
import json
import math
import os
from pathlib import Path
import subprocess
import tomllib

AUTH = {'unknown', 'unsupported', 'not_logged_in', 'notLoggedIn', 'bearer_token', 'bearerToken', 'oauth', 'oAuth'}


def inspect_hooks(hooks, location):
    findings = []
    if not isinstance(hooks, dict):
        return [{'code': 'invalid-hooks', 'severity': 'error', 'location': location}]
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            findings.append({'code': 'invalid-hook-groups', 'severity': 'error', 'location': location, 'event': event})
            continue
        for group_index, group in enumerate(groups):
            handlers = group.get('hooks') if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                findings.append({'code': 'invalid-hook-handlers', 'severity': 'error', 'location': location, 'event': event})
                continue
            for index, handler in enumerate(handlers):
                row = {'location': location, 'event': event, 'group': group_index, 'handler': index}
                if not isinstance(handler, dict):
                    findings.append(dict(row, code='invalid-hook-handler', severity='error'))
                    continue
                timeout = handler.get('timeout')
                if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
                    findings.append(dict(row, code='invalid-hook-timeout', severity='error'))
                elif event in ('SessionEnd', 'Interrupt') and timeout is not None and timeout > 3:
                    findings.append(dict(row, code='hook-timeout-clamped', severity='error',
                                         configured_seconds=timeout, host_max_seconds=3))
    return findings


def mcp_summary(name, config):
    if not isinstance(config, dict):
        return {'name': name, 'configuration': 'invalid', 'authentication': 'unverified'}
    transport = 'streamable_http' if isinstance(config.get('url'), str) else 'stdio' if isinstance(config.get('command'), str) else 'unknown'
    return {'name': name, 'configuration': 'present', 'enabled': config.get('enabled', True) is True,
            'transport': transport, 'authentication': 'unverified', 'connectivity': 'not_tested'}


def sanitize_native(rows):
    """An allowlist; never expose raw transport, headers, environment or errors."""
    if not isinstance(rows, list):
        raise ValueError('Unexpected MCP inventory shape')
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            raise ValueError('Invalid MCP inventory row')
        transport = row.get('transport', {})
        kind = transport.get('type') if isinstance(transport, dict) else None
        kind = kind if kind in ('stdio', 'streamable_http') else 'unknown'
        status = row.get('auth_status', 'unknown')
        status = status if isinstance(status, str) and status in AUTH else 'unknown'
        auth = ('login_required' if status in ('not_logged_in', 'notLoggedIn') else
                'credential_present' if status in ('oauth', 'oAuth', 'bearer_token', 'bearerToken') else
                'oauth_unsupported' if status == 'unsupported' else 'unverified')
        clean = {'name': row['name'], 'enabled': row.get('enabled') is True,
                 'transport': kind, 'authentication': auth, 'native_auth_status': status,
                 'connectivity': 'not_tested'}
        if kind == 'streamable_http' and auth in ('login_required', 'unverified') and clean['enabled']:
            clean['suggested_login_argv'] = ['codex', 'mcp', 'login', '--', row['name']]
            clean['login_note'] = ('Authenticate this server in Codex.' if auth == 'login_required' else
                                  'Authentication is unverified; if this service requires OAuth, authenticate it in Codex.')
        out.append(clean)
    return out


def duplicate_hooks(project_hooks, global_hooks):
    """Compare registrations without executing commands or exposing their values."""
    def rows(document):
        hooks = document.get('hooks', {}) if isinstance(document, dict) else {}
        if not isinstance(hooks, dict):
            return
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for gi, group in enumerate(groups):
                if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                    continue
                for hi, handler in enumerate(group['hooks']):
                    if not isinstance(handler, dict) or handler.get('type') != 'command':
                        continue
                    command = handler.get('command')
                    matcher = group.get('matcher')
                    if isinstance(command, str) and (matcher is None or isinstance(matcher, str)):
                        yield (event, matcher, command), (gi, hi)
    global_keys = {key for key, _ in rows(global_hooks)}
    return [{'code': 'duplicate-global-project-hook', 'severity': 'warning',
             'location': '.codex/hooks.json', 'event': key[0], 'group': pos[0], 'handler': pos[1],
             'note': 'The same command and matcher are also registered globally. If both are trusted and enabled, both run; choose the intended scope before removing a registration.'}
            for key, pos in rows(project_hooks) if key in global_keys]


SOURCE_MODES = {'default', 'plan', 'acceptEdits', 'auto', 'dontAsk', 'bypassPermissions'}
APPROVAL_POLICIES = {'on-request', 'on-failure', 'untrusted', 'never'}
SESSION_RECORD_BYTES = 1024 * 1024
SESSION_TOTAL_BYTES = 512 * 1024 * 1024


def permission_session(path, project):
    """Read only an explicitly selected transcript; never retain message content."""
    path = Path(path).expanduser().resolve()
    result = {'path': str(path), 'status': 'no_matching_mode', 'latest': None,
              'malformed_records': 0, 'oversized_records': 0, 'complete': True}
    total = line = 0
    try:
        if not path.is_file():
            raise OSError('Session is not a regular file')
        with path.open('rb') as handle:
            size = os.fstat(handle.fileno()).st_size
            tail = size > SESSION_TOTAL_BYTES
            if tail:
                result['complete'] = False
                result['scan_strategy'] = 'bounded_tail'
                handle.seek(size - SESSION_TOTAL_BYTES)
                # The first bytes can be inside JSON. Discard that partial record
                # in bounded chunks before parsing any complete records.
                while True:
                    fragment = handle.readline(SESSION_RECORD_BYTES + 1)
                    if not fragment or fragment.endswith(b'\n'):
                        break
                result['scan_start_byte'] = handle.tell()
            else:
                result['scan_strategy'] = 'full'
            while True:
                record_offset = handle.tell()
                raw = handle.readline(SESSION_RECORD_BYTES + 1)
                if not raw:
                    break
                line += 1
                total += len(raw)
                oversized = len(raw) > SESSION_RECORD_BYTES
                if oversized:
                    result['oversized_records'] += 1
                    while not raw.endswith(b'\n') and total <= SESSION_TOTAL_BYTES:
                        raw = handle.readline(SESSION_RECORD_BYTES + 1)
                        total += len(raw)
                        if not raw:
                            break
                if total > SESSION_TOTAL_BYTES:
                    result['complete'] = False
                    break
                if oversized:
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    result['malformed_records'] += 1
                    continue
                if not isinstance(row, dict):
                    result['malformed_records'] += 1
                    continue
                if row.get('type') != 'user' or row.get('cwd') != str(project):
                    continue
                mode = row.get('permissionMode')
                if not isinstance(mode, str) or mode not in SOURCE_MODES:
                    continue
                stamp = row.get('timestamp')
                try:
                    if not isinstance(stamp, str) or len(stamp) > 40:
                        raise ValueError('Invalid timestamp')
                    parsed = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    if parsed.tzinfo is None:
                        raise ValueError('Timestamp has no timezone')
                    stamp = parsed.isoformat()
                except ValueError:
                    stamp = None
                result['latest'] = {'line': None if tail else line, 'timestamp': stamp, 'permission_mode': mode}
                if tail:
                    result['latest']['byte_offset'] = record_offset
                result['status'] = 'observed'
    except OSError:
        result['status'] = 'unreadable'
        result['complete'] = False
    return result


def permission_summary(project, config, claude_session=None):
    """Saved project settings are evidence of configuration, never running policy."""
    result = {'source_saved': {'status': 'not_found'},
              'target_configured': {'location': '.codex/config.toml'},
              'runtime_parity': 'not_verified', 'managed_host_overrides': 'not_verified',
              'note': 'Saved settings do not capture launch flags or session mode changes. '
                      'Target runtime policy, inherited configuration and managed host overrides remain unverified.'}
    findings = []
    for relative, kind in [('.cue/effective-settings.json', 'effective_snapshot'),
                           ('.cue/settings.json', 'shared_snapshot'),
                           ('.cue-source-archive/project/settings.json', 'project_archive_only')]:
        path = project / relative
        if not path.is_file():
            continue
        source = {'location': relative, 'kind': kind, 'status': 'mode_not_recorded'}
        try:
            document = json.loads(path.read_text())
            perms = document.get('permissions', {}) if isinstance(document, dict) else None
            if not isinstance(perms, dict):
                raise ValueError('Invalid permissions')
            mode = perms.get('defaultMode')
            if mode is not None:
                if not isinstance(mode, str) or mode not in SOURCE_MODES:
                    raise ValueError('Invalid mode')
                source.update(status='recorded', default_mode=mode)
        except (OSError, ValueError):
            source['status'] = 'unreadable_or_invalid'
            findings.append({'code': 'source-permission-settings-invalid', 'severity': 'warning', 'location': relative})
        result['source_saved'] = source
        break
    target = result['target_configured']
    policy = config.get('approval_policy')
    target['approval_policy'] = policy if isinstance(policy, str) and policy in APPROVAL_POLICIES else 'not_recorded_or_unrecognized'
    profile = config.get('default_permissions')
    target['permission_profile'] = profile if isinstance(profile, str) and re.fullmatch(r'[A-Za-z0-9_:.-]{1,64}', profile) else 'not_recorded_or_unrecognized'
    sandbox = config.get('sandbox_mode')
    target['sandbox_mode'] = sandbox if isinstance(sandbox, str) and sandbox in {'read-only', 'workspace-write', 'danger-full-access'} else 'not_recorded_or_unrecognized'
    if result['source_saved'].get('status') == 'recorded':
        findings.append({'code': 'permission-mode-parity-unverified', 'severity': 'warning',
                         'note': 'Claude permission modes and Codex approval/profile settings are different controls; saved values alone do not prove equivalence.'})
    if claude_session is not None:
        session = permission_session(claude_session, project)
        result['source_session'] = session
        if session['status'] == 'unreadable' or not session['complete'] or session['malformed_records'] or session['oversized_records']:
            findings.append({'code': 'source-session-inspection-incomplete', 'severity': 'warning'})
        observed = session['latest']
        if observed and observed['permission_mode'] != result['source_saved'].get('default_mode') and result['source_saved'].get('status') == 'recorded':
            findings.append({'code': 'source-saved-session-mode-mismatch', 'severity': 'warning',
                             'note': 'The selected session records a different mode from saved source settings; no permission changes were applied.'})
        if observed and observed['permission_mode'] == 'bypassPermissions' and target['approval_policy'] in {'on-request', 'on-failure', 'untrusted'}:
            findings.append({'code': 'source-session-bypass-target-approvals', 'severity': 'warning',
                             'note': 'The selected Claude session bypasses permissions while target configuration can request approval. Runtime policy is still unverified.'})
    return result, findings


def diagnose(project, native_mcp=False, executable='codex', timeout=20, codex_home=None, claude_session=None):
    project = Path(project).expanduser().resolve()
    report = {'schema_version': 1, 'project': str(project), 'findings': [], 'mcp': [],
              'source_preservation': 'archive_directory_present' if (project / '.cue-source-archive').is_dir() else 'not_found',
              'native_inventory_checked': False, 'hooks_executed': False, 'mcp_servers_started': False,
              'model_turns': 0, 'hook_trust': 'not_verified',
              'note': 'Preserved source configuration does not authenticate Codex. Connectivity and hook execution require separate tests.'}
    if not project.is_dir():
        report['findings'].append({'code': 'project-not-found', 'severity': 'error'})
    config_path = project / '.codex/config.toml'
    config = {}
    if config_path.exists():
        try:
            config = tomllib.loads(config_path.read_text())
        except (OSError, ValueError):
            report['findings'].append({'code': 'config-unreadable-or-invalid', 'severity': 'error'})
    else:
        report['findings'].append({'code': 'target-config-not-found', 'severity': 'error'})
    report['permissions'], permission_findings = permission_summary(project, config, claude_session)
    report['findings'].extend(permission_findings)
    if 'hooks' in config:
        report['findings'].extend(inspect_hooks(config['hooks'], '.codex/config.toml'))
    hook_path = project / '.codex/hooks.json'
    document = {}
    if hook_path.exists():
        try:
            document = json.loads(hook_path.read_text())
            report['findings'].extend(inspect_hooks(document.get('hooks') if isinstance(document, dict) else None, '.codex/hooks.json'))
        except (OSError, ValueError):
            report['findings'].append({'code': 'hook-file-unreadable-or-invalid', 'severity': 'error'})
    home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()
    global_path = home / 'hooks.json'
    report['global_hooks_checked'] = False
    if global_path.resolve() != hook_path.resolve() and global_path.exists():
        try:
            global_document = json.loads(global_path.read_text())
            global_findings = inspect_hooks(global_document.get('hooks') if isinstance(global_document, dict) else None, 'CODEX_HOME/hooks.json')
            report['findings'].extend(global_findings)
            report['findings'].extend(duplicate_hooks(document, global_document))
            report['global_hooks_checked'] = True
        except (OSError, ValueError):
            report['findings'].append({'code': 'global-hook-file-unreadable-or-invalid', 'severity': 'warning'})
    servers = config.get('mcp_servers', {})
    if not isinstance(servers, dict):
        report['findings'].append({'code': 'invalid-mcp-config', 'severity': 'error'})
    else:
        report['mcp'] = [mcp_summary(name, value) for name, value in sorted(servers.items())]
    if native_mcp and project.is_dir():
        try:
            process = subprocess.run([executable, 'mcp', 'list', '--json'], cwd=project,
                                     capture_output=True, text=True, timeout=timeout, check=False)
            if process.returncode:
                report['findings'].append({'code': 'native-mcp-list-failed', 'severity': 'warning', 'exit_code': process.returncode})
            else:
                report['native_mcp'] = sanitize_native(json.loads(process.stdout))
                report['native_inventory_checked'] = True
                for row in report['native_mcp']:
                    if row['enabled'] and row['authentication'] == 'login_required':
                        report['findings'].append({'code': 'mcp-login-required', 'severity': 'warning', 'server': row['name']})
                    elif row['enabled'] and row['transport'] == 'streamable_http' and row['authentication'] == 'unverified':
                        report['findings'].append({'code': 'mcp-auth-unverified', 'severity': 'warning', 'server': row['name']})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            report['findings'].append({'code': 'native-mcp-inventory-unavailable', 'severity': 'warning'})
    report['offline_checks_passed'] = not any(row['severity'] == 'error' for row in report['findings'])
    report['runtime_readiness'] = 'not_verified'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--native-mcp', action='store_true', help='Read sanitized Codex MCP configuration/auth metadata; never log in or start servers')
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--codex-home', type=Path, help='Compare global hooks from this Codex home; defaults to CODEX_HOME or ~/.codex')
    parser.add_argument('--claude-session', type=Path, help='Inspect only this explicit Claude JSONL for user permission modes with an exact project cwd match; no message content is reported')
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be a positive finite number')
    if args.report:
        project = args.project.expanduser().resolve()
        destination = args.report.expanduser().resolve()
        protected = [project / name for name in ('.codex', '.agents', '.claude', '.cue-source-archive')]
        global_home = Path(args.codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()
        protected.extend([global_home, project / '.cue'])
        if args.claude_session and destination == args.claude_session.expanduser().resolve():
            parser.error('--report must not overwrite the inspected session')
        if destination in [project / name for name in ('AGENTS.md', 'CLAUDE.md', 'CLAUDE.local.md', '.mcp.json')] or any(destination.is_relative_to(path) for path in protected):
            parser.error('--report must not overwrite project configuration or preserved sources')
    report = diagnose(args.project, args.native_mcp, args.codex, args.timeout, args.codex_home, args.claude_session)
    rendered = json.dumps(report, indent=2) + '\n'
    if args.report:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered)
    print(rendered, end='')
    return 0 if report['offline_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
