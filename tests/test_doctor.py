"""Doctor reports target readiness gaps without executing source code or OAuth."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

path = Path(__file__).resolve().parents[1] / 'converter/doctor.py'
spec = importlib.util.spec_from_file_location('converter_doctor', path)
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cue-doctor-')
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.home = self.project / 'synthetic-home'
        self.home.mkdir()
        self.environment = patch.dict(doctor.os.environ, {'CODEX_HOME': str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        (self.project / '.codex').mkdir()
        (self.project / '.codex/config.toml').write_text('[mcp_servers.example]\nurl="https://example.test?token=SECRET"\nhttp_headers={Authorization="SECRET"}\n')

    def test_offline_never_launches_process_or_discloses_values(self):
        with patch.object(doctor.subprocess, 'run', side_effect=AssertionError('Must stay offline')):
            report = doctor.diagnose(self.project)
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertEqual(report['mcp'][0]['authentication'], 'unverified')
        self.assertEqual(report['runtime_readiness'], 'not_verified')

    def test_short_lifecycle_limits_are_errors_without_running_hooks(self):
        routes = {event: [{'hooks': [{'type': 'command', 'command': 'echo SECRET', 'timeout': timeout}]}]
                  for event, timeout in [('SessionEnd', 120), ('Interrupt', 4), ('PreToolUse', 120)]}
        (self.project / '.codex/hooks.json').write_text(json.dumps({'hooks': routes}))
        report = doctor.diagnose(self.project)
        self.assertFalse(report['offline_checks_passed'])
        self.assertEqual({f['event'] for f in report['findings']}, {'SessionEnd', 'Interrupt'})
        self.assertNotIn('SECRET', json.dumps(report))
        for event in ('SessionEnd', 'Interrupt'):
            routes[event][0]['hooks'][0]['timeout'] = 3
        (self.project / '.codex/hooks.json').write_text(json.dumps({'hooks': routes}))
        self.assertTrue(doctor.diagnose(self.project)['offline_checks_passed'])

    def test_unknown_auth_is_not_logged_out_and_stdio_unsupported_is_normal(self):
        rows = doctor.sanitize_native([
            {'name': 'remote', 'enabled': True, 'auth_status': 'unknown', 'transport': {'type': 'streamable_http', 'url': 'SECRET'}},
            {'name': 'local', 'enabled': True, 'auth_status': 'unsupported', 'transport': {'type': 'stdio', 'env': {'SECRET': 'SECRET'}}}])
        self.assertEqual(rows[0]['authentication'], 'unverified')
        self.assertIn('if this service requires OAuth', rows[0]['login_note'])
        self.assertEqual(rows[1]['authentication'], 'oauth_unsupported')
        self.assertNotIn('suggested_login_argv', rows[1])
        self.assertNotIn('SECRET', json.dumps(rows))

    def test_explicit_native_logged_out_suggests_login_without_executing_it(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps([{'name': 'remote', 'enabled': True,
            'auth_status': 'not_logged_in', 'transport': {'type': 'streamable_http'}}]))
        with patch.object(doctor.subprocess, 'run', return_value=result) as runner:
            report = doctor.diagnose(self.project, native_mcp=True)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], ['codex', 'mcp', 'list', '--json'])
        self.assertEqual(report['native_mcp'][0]['authentication'], 'login_required')
        self.assertEqual(report['native_mcp'][0]['suggested_login_argv'], ['codex', 'mcp', 'login', '--', 'remote'])
        self.assertTrue(report['offline_checks_passed'])

    def test_credentials_are_not_connectivity_proof(self):
        for status in ('oauth', 'oAuth', 'bearer_token', 'bearerToken'):
            row = doctor.sanitize_native([{'name': 'remote', 'auth_status': status}])[0]
            self.assertEqual(row['authentication'], 'credential_present')
            self.assertEqual(row['connectivity'], 'not_tested')

    def test_native_failure_never_prints_stderr_secrets(self):
        result = SimpleNamespace(returncode=1, stderr='token=SECRET', stdout='SECRET')
        with patch.object(doctor.subprocess, 'run', return_value=result):
            report = doctor.diagnose(self.project, native_mcp=True)
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertFalse(report['native_inventory_checked'])

    def test_invalid_files_report_metadata_only(self):
        (self.project / '.codex/config.toml').write_text('SECRET-invalid=[[')
        (self.project / '.codex/hooks.json').write_text('{SECRET-invalid')
        report = doctor.diagnose(self.project)
        self.assertFalse(report['offline_checks_passed'])
        self.assertNotIn('SECRET', json.dumps(report))

    def test_preserved_archive_does_not_claim_authentication(self):
        (self.project / '.cue-source-archive').mkdir()
        report = doctor.diagnose(self.project)
        self.assertEqual(report['source_preservation'], 'archive_directory_present')
        self.assertEqual(report['mcp'][0]['authentication'], 'unverified')
        self.assertEqual(report['runtime_readiness'], 'not_verified')

    def test_nonfinite_and_invalid_timeouts_are_reported(self):
        for value in (float('nan'), float('inf'), True, 0, -1, '120'):
            with self.subTest(timeout=value):
                findings = doctor.inspect_hooks({'SessionEnd': [{'hooks': [{'timeout': value}]}]}, 'fixture')
                self.assertEqual(findings[0]['code'], 'invalid-hook-timeout')

    def test_report_cannot_overwrite_inspected_config_including_symlink_alias(self):
        config = self.project / '.codex/config.toml'
        prior = config.read_bytes()
        alias = self.project / 'alias.json'
        alias.symlink_to(config)
        for report in (config, alias):
            with self.subTest(report=report), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    doctor.main([str(self.project), '--report', str(report)])
                self.assertEqual(caught.exception.code, 2)
            self.assertEqual(config.read_bytes(), prior)

    def test_cli_rejects_unbounded_native_probe_timeout(self):
        for timeout in ('nan', 'inf', '0', '-1'):
            with self.subTest(timeout=timeout), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    doctor.main([str(self.project), '--native-mcp', '--timeout', timeout])
                self.assertEqual(caught.exception.code, 2)

    def test_duplicate_global_hooks_report_locations_without_commands(self):
        hook = {'type': 'command', 'command': 'echo SECRET', 'timeout': 5}
        document = {'hooks': {'Stop': [{'hooks': [hook]}]}}
        (self.project / '.codex/hooks.json').write_text(json.dumps(document))
        (self.home / 'hooks.json').write_text(json.dumps(document))
        with patch.object(doctor.subprocess, 'run', side_effect=AssertionError('Must stay offline')):
            report = doctor.diagnose(self.project)
        duplicates = [f for f in report['findings'] if f['code'] == 'duplicate-global-project-hook']
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]['event'], 'Stop')
        self.assertTrue(report['global_hooks_checked'])
        self.assertNotIn('SECRET', json.dumps(report))

    def test_distinct_matchers_and_events_are_not_exact_duplicates(self):
        command = {'type': 'command', 'command': 'echo fixture'}
        project = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [command]}]}}
        other = {'hooks': {'PreToolUse': [{'matcher': 'Read', 'hooks': [command]}],
                           'Stop': [{'matcher': 'Bash', 'hooks': [command]}]}}
        self.assertEqual(doctor.duplicate_hooks(project, other), [])

    def test_same_hook_file_is_not_compared_with_itself(self):
        document = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo fixture'}]}]}}
        (self.project / '.codex/hooks.json').write_text(json.dumps(document))
        report = doctor.diagnose(self.project, codex_home=self.project / '.codex')
        self.assertFalse(report['global_hooks_checked'])
        self.assertFalse(any(f['code'] == 'duplicate-global-project-hook' for f in report['findings']))

    def test_duplicate_detection_ignores_different_deadlines(self):
        a = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo fixture', 'timeout': 5}]}]}}
        b = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo fixture', 'timeout': 120}]}]}}
        self.assertEqual(len(doctor.duplicate_hooks(a, b)), 1)


    def permission_fixture(self):
        cue = self.project / '.cue'
        cue.mkdir(exist_ok=True)
        (cue / 'effective-settings.json').write_text(json.dumps({'permissions': {'defaultMode': 'acceptEdits'}, 'env': {'TOKEN': 'SECRET'}}))
        (self.project / '.codex/config.toml').write_text('approval_policy="on-request"\ndefault_permissions="converted"\n')
        path = self.project / 'selected-session.jsonl'
        row = {'type': 'user', 'cwd': str(self.project.resolve()), 'permissionMode': 'bypassPermissions',
               'timestamp': '2026-09-10T06:07:48.947Z', 'message': {'content': 'SECRET'}}
        path.write_text(json.dumps(row) + '\n')
        return path, row

    def test_permission_saved_and_observed_modes_are_distinct_private_metadata(self):
        session, _ = self.permission_fixture()
        with patch.object(doctor.subprocess, 'run', side_effect=AssertionError('offline')):
            report = doctor.diagnose(self.project, claude_session=session)
        permissions = report['permissions']
        self.assertEqual(permissions['source_saved']['default_mode'], 'acceptEdits')
        self.assertEqual(permissions['target_configured']['approval_policy'], 'on-request')
        self.assertEqual(permissions['target_configured']['permission_profile'], 'converted')
        self.assertEqual(permissions['source_session']['latest'], {
            'line': 1, 'timestamp': '2026-09-10T06:07:48.947000+00:00', 'permission_mode': 'bypassPermissions'})
        self.assertIn('source-saved-session-mode-mismatch', [x['code'] for x in report['findings']])
        self.assertIn('source-session-bypass-target-approvals', [x['code'] for x in report['findings']])
        self.assertEqual(permissions['managed_host_overrides'], 'not_verified')
        self.assertNotIn('SECRET', json.dumps(report))

    def test_session_is_never_discovered_implicitly(self):
        self.permission_fixture()
        with patch.object(doctor, 'permission_session', side_effect=AssertionError('explicit only')):
            report = doctor.diagnose(self.project)
        self.assertNotIn('source_session', report['permissions'])
        self.assertEqual(report['permissions']['runtime_parity'], 'not_verified')

    def test_other_cwd_missing_cwd_and_non_user_records_cannot_override(self):
        path, row = self.permission_fixture()
        records = [row, dict(row, cwd='/other', permissionMode='default'),
                   dict(row, cwd=None, permissionMode='plan'), dict(row, type='assistant', permissionMode='default')]
        path.write_text('\n'.join(json.dumps(x) for x in records))
        result = doctor.diagnose(self.project, claude_session=path)['permissions']['source_session']
        self.assertEqual(result['latest']['line'], 1)
        self.assertEqual(result['latest']['permission_mode'], 'bypassPermissions')

    def test_malformed_records_and_oversize_are_visible_without_raw_content(self):
        path, row = self.permission_fixture()
        path.write_bytes(b'{SECRET invalid}\n' + b'[' + b'X' * 600 + b']\n' + json.dumps(row).encode() + b'\n')
        with patch.object(doctor, 'SESSION_RECORD_BYTES', 512):
            report = doctor.diagnose(self.project, claude_session=path)
        session = report['permissions']['source_session']
        self.assertEqual(session['malformed_records'], 1)
        self.assertEqual(session['oversized_records'], 1)
        self.assertEqual(session['latest']['line'], 3)
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertIn('source-session-inspection-incomplete', [x['code'] for x in report['findings']])

    def test_session_scan_total_bound_marks_partial(self):
        path, _ = self.permission_fixture()
        with patch.object(doctor, 'SESSION_TOTAL_BYTES', 4):
            result = doctor.diagnose(self.project, claude_session=path)['permissions']['source_session']
        self.assertFalse(result['complete'])
        self.assertIsNone(result['latest'])

    def test_oversized_file_scans_newest_window_with_byte_provenance(self):
        path, row = self.permission_fixture()
        old = dict(row, permissionMode='default')
        prefix = json.dumps(old) + '\n' + ('X' * 4096) + '\n'
        path.write_text(prefix + json.dumps(row) + '\n')
        with patch.object(doctor, 'SESSION_TOTAL_BYTES', 1024):
            result = doctor.diagnose(self.project, claude_session=path)['permissions']['source_session']
        self.assertFalse(result['complete'])
        self.assertEqual(result['scan_strategy'], 'bounded_tail')
        self.assertEqual(result['latest']['permission_mode'], 'bypassPermissions')
        self.assertIsNone(result['latest']['line'])
        self.assertEqual(result['latest']['byte_offset'], len(prefix.encode()))
        self.assertEqual(result['scan_start_byte'], len(prefix.encode()))

    def test_invalid_permission_shapes_are_not_echoed(self):
        path, row = self.permission_fixture()
        (self.project / '.cue/effective-settings.json').write_text('{"permissions":{"defaultMode":{"SECRET":1}}}')
        row.update(permissionMode='SECRET', timestamp='SECRET')
        path.write_text(json.dumps(row))
        report = doctor.diagnose(self.project, claude_session=path)
        self.assertEqual(report['permissions']['source_saved']['status'], 'unreadable_or_invalid')
        self.assertIsNone(report['permissions']['source_session']['latest'])
        self.assertNotIn('SECRET', json.dumps(report))

    def test_archive_fallback_is_labeled_non_effective(self):
        archive = self.project / '.cue-source-archive/project'
        archive.mkdir(parents=True)
        (archive / 'settings.json').write_text('{"permissions":{"defaultMode":"default"}}')
        report = doctor.diagnose(self.project)
        self.assertEqual(report['permissions']['source_saved']['kind'], 'project_archive_only')
        self.assertEqual(report['permissions']['source_saved']['default_mode'], 'default')

    def test_report_cannot_overwrite_session_or_permission_snapshot(self):
        path, _ = self.permission_fixture()
        for destination in (path, self.project / '.cue/effective-settings.json'):
            prior = destination.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                doctor.main([str(self.project), '--claude-session', str(path), '--report', str(destination)])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(destination.read_bytes(), prior)

    def test_missing_session_and_nested_malformed_json_do_not_crash(self):
        path, row = self.permission_fixture()
        missing = doctor.diagnose(self.project, claude_session=path.with_name('missing.jsonl'))
        self.assertEqual(missing['permissions']['source_session']['status'], 'unreadable')
        path.write_text('[' * 2000 + '0' + ']' * 2000 + '\n' + json.dumps(row) + '\n')
        session = doctor.diagnose(self.project, claude_session=path)['permissions']['source_session']
        self.assertEqual(session['malformed_records'], 1)
        self.assertEqual(session['latest']['line'], 2)

    def test_matching_session_does_not_invent_mismatch_or_timestamp(self):
        path, row = self.permission_fixture()
        row.update(permissionMode='acceptEdits', timestamp='SECRET')
        path.write_text(json.dumps(row))
        report = doctor.diagnose(self.project, claude_session=path)
        self.assertIsNone(report['permissions']['source_session']['latest']['timestamp'])
        self.assertNotIn('source-saved-session-mode-mismatch', [x['code'] for x in report['findings']])
        self.assertNotIn('SECRET', json.dumps(report))

    def test_cli_session_inspection_is_wired(self):
        path, _ = self.permission_fixture()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(doctor.main([str(self.project), '--claude-session', str(path)]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['permissions']['source_session']['latest']['line'], 1)


if __name__ == '__main__':
    unittest.main()
