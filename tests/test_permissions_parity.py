"""Permission regressions use synthetic events/files; no protected file is read."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1] / 'converter'
spec = importlib.util.spec_from_file_location('permission_protocol_test', CODE / 'protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


class PermissionMatching(unittest.TestCase):
    def call(self, name, **inputs):
        return protocol.normalize({'tool_name': name, 'tool_input': inputs, 'cwd': '/fixture/project'})

    def test_tool_denies_and_aliases(self):
        cases = [
            ('Bash', self.call('exec_command', cmd='printf safe')),
            ('B*', self.call('functions.exec_command', cmd='printf safe')),
            ('mcp__*', self.call('mcp__calendar__create_event', title='fixture')),
            ('mcp__calendar', self.call('mcp__calendar__create_event', title='fixture')),
            ('mcp__calendar__create_*', self.call('mcp__calendar__create_event', title='fixture')),
            ('mcp__cue_questions__AskUserQuestion', self.call('mcp__cue_questions__AskUserQuestion', questions=[])),
            ('Agent(reviewer)', self.call('spawn_agent', agent_type='reviewer')),
            ('Task(reviewer)', self.call('spawn_agent', agent_type='reviewer')),
            ('Agent(model:opus)', self.call('spawn_agent', agent_type='reviewer', model='opus')),
            ('Bash(run_in_background:true)', self.call('exec_command', cmd='echo fixture', run_in_background=True)),
        ]
        for rule, data in cases:
            with self.subTest(rule=rule):
                self.assertTrue(protocol.permission_rule_matches(data, rule))
        self.assertFalse(protocol.permission_rule_matches(self.call('spawn_agent'), 'Agent(model:*)'))
        self.assertFalse(protocol.permission_rule_matches(self.call('mcp__calendar2__create_event'), 'mcp__calendar'))

    def test_path_anchor_types_and_settings_scope(self):
        resolve = protocol.permission_path_pattern
        self.assertEqual(resolve('/src/**', '/target', '/work', '/user-settings'), '/user-settings/src/**')
        self.assertEqual(resolve('//tmp/fixture/**', '/target', '/work'), '/tmp/fixture/**')
        self.assertEqual(resolve('src/**', '/target', '/work'), '/work/**/src/**')
        self.assertEqual(resolve('src/**', '/target', '/work', action='allow'), '/work/src/**')
        self.assertEqual(resolve('.env', '/target', '/work'), '/work/**/.env')

    def test_path_wildcard_boundaries(self):
        cases = [
            ('Edit(/src/*.ts)', '/fixture/project/src/one.ts', True),
            ('Edit(/src/*.ts)', '/fixture/project/src/nested/two.ts', False),
            ('Edit(/src/**/*.ts)', '/fixture/project/src/nested/two.ts', True),
            ('Read(.env)', '/fixture/project/nested/.env', True),
            ('Read(.env)', '/fixture/other/.env', False),
            ('Read(secrets/**)', '/fixture/project/vendor/secrets/file', True),
            ('Read(/secrets/**)', '/fixture/project/vendor/secrets/file', False),
            (r'Edit(/reports/\[exact\].md)', '/fixture/project/reports/[exact].md', True),
        ]
        for rule, path, expected in cases:
            with self.subTest(rule=rule, path=path):
                data = self.call('Write', file_path=path)
                self.assertEqual(protocol.permission_rule_matches(data, rule, '/fixture/project'), expected)

    def test_deny_checks_symlink_spelling_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'allowed').mkdir()
            (root / 'secret').mkdir()
            (root / 'allowed/link').symlink_to(root / 'secret/data')
            data = self.call('Write', file_path=str(root / 'allowed/link'))
            self.assertTrue(protocol.permission_rule_matches(data, 'Edit(/secret/**)', root))
            self.assertTrue(protocol.permission_rule_matches(data, 'Edit(/allowed/**)', root))

    def test_bash_restrictions_cover_wrappers_and_nested_commands(self):
        for command in ['git push', 'X=1 git push origin', 'timeout 5 git push origin',
                        'nice -n 5 git push origin', 'command git push origin',
                        'true; git push origin', '(git push origin)',
                        'echo "$(git push origin)"', 'echo `git push origin`',
                        'for i in one; do git push origin; done']:
            with self.subTest(command=command):
                self.assertEqual(protocol.restricted_command(command, ['Bash(git push *)']), 'Bash(git push *)')
        for command in ["echo '$(git push origin)'", "echo '(git push origin)'", 'command -v git push',
                        "echo ';' git push origin", "printf x > 'git push origin'"]:
            with self.subTest(command=command):
                self.assertIsNone(protocol.restricted_command(command, ['Bash(git push *)']))

    def test_grants_do_not_broaden_with_new_restrictive_parser(self):
        for command in ['X=1 git status', 'git status; echo other', 'git status $(echo other)', 'timeout 5 git status']:
            self.assertFalse(protocol.permitted_exact(command, ['Bash(git:*)']))
        self.assertTrue(protocol.permitted_exact('git status', ['Bash(git:*)']))


class PermissionRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='permission-parity-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source, self.out = self.base / 'source', self.base / 'converted'
        (self.source / '.claude').mkdir(parents=True)
        self.global_settings = self.base / 'global.json'
        self.global_settings.write_text('{}')
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', CUE_STATE_ROOT=str(self.base / 'state'))

    def convert(self, permissions, hooks=None):
        (self.source / '.claude/settings.json').write_text(json.dumps({'permissions': permissions, 'hooks': hooks or {}}))
        result = subprocess.run([sys.executable, str(CODE / 'claude_to_codex.py'), str(self.source),
                                 '--output', str(self.out), '--global-settings', str(self.global_settings)],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def event(self, name='exec_command', event='PreToolUse', **inputs):
        payload = {'hook_event_name': event, 'tool_name': name, 'tool_input': inputs,
                   'session_id': 'synthetic', 'cwd': str(self.out)}
        result = subprocess.run([sys.executable, str(self.out / '.cue/scripts/converted_hook.py')],
                                input=json.dumps(payload), capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def denied(self, result):
        hs = result.get('hookSpecificOutput', {})
        self.assertTrue(hs.get('permissionDecision') == 'deny' or hs.get('decision', {}).get('behavior') == 'deny', result)

    def test_bare_bash_deny_blocks_even_escalation(self):
        self.convert({'deny': ['Bash'], 'allow': ['Bash(echo:*)']})
        self.denied(self.event(cmd='echo fixture', sandbox_permissions='require_escalated'))
        self.denied(self.event(event='PermissionRequest', cmd='echo fixture'))

    def test_tool_deny_beats_source_hook_permission_grant(self):
        grant = json.dumps({'hookSpecificOutput': {'decision': {'behavior': 'allow'}}})
        self.convert({'deny': ['mcp__calendar__*']}, {'PermissionRequest': [
            {'hooks': [{'type': 'command', 'command': 'printf %s ' + shlex.quote(grant)}]}]})
        self.denied(self.event('mcp__calendar__create_event', title='fixture'))
        self.denied(self.event('mcp__calendar__create_event', event='PermissionRequest', title='fixture'))

    def test_ask_does_not_run_silently_or_accept_escalation_as_consent(self):
        self.convert({'ask': ['Bash(echo secret*)'], 'allow': ['Bash(echo:*)']})
        self.denied(self.event(cmd='echo secret-value'))
        self.denied(self.event(cmd='echo secret-value', sandbox_permissions='require_escalated'))
        self.assertEqual(self.event(event='PermissionRequest', cmd='echo secret-value'), {})

    def test_nonbash_ask_cannot_be_overruled_by_hook_allow(self):
        grant = json.dumps({'hookSpecificOutput': {'decision': {'behavior': 'allow'}}})
        self.convert({'ask': ['mcp__calendar__*']}, {'PermissionRequest': [
            {'hooks': [{'type': 'command', 'command': 'printf %s ' + shlex.quote(grant)}]}]})
        self.denied(self.event('mcp__calendar__create_event', title='fixture'))
        self.assertEqual(self.event('mcp__calendar__create_event', event='PermissionRequest', title='fixture'), {})

    def test_edit_deny_covers_patch_add_delete_and_move_destination(self):
        self.convert({'deny': ['Edit(/locked/**)']})
        patches = [
            '*** Begin Patch\n*** Add File: locked/new.txt\n+fixture\n*** End Patch\n',
            '*** Begin Patch\n*** Delete File: locked/old.txt\n*** End Patch\n',
            '*** Begin Patch\n*** Update File: allowed.txt\n*** Move to: locked/moved.txt\n@@\n-old\n+new\n*** End Patch\n',
        ]
        for patch in patches:
            self.denied(self.event('apply_patch', command=patch))

    def test_rewritten_mcp_input_rechecks_parameter_deny(self):
        rewrite = json.dumps({'hookSpecificOutput': {'permissionDecision': 'allow',
                             'updatedInput': {'agent_type': 'blocked'}}})
        self.convert({'deny': ['Agent(blocked)']}, {'PreToolUse': [
            {'hooks': [{'type': 'command', 'command': 'printf %s ' + shlex.quote(rewrite)}]}]})
        self.denied(self.event('spawn_agent', agent_type='allowed'))

    def test_corrupt_permission_state_fails_closed(self):
        self.convert({'deny': ['Bash']})
        (self.out / '.cue/effective-settings.json').write_text('{invalid')
        self.denied(self.event(cmd='echo fixture'))
        self.denied(self.event(event='PermissionRequest', cmd='echo fixture'))

    def test_reserved_permission_rewrite_is_not_forwarded_as_approval(self):
        rewrite = json.dumps({'hookSpecificOutput': {'decision': {'behavior': 'allow', 'updatedInput': {'command': 'echo changed'}}}})
        self.convert({}, {'PermissionRequest': [{'hooks': [{'type': 'command', 'command': 'printf %s ' + shlex.quote(rewrite)}]}]})
        self.denied(self.event(event='PermissionRequest', cmd='echo fixture'))


if __name__ == '__main__':
    unittest.main()
