"""Setup orchestration tests; native compilation is verified separately."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import setup
from converter import native_runtime


class SetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='native-setup-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / 'project'
        (self.project / '.claude').mkdir(parents=True)
        (self.project / '.claude/settings.json').write_text(json.dumps({
            'statusLine': {'type': 'command', 'command': 'echo fixture-status'}}))
        self.global_settings = self.root / 'global.json'
        self.global_settings.write_text('{}')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.work = self.root / 'work'
        self.codex_home = self.root / 'codex-home'
        self.codex_home.mkdir()
        (self.root/'patch').write_text('synthetic patch')
        (self.root/'question.patch').write_text('synthetic question patch')
        (self.root/'update.patch').write_text('synthetic updater patch')
        (self.root/'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'upstream_commit': native_runtime.UPSTREAM_COMMIT,
            'release_id': 'fixture-native-1', 'release_sequence': 1,
            'patch_file': 'patch', 'patch_sha256': native_runtime.sha256(b'synthetic patch'),
            'feature_marker': 'fixture_status',
            'additional_patches': [{'patch_file': 'question.patch',
                'patch_sha256': native_runtime.sha256(b'synthetic question patch'),
                'feature_marker': native_runtime.QUESTION_FEATURE_MARKER},
                {'patch_file': 'update.patch',
                 'patch_sha256': native_runtime.sha256(b'synthetic updater patch'),
                 'feature_marker': native_runtime.UPDATE_FEATURE_MARKER}]}))

    def call_setup(self, *, fail_project=False, plan_only=False, partial_project=False, fail_native=False):
        events = []
        def native_apply(*args):
            events.append('native')
            Path(args[1]).write_text('{}')
            if fail_native:
                raise RuntimeError('synthetic post-install failure')
        def project_apply(*args):
            events.append('project')
            if fail_project:
                raise ValueError('synthetic target drift')
            if partial_project:
                original_apply(*args)
                raise ValueError('synthetic project post-write failure')
            return original_apply(*args)
        original_apply = setup.install.apply
        with mock.patch.dict(os.environ, {'PATH': str(self.bin), 'CODEX_HOME': str(self.codex_home)}), \
                mock.patch.object(setup, 'native_assets', return_value=(self.root/'manifest.json', self.root/'patch')), \
                mock.patch.object(native_runtime, 'acquire_source', return_value=self.root/'source'), \
                mock.patch.object(native_runtime, 'provision_toolchain', return_value={}), \
                mock.patch.object(native_runtime, 'plan_install', return_value={}), \
                mock.patch.object(native_runtime, 'apply_install', side_effect=native_apply), \
                mock.patch.object(native_runtime, 'rollback_install', side_effect=lambda *a: events.append('rollback')), \
                mock.patch.object(setup.install, 'apply', side_effect=project_apply), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = setup.main([str(self.project), '--work-dir', str(self.work),
                                 '--global-settings', str(self.global_settings),
                                 *(['--plan-only'] if plan_only else [])])
        return result, events

    def test_native_install_precedes_project_config_activation(self):
        result, events = self.call_setup()
        self.assertEqual((result, events), (0, ['native', 'project']))
        config = tomllib.loads((self.project / '.codex/config.toml').read_text())
        self.assertIn('status_provider', config['tui'])
        self.assertFalse((self.project/'.codex/cue-codex').exists())
        self.assertNotIn(str(self.work/'project-stage'), json.dumps(config))
        self.assertTrue((self.work/'project-receipt.json').is_file())

    def test_converted_hooks_are_enabled_with_supported_flag(self):
        settings = self.project / '.claude/settings.json'
        source = json.loads(settings.read_text())
        source['hooks'] = {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo fixture-hook'}]}]}
        settings.write_text(json.dumps(source))
        result, _ = self.call_setup()
        self.assertEqual(result, 0)
        config = tomllib.loads((self.project / '.codex/config.toml').read_text())
        self.assertIs(config['features']['hooks'], True)
        self.assertNotIn('codex_hooks', config['features'])
        hooks = json.loads((self.project / '.codex/hooks.json').read_text())
        self.assertTrue(hooks['hooks']['Stop'])

    def test_post_install_diagnostics_report_duplicates_without_modifying_global_hooks(self):
        command = 'private fixture command'
        global_hooks = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': command}]}]}}
        global_path = self.codex_home / 'hooks.json'
        global_path.write_text(json.dumps(global_hooks))
        original_apply = setup.install.apply
        def apply_with_duplicate(plan, receipt):
            result = original_apply(plan, receipt)
            (self.project / '.codex/hooks.json').write_text(json.dumps(global_hooks))
            return result
        with mock.patch.object(setup.install, 'apply', side_effect=apply_with_duplicate), \
                mock.patch.object(setup.doctor.subprocess, 'run', side_effect=AssertionError('offline diagnostic launched a process')):
            result, events = self.call_setup()
        self.assertEqual((result, events), (0, ['native', 'project']))
        diagnostics_path = self.work / 'project-diagnostics.json'
        report_text = diagnostics_path.read_text()
        report = json.loads(report_text)
        self.assertIn('duplicate-global-project-hook', [row['code'] for row in report['findings']])
        self.assertNotIn(command, report_text)
        self.assertEqual(json.loads(global_path.read_text()), global_hooks)
        self.assertEqual(diagnostics_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(report['hooks_executed'])
        self.assertFalse(report['mcp_servers_started'])
        self.assertEqual(report['model_turns'], 0)

    def test_project_failure_rolls_back_native_install(self):
        self.assertEqual(self.call_setup(fail_project=True), (1, ['native', 'project', 'rollback']))

    def test_partial_project_install_restores_files_and_runtime(self):
        self.assertEqual(self.call_setup(partial_project=True), (1, ['native', 'project', 'rollback']))
        self.assertFalse((self.project/'.codex/config.toml').exists())
        receipt = json.loads((self.work/'project-receipt.json').read_text())
        self.assertEqual(receipt['state'], 'rolled-back')

    def test_native_post_write_failure_is_rolled_back(self):
        self.assertEqual(self.call_setup(fail_native=True), (1, ['native', 'rollback']))
        self.assertFalse((self.project/'.codex').exists())

    def test_plan_only_does_not_activate_files_or_binary(self):
        self.assertEqual(self.call_setup(plan_only=True), (0, []))
        self.assertFalse((self.project/'.codex').exists())
        self.assertEqual((self.work/'native/patch').read_text(), 'synthetic patch')
        self.assertEqual((self.work/'native/question.patch').read_text(), 'synthetic question patch')
        self.assertFalse((self.work/'project-diagnostics.json').exists())

    def test_off_path_destination_is_not_claimed_as_plain_codex(self):
        with mock.patch.dict(os.environ, {'PATH': str(self.bin)}):
            with self.assertRaisesRegex(ValueError, 'not on PATH'):
                setup.runtime_directory(self.root/'unreachable')

    def test_existing_codex_symlink_destination_is_preserved(self):
        executable = self.root/'original'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
        (self.bin/'codex').symlink_to(executable)
        with mock.patch.dict(os.environ, {'PATH': str(self.bin)}):
            self.assertEqual(setup.runtime_directory(), self.bin)

    def test_explicit_destination_after_stock_codex_is_rejected(self):
        stock = self.root/'stock'
        stock.mkdir()
        binary = stock/'codex'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': str(stock) + os.pathsep + str(self.bin)}):
            with self.assertRaisesRegex(ValueError, 'earlier PATH'):
                setup.runtime_directory(self.bin)

    def test_relative_and_empty_path_entries_keep_search_order(self):
        binary = self.root/'codex'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        for first in ('', os.path.relpath(self.root)):
            with self.subTest(entry=first), mock.patch.dict(os.environ, {'PATH': first + os.pathsep + str(self.bin)}):
                if first == '':
                    with mock.patch.object(setup.shutil, 'which', return_value='codex'):
                        with self.assertRaisesRegex(ValueError, 'earlier PATH'):
                            setup.runtime_directory(self.bin)
                else:
                    with self.assertRaisesRegex(ValueError, 'earlier PATH'):
                        setup.runtime_directory(self.bin)


if __name__ == '__main__':
    unittest.main()
