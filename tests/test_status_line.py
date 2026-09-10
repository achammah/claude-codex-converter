"""Status conversion and explicit rendering; all state/provider data is synthetic."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile
import termios
import tomllib
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
from converter import status_line as status
from converter import codex_tui


class StatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='converter-status-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source'
        self.output = self.root / 'output'
        (self.source / '.claude').mkdir(parents=True)
        self.sid = 'synthetic-codex-session'
        self.global_settings = self.root / 'global.json'
        self.global_settings.write_text('{}')

    def convert(self, body, *, extra=(), source_options=None):
        self.script = self.source / '.claude/status.py'
        self.script.write_text(body)
        self.settings = {'statusLine': {'type': 'command', 'command': 'python3 .claude/status.py'}}
        self.settings.update(source_options or {})
        (self.source / '.claude/settings.json').write_text(json.dumps(self.settings))
        command = [sys.executable, str(PACKAGE / 'converter/claude_to_codex.py'), str(self.source),
                   '--output', str(self.output), '--global-settings', str(self.global_settings), *extra]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.output / '.cue/status-line.json').read_text())

    def test_no_implicit_execution_and_explicit_bridge_gets_native_session_payload(self):
        marker = self.root / 'only-explicit-source-execution'
        body = ('import json, os, sys\nfrom pathlib import Path\n'
                'd=json.load(sys.stdin)\n'
                f'Path({str(marker)!r}).write_text("executed")\n'
                'print(json.dumps({"payload": d, "state_root": os.environ["CUE_STATE_ROOT"]}))\n')
        manifest = self.convert(body)
        self.assertFalse(marker.exists())
        self.assertTrue(manifest['native_footer_supported'])
        self.assertEqual(manifest['native_footer_mode'], 'builtin-items')
        self.assertEqual(manifest['native_footer_items'], status.NATIVE_STATUS_LINE_ITEMS)
        self.assertFalse(manifest['source_command_in_native_footer'])
        self.assertEqual(manifest['activation'], 'generated-cue-codex-launcher-or-explicit-reader')
        config = tomllib.loads((self.output / '.codex/config.toml').read_text())
        self.assertEqual(config['tui']['status_line'], status.NATIVE_STATUS_LINE_ITEMS)
        launcher = self.output / '.codex/cue-codex'
        reader = self.output / '.codex/cue-status'
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertTrue(os.access(reader, os.X_OK))
        self.assertIn('--run-source-status', launcher.read_text())
        self.assertTrue(manifest['bridge_supported'])
        state = self.output / '.cue/state/codex'
        raw = {'session_id': self.sid, 'hook_event_name': 'SessionStart', 'cwd': str(self.output),
               'model': 'synthetic-model', 'reasoning_effort': 'high',
               'tool_input': {'secret': 'must-not-be-copied'}, 'transcript_path': '/private/history'}
        hook = subprocess.run([sys.executable, str(self.output / '.cue/scripts/converted_hook.py')],
                              input=json.dumps(raw), text=True, capture_output=True, timeout=10)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        snapshot = status.read_status(state, self.sid)
        self.assertEqual(snapshot['model'], 'synthetic-model')
        self.assertNotIn('must-not-be-copied', json.dumps(snapshot))
        self.assertNotIn('transcript_path', snapshot)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(status.main([str(self.output), '--session', self.sid]), 0)
        self.assertFalse(marker.exists())
        executed = status.run_source(self.output, snapshot)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        result = json.loads(executed.stdout)
        self.assertEqual(result['payload']['session_id'], self.sid)
        self.assertEqual(result['payload']['model']['display_name'], 'synthetic-model')
        self.assertEqual(result['state_root'], str(state))
        self.assertNotIn('cost', result['payload'])
        self.assertTrue(marker.exists())

    def test_source_unknown_options_and_unsupported_shapes_are_preserved(self):
        value = {'type': 'future-type', 'command': 'echo test', 'padding': 3, 'future': {'x': 1}}
        self.output.mkdir()
        manifest = status.build_manifest(value, translate=lambda x:x, project=self.source, output=self.output)
        self.assertEqual(manifest['source'], value)
        self.assertEqual(manifest['unmapped_fields'], ['future', 'padding'])
        self.assertFalse(manifest['bridge_supported'])

    def test_native_provider_receives_real_stdin_without_cached_hook_state(self):
        self.convert('import json,sys\nprint(json.dumps(json.load(sys.stdin)))\n', extra=('--native-status',))
        cfg = tomllib.loads((self.output / '.codex/config.toml').read_text())
        provider = cfg['tui']['status_provider']
        payload = {'session_id': self.sid, 'cwd': str(self.output),
                   'model': {'id': 'actual-model', 'display_name': 'actual-model'},
                   'reasoning_effort': 'high'}
        result = subprocess.run(provider['command'], input=json.dumps(payload),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), payload)

    def test_native_provider_rejects_missing_session_before_source_runs(self):
        marker = self.root / 'unexpected-execution'
        self.convert(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n', extra=('--native-status',))
        cfg = tomllib.loads((self.output / '.codex/config.toml').read_text())
        result = subprocess.run(cfg['tui']['status_provider']['command'], input='{}',
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_native_provider_rejects_conflicting_session(self):
        with mock.patch('sys.stdin', io.StringIO(json.dumps({'session_id': self.sid}))), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            status.main([str(self.output), '--native-stdin', '--session', 'another-session'])

    def test_current_native_cwd_overrides_stale_hook_cwd(self):
        self.convert('import os\nprint(os.getcwd())\n', extra=('--native-status',))
        current = self.output/'nested'
        current.mkdir()
        # Use an absolute provider command so only the process cwd is varied.
        manifest_path = self.output/'.cue/status-line.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['command'] = 'python3 ' + str(self.output/'.cue/status.py')
        manifest_path.write_text(json.dumps(manifest))
        result = status.run_source(self.output, {'session_id': self.sid, 'cwd': str(self.output)},
                                   supplied={'session_id': self.sid, 'cwd': str(current)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(current))

    def test_disabling_source_hooks_also_disables_passive_status_capture(self):
        manifest = self.convert('print("status")\n', source_options={'disableAllHooks': True})
        self.assertFalse(manifest['capture_enabled'])
        hook = subprocess.run([sys.executable, str(self.output / '.cue/scripts/converted_hook.py')],
                              input=json.dumps({'session_id': self.sid, 'hook_event_name': 'SessionStart'}),
                              text=True, capture_output=True, timeout=10)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(status.read_status(self.output / '.cue/state/codex', self.sid)['state'], 'unobserved')

    def context(self):
        return {'schema': 1, 'session_id': self.sid,
                'organisation': {'profile': 'example-org', 'source': 'provider fixture'},
                'board': {'id': 'example-board', 'org': 'example-org', 'title': 'Convert all resources',
                          'short': 'Convert the setup', 'done': 7, 'total': 10, 'at': 1000}}

    def test_provider_context_is_session_and_organization_bound(self):
        value = {'session_id': self.sid, 'model': 'example-model'}
        merged = status.merge_context(value, self.context())
        text = status.render(merged, now=1001)
        self.assertIn('organisation: example-org', text)
        self.assertIn('Convert the setup  ·  7/10 (70%)', text)
        self.assertNotIn('stale', text)
        self.assertIn('stale', status.render(merged, now=1301))
        self.assertEqual(status.source_payload(merged)['board']['done'], 7)
        for change in ('session', 'org', 'counts', 'boolean', 'secret', 'stamp'):
            context = self.context()
            if change == 'session': context['session_id'] = 'other-session'
            elif change == 'org': context['board']['org'] = 'other-org'
            elif change == 'counts': context['board']['done'] = 11
            elif change == 'boolean': context['board']['done'] = True
            elif change == 'secret': context['organisation']['apiKey'] = 'not-allowed'
            elif change == 'stamp': context['board']['at'] = float('nan')
            with self.subTest(change=change), self.assertRaises(ValueError):
                status.merge_context(value, context)

    def test_context_does_not_retain_an_old_organization_board(self):
        initial = status.merge_context({'session_id': self.sid}, self.context())
        moved = status.merge_context(initial, {'session_id': self.sid,
                                             'organisation': {'profile': 'different-org'}, 'board': None})
        self.assertNotIn('board', moved)
        self.assertIn('No board selected', status.render(moved))

    def test_provider_context_file_renders_through_public_cli(self):
        self.convert('print("status")\n')
        context = self.root / 'context.json'
        context.write_text(json.dumps(self.context()))
        result = subprocess.run([sys.executable, '-c', 'from converter.cli import main; raise SystemExit(main())', 'status', str(self.output),
                                 '--session', self.sid, '--context', str(context), '--json'],
                                cwd=PACKAGE, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['board']['done'], 7)

    def test_literal_state_binding_reads_codex_session_and_keeps_archive_immutable(self):
        legacy = self.root / 'legacy-state'
        body = ('import json, sys\nfrom pathlib import Path\n'
                'd=json.load(sys.stdin)\n'
                f'print((Path({str(legacy)!r}) / d["session_id"] / "track-touched").read_text())\n')
        original_hash = hashlib.sha256(body.encode()).hexdigest()
        manifest = self.convert(body, extra=['--legacy-state-root', str(legacy) + '=.cue/state/codex/sessions'])
        rebound = [r for r in manifest['state_rebindings'] if r['status'] == 'rebound']
        self.assertEqual(len(rebound), 1)
        self.assertEqual(rebound[0]['before_sha256'], original_hash)
        archive = self.output / '.cue-source-archive/project/status.py'
        self.assertEqual(archive.read_bytes(), body.encode())
        self.assertEqual(self.script.read_bytes(), body.encode())
        current = self.output / '.cue/state/codex/sessions' / self.sid
        current.mkdir(parents=True)
        (current / 'track-touched').write_text('CURRENT-CODEX-BOARD')
        old = legacy / self.sid
        old.mkdir(parents=True)
        (old / 'track-touched').write_text('OLD-CLAUDE-BOARD')
        value = {'session_id': self.sid, 'cwd': str(self.output)}
        result = status.run_source(self.output, value)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'CURRENT-CODEX-BOARD')
        # Mutation evidence: breaking only the rebinding changes the observed
        # board back to the old store. Restore the generated copy afterwards.
        generated = self.output / '.cue/status.py'
        correct = generated.read_text()
        generated.write_text(body)
        broken = status.run_source(self.output, value)
        self.assertNotEqual(broken.stdout.strip(), 'CURRENT-CODEX-BOARD')
        self.assertEqual(broken.stdout.strip(), 'OLD-CLAUDE-BOARD')
        generated.write_text(correct)
        self.assertEqual(archive.read_bytes(), body.encode())

    def test_all_literal_anchors_move_together_and_missing_root_stays_manual(self):
        legacy = str(self.root / 'legacy')
        body = f'a={legacy!r}\nb={legacy!r}\n'
        manifest = self.convert(body, extra=['--legacy-state-root', legacy + '=.cue/state/codex/sessions',
                                           '--legacy-state-root', '/missing=.cue/state/codex/other'])
        converted = (self.output / '.cue/status.py').read_text()
        self.assertNotIn(legacy, converted)
        self.assertEqual(converted.count(str(self.output / '.cue/state/codex/sessions')), 2)
        self.assertEqual({row['status'] for row in manifest['state_rebindings']}, {'rebound', 'manual'})
        self.assertTrue(any(row.get('matches') == 2 and row['status'] == 'rebound'
                            for row in manifest['state_rebindings']))
        self.assertTrue(any('No staged literal' in row.get('reason', '')
                            for row in manifest['state_rebindings']))

    def test_invalid_python_after_rebinding_does_not_overwrite_copy(self):
        output = self.root / "target'quote"
        (output / '.cue').mkdir(parents=True)
        path = output / '.cue/status.py'
        original = "path = '/legacy/session'\n"
        path.write_text(original)
        rows = status._bind_staged_state(output, {'/legacy/session': '.cue/state/codex/sessions'})
        self.assertEqual(path.read_text(), original)
        self.assertEqual(rows[0]['status'], 'manual')

    def test_state_root_boundaries_and_duplicate_mappings(self):
        for bindings in [['relative=.cue/state/codex/sessions'], ['/old=/tmp/escape'],
                         ['/old=.cue/state/codex/../escape'], ['/old=.cue/source'],
                         ['/old=.cue/state/codex/a', '/old=.cue/state/codex/b'],
                         ['/old=.cue/state/codex/a', '/old/sub=.cue/state/codex/b']]:
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                status.parse_state_roots(bindings)

    def test_installation_relocates_rebound_reader_and_preserves_source_archive(self):
        from converter import install
        legacy = self.root / 'legacy'
        body = ('import json, sys\nfrom pathlib import Path\n'
                'd=json.load(sys.stdin)\n'
                f'print((Path({str(legacy)!r}) / d["session_id"] / "track-touched").read_text())\n')
        self.convert(body, extra=['--legacy-state-root', str(legacy) + '=.cue/state/codex/sessions'])
        target = self.root / 'installed'
        target.mkdir()
        plan, receipt = self.root / 'plan.json', self.root / 'receipt.json'
        with contextlib.redirect_stdout(io.StringIO()):
            install.plan(self.output, target, plan)
            install.apply(plan, receipt)
        current = target / '.cue/state/codex/sessions' / self.sid
        current.mkdir(parents=True)
        (current / 'track-touched').write_text('INSTALLED-CODEX-BOARD')
        result = status.run_source(target, {'session_id': self.sid, 'cwd': str(target)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'INSTALLED-CODEX-BOARD')
        self.assertNotIn(str(self.output), (target / '.cue/status.py').read_text())
        self.assertEqual((target / '.cue-source-archive/project/status.py').read_bytes(), body.encode())
        rows = json.loads((target / '.cue/file-manifest.json').read_text())
        row = next(row for row in rows if row.get('target') == '.cue/status.py')
        self.assertEqual(row['target_sha256'], hashlib.sha256((target / '.cue/status.py').read_bytes()).hexdigest())

    def test_recognized_cache_helper_maps_literal_reader(self):
        self.output.mkdir()
        helper = self.output / '.cue/hooks/lib/session_cache.py'
        helper.parent.mkdir(parents=True)
        helper.write_text('import os\ndef _root():\n    if os.environ.get("CUE_STATE_ROOT"):\n'
                          '        return os.path.join(os.environ["CUE_STATE_ROOT"], "sessions")\n'
                          '    return os.path.join(os.path.expanduser("~"), ".example", "sessions")\n')
        reader = self.output / '.cue/status.py'
        reader.write_text('path = "~/.example/sessions/"\n')
        manifest = status.build_manifest({'type': 'command', 'command': 'python3 .cue/status.py'},
                                          translate=lambda x:x, project=self.source, output=self.output)
        self.assertEqual(manifest['legacy_state_roots'], {'~/.example/sessions': '.cue/state/codex/sessions'})
        self.assertEqual(manifest['state_rebindings'][0]['status'], 'rebound')

    def test_source_stderr_exit_and_timeout_are_visible(self):
        self.convert('import sys\nprint("real diagnostic",file=sys.stderr)\nsys.exit(7)\n')
        value = {'session_id': self.sid, 'cwd': str(self.output)}
        result = status.run_source(self.output, value)
        self.assertEqual(result.returncode, 7)
        self.assertIn('real diagnostic', result.stderr)
        (self.output / '.cue/status.py').write_text('import time\ntime.sleep(30)\n')
        timed = status.run_source(self.output, value, timeout=0.05)
        self.assertEqual(timed.returncode, 124)
        self.assertIn('exceeded its render timeout', timed.stderr)

    def test_snapshot_ids_cannot_escape_and_terminal_control_text_is_removed(self):
        state = self.root / 'state'
        status.capture_event({'session_id': '../../escape', 'cwd': '\x1b[31mdanger\nnext'}, state, now=1)
        self.assertTrue(status.status_path(state, '../../escape').is_relative_to(state))
        self.assertEqual(status.read_status(state, '../../escape')['cwd'], 'dangernext')
        self.assertFalse((self.root / 'escape').exists())

    def test_hook_announces_exact_session_only_to_its_launcher_channel(self):
        state = self.root / 'state'
        channel = state / 'launcher/session.json'
        outside = self.root / 'outside.json'
        with mock.patch.dict(os.environ, {'CUE_CODEX_STATUS_CHANNEL': str(channel)}):
            status.capture_event({'session_id': self.sid, 'hook_event_name': 'SessionStart'}, state, now=7)
        self.assertEqual(json.loads(channel.read_text())['session_id'], self.sid)
        with mock.patch.dict(os.environ, {'CUE_CODEX_STATUS_CHANNEL': str(outside)}):
            status.capture_event({'session_id': 'other', 'hook_event_name': 'SessionStart'}, state, now=8)
        self.assertFalse(outside.exists())

    def test_tui_launcher_reserves_rows_and_renders_status_in_a_real_pty(self):
        fake_codex = self.root / 'fake-codex'
        fake_codex.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, pathlib, time\n'
            'p=pathlib.Path(os.environ["CUE_CODEX_STATUS_CHANNEL"])\n'
            'p.parent.mkdir(parents=True, exist_ok=True)\n'
            'p.write_text(json.dumps({"session_id":"pty-session"}))\n'
            'print("FAKE CODEX", flush=True)\n'
            'time.sleep(1)\n')
        fake_codex.chmod(0o755)
        provider = self.root / 'provider.py'
        provider.write_text('print("organisation: example-org")\nprint("Convert setup  ·  2/3")\n')
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        fcntl = __import__('fcntl')
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
        proc = subprocess.Popen([
            sys.executable, str(PACKAGE / 'converter/codex_tui.py'),
            '--project', str(self.root), '--codex', str(fake_codex),
            '--status-program', str(provider), '--refresh-seconds', '0.05',
            '--status-timeout', '1'], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        while proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                output.extend(os.read(master, 65536))
        while True:
            ready, _, _ = select.select([master], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        self.assertEqual(proc.returncode, 0, output.decode(errors='replace'))
        rendered = output.decode(errors='replace')
        self.assertIn('FAKE CODEX', rendered)
        self.assertIn('organisation: example-org', rendered)
        self.assertIn('Convert setup', rendered)

    def test_tui_status_text_is_control_safe_and_width_bounded(self):
        self.assertEqual(codex_tui.clean_lines('a\n\x1b[31mb\x1b[0m\nc'), ['a', 'b'])
        self.assertEqual(codex_tui.fit_cells('abcdef', 4), 'abc…')
        self.assertEqual(codex_tui.fit_cells('界界', 3), '界…')

    def test_tui_session_discovery_uses_resume_id_or_exact_project_metadata(self):
        sid = '11111111-1111-4111-8111-111111111111'
        self.assertEqual(codex_tui.session_from_args(['resume', sid]), sid)
        home = self.root / 'codex-home'
        baseline = codex_tui.session_snapshot(home)
        transcript = home / 'sessions/2026/01/01' / ('run-' + sid + '.jsonl')
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': sid, 'cwd': str(self.root)}}) + '\n')
        self.assertEqual(codex_tui.discover_session(home, self.root, baseline), sid)
        other = home / 'sessions/2026/01/01/run-other.jsonl'
        other.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': '22222222-2222-4222-8222-222222222222', 'cwd': str(self.root)}}) + '\n')
        self.assertIsNone(codex_tui.discover_session(home, self.root, baseline))


if __name__ == '__main__':
    unittest.main()
