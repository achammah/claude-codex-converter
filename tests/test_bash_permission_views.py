"""Literal shell paths restrict calls without granting or replaying hook events."""
import importlib.util
import json
from pathlib import Path
import shlex
import sys
import unittest
import test_native_hook_ask as fixtures

CODE = Path(__file__).resolve().parents[1] / 'converter'
SOURCES = [CODE / 'protocol.py']
PROJECT = Path(__file__).resolve().parents[2] / '.cue/converter/protocol.py'
if PROJECT.exists():
    SOURCES.append(PROJECT)


class BashPermissionViews(unittest.TestCase):
    def protocols(self):
        for path in SOURCES:
            spec = importlib.util.spec_from_file_location('permission_view_test', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            yield module

    def match(self, protocol, command, rule, action='deny'):
        event = protocol.normalize({'tool_name': 'exec_command', 'tool_input': {'cmd': command}, 'cwd': '/fixture'})
        return protocol.permission_match(event, {'permissions': {action: [rule]}}, action, Path('/fixture'))

    def test_every_operand_and_redirect_is_restricted(self):
        for protocol in self.protocols():
            for command, rule in [('cat public secret', 'Read(/secret)'),
                                  ('head -n 5 public secret', 'Read(/secret)'),
                                  ('tail -c10 public secret', 'Read(/secret)'),
                                  ('rg -e pattern public secret', 'Read(/secret)'),
                                  ('printf value > target', 'Edit(/target)'),
                                  ('custom-command > target', 'Edit(/target)'),
                                  ('cd ./sub && cat secret', 'Read(/sub/secret)')]:
                with self.subTest(source=protocol.__file__, command=command):
                    self.assertIsNotNone(self.match(protocol, command, rule))
                    self.assertIsNone(self.match(protocol, command, rule, 'ask'))

    def test_file_allows_never_authorize_bash(self):
        for protocol in self.protocols():
            for command in ['cat secret', 'cat "$FILE"', 'custom-command > secret']:
                self.assertIsNone(self.match(protocol, command, 'Read(/secret)', 'allow'))
                self.assertIsNone(self.match(protocol, command, 'Edit(/secret)', 'allow'))

    def test_unresolved_does_not_invent_paths_or_lose_bash_restriction(self):
        for protocol in self.protocols():
            self.assertIsNone(self.match(protocol, 'cat "$FILE"', 'Read(/secret)'))
            self.assertIsNone(self.match(protocol, 'custom_function; cat secret', 'Read(/secret)'))
            self.assertIsNotNone(self.match(protocol, 'cat "$FILE"', 'Bash(cat:*)'))

    def test_quoted_path_and_original_event_unchanged(self):
        for protocol in self.protocols():
            data = {'tool_name': 'Bash', 'tool_input': {'command': "cat 'two words'"}, 'cwd': '/fixture'}
            before = json.dumps(data, sort_keys=True)
            self.assertIsNotNone(protocol.permission_match(data, {'permissions': {'deny': ['Read(/two words)']}}, 'deny', Path('/fixture')))
            self.assertEqual(json.dumps(data, sort_keys=True), before)


class BashPermissionRuntime(unittest.TestCase):
    setUp = fixtures.NativeHookAskTests.setUp
    convert = fixtures.NativeHookAskTests.convert
    event = fixtures.NativeHookAskTests.event

    def test_deny_wins_over_earlier_file_ask(self):
        self.convert({'ask': ['Read(/public)'], 'deny': ['Read(/secret)']})
        self.assertEqual(self.event(1, inputs={'cmd': 'cat public secret'})['permissionDecision'], 'deny')

    def test_file_ask_does_not_propagate_and_bash_hook_runs_once(self):
        counter = self.out.parent / 'counter.jsonl'
        script = self.out.parent / 'hook.py'
        script.write_text('import json,sys\nfrom pathlib import Path\nd=json.load(sys.stdin)\n'
                          'with Path('+repr(str(counter))+').open("a") as f: f.write(json.dumps(d)+"\\n")\n'
                          'print("{}")\n')
        hooks = {'PreToolUse': [{'hooks': [{'type': 'command', 'command': shlex.quote(sys.executable)+' '+shlex.quote(str(script))}]}]}
        self.convert({'ask': ['Read(/secret)']}, hooks)
        self.assertNotIn('permissionDecision', self.event(1, inputs={'cmd': 'cat public secret'}))
        events = [json.loads(line) for line in counter.read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['tool_name'], 'Bash')
        self.assertEqual(events[0]['tool_input']['command'], 'cat public secret')


if __name__ == '__main__':
    unittest.main()
