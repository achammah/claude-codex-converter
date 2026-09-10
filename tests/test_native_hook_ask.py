"""Native HookAsk capability is a host attestation, never user consent."""
import json
import shlex
import shutil
import subprocess
import sys
import unittest
import test_permissions_parity as fixtures


class NativeHookAskTests(unittest.TestCase):
    setUp = fixtures.PermissionRuntime.setUp
    def convert(self, permissions, hooks=None):
        cue = self.out / '.cue'
        (cue / 'scripts').mkdir(parents=True, exist_ok=True)
        (cue / '.converter-runtime').mkdir(exist_ok=True)
        shutil.copyfile(fixtures.CODE / 'runtime.py', cue / 'scripts/converted_hook.py')
        for name in ('protocol.py', 'hook_timeouts.py', 'bash_file_views.py'):
            shutil.copyfile(fixtures.CODE / name, cue / '.converter-runtime' / name)
        (cue / 'effective-settings.json').write_text(json.dumps({'permissions': permissions}))
        routes = {event: [{'handler': handler, 'source': 'isolated-test',
                          'matcher': group.get('matcher')} for group in groups
                         for handler in group['hooks']]
                  for event, groups in (hooks or {}).items()}
        (cue / 'hook-routes.json').write_text(json.dumps(routes))

    def event(self, marker=None, name='exec_command', inputs=None):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': name,
                   'tool_input': inputs or {'cmd': 'echo fixture'},
                   'session_id': 'hook-ask', 'cwd': str(self.out)}
        if marker is not None:
            payload['codex_hook_ask'] = marker
        result = subprocess.run([sys.executable, str(self.out / '.cue/scripts/converted_hook.py')],
                                input=json.dumps(payload), capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout).get('hookSpecificOutput', {})

    def hooks(self, *outputs):
        return {'PreToolUse': [{'hooks': [{'type': 'command',
                    'command': 'printf %s '+shlex.quote(json.dumps(output))} for output in outputs]}]}

    def test_host_attestation_is_exact_and_not_tool_input(self):
        self.convert({'ask': ['Bash(echo:*)']})
        self.assertEqual(self.event(1)['permissionDecision'], 'ask')
        for marker in [None, True, False, '1', 0, 2, 1.0]:
            with self.subTest(marker=marker):
                self.assertEqual(self.event(marker)['permissionDecision'], 'deny')
        self.assertEqual(self.event(inputs={'cmd': 'echo fixture', 'codex_hook_ask': 1,
                                          'sandbox_permissions': 'require_escalated'})['permissionDecision'], 'deny')

    def test_source_deny_and_later_hook_deny_win_over_ask(self):
        self.convert({'ask': ['Bash(echo:*)'], 'deny': ['Bash(echo fixture)']})
        self.assertEqual(self.event(1)['permissionDecision'], 'deny')
        self.convert({'ask': ['Bash(echo:*)']}, self.hooks({'decision': 'block', 'reason': 'later veto'}))
        result = self.event(1)
        self.assertEqual(result['permissionDecision'], 'deny')
        self.assertIn('later veto', result['permissionDecisionReason'])
        self.convert({}, self.hooks({'hookSpecificOutput': {'permissionDecision': 'ask'}},
                                    {'decision': 'block', 'reason': 'sibling veto'}))
        self.assertEqual(self.event(1)['permissionDecision'], 'deny')

    def test_source_hook_ask_only_on_supported_attested_route(self):
        self.convert({}, self.hooks({'hookSpecificOutput': {'permissionDecision': 'ask'}}))
        self.assertEqual(self.event(1)['permissionDecision'], 'ask')
        self.assertEqual(self.event()['permissionDecision'], 'deny')
        self.assertEqual(self.event(1, 'spawn_agent', {'agent_type': 'reader'})['permissionDecision'], 'deny')
        self.assertEqual(self.event(1, 'mcp__fixture__action', {'value': 'fixture'})['permissionDecision'], 'ask')

    def test_ask_and_rewrite_remain_blocked(self):
        rewrite = {'hookSpecificOutput': {'permissionDecision': 'allow',
                                         'updatedInput': {'command': 'echo changed'}}}
        self.convert({'ask': ['Bash(echo:*)']}, self.hooks(rewrite))
        self.assertEqual(self.event(1)['permissionDecision'], 'deny')
        self.convert({}, self.hooks({'hookSpecificOutput': {'permissionDecision': 'ask',
                                                          'updatedInput': {'command': 'echo changed'}}}))
        self.assertEqual(self.event(1)['permissionDecision'], 'deny')


if __name__ == '__main__':
    unittest.main()
