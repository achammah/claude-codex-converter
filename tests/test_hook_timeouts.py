"""Prevent the converter's 120-second wrapper from cutting off source deadlines.

Run: python3 -m unittest discover -s tests -p test_hook_timeouts.py -v
Synthetic settings only. No source hook or long sleep executes.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CONVERTER = Path(__file__).resolve().parents[1] / 'converter/claude_to_codex.py'


class HookTimeoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='converter-deadline-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'source'
        self.output = self.root / 'out'
        (self.source / '.claude').mkdir(parents=True)
        self.global_settings = self.root / 'global.json'
        self.global_settings.write_text('{}')
        self.env = dict(os.environ, CUE_STATE_ROOT=str(self.root / 'state'), PYTHONDONTWRITEBYTECODE='1')

    def convert(self, events):
        hooks = {event: [{'hooks': [{'type': 'command', 'command': 'echo synthetic', **fields}
                                   for fields in handlers]}] for event, handlers in events.items()}
        (self.source / '.claude/settings.json').write_text(json.dumps({'hooks': hooks}))
        result = subprocess.run([sys.executable, str(CONVERTER), str(self.source), '--output', str(self.output),
                                 '--global-settings', str(self.global_settings)], env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.hooks = json.loads((self.output / '.codex/hooks.json').read_text())['hooks']
        self.findings = json.loads((self.output / '.cue/conversion-findings.json').read_text())

    def timeout(self, event):
        return self.hooks[event][0]['hooks'][0]['timeout']

    def child_timeouts(self, event, *, tool='Bash', tool_input=None):
        # Import the actual generated runtime in a fresh process, replacing only
        # its subprocess boundary. This observes dispatched arguments, not a
        # reimplementation of timeout selection.
        program = '''import importlib.util, json, sys
from pathlib import Path
from unittest.mock import patch
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('generated_hook', root / '.cue/scripts/converted_hook.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
data = json.load(sys.stdin)
routes = json.loads((root / '.cue/hook-routes.json').read_text())
seen = []
def run(*args, **kwargs):
    seen.append(kwargs['timeout'])
    return runtime.subprocess.CompletedProcess(args, 0, '', '')
with patch.object(runtime.subprocess, 'run', side_effect=run):
    runtime.dispatch(data, routes)
print(json.dumps(seen))
'''
        event_data = {'hook_event_name': event, 'tool_name': tool,
                      'tool_input': tool_input or {}, 'cwd': str(self.output), 'session_id': 'fixture'}
        result = subprocess.run([sys.executable, '-c', program, str(self.output)], env=self.env,
                                input=json.dumps(event_data), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_explicit_long_timeout_reaches_native_config_and_source_process(self):
        self.convert({'PreToolUse': [{'timeout': 86400}]})
        self.assertEqual(self.timeout('PreToolUse'), 86430)
        self.assertEqual(self.child_timeouts('PreToolUse'), [86400])

    def test_explicit_short_fractional_timeout_is_not_extended_in_child(self):
        self.convert({'Stop': [{'timeout': 0.25}]})
        self.assertEqual(self.timeout('Stop'), 31)
        self.assertEqual(self.child_timeouts('Stop'), [0.25])

    def test_unspecified_command_deadlines_follow_source_event_defaults(self):
        self.convert({'Stop': [{}], 'UserPromptSubmit': [{}], 'SessionEnd': [{}]})
        self.assertEqual(self.timeout('Stop'), 630)
        self.assertEqual(self.timeout('UserPromptSubmit'), 60)
        self.assertEqual(self.child_timeouts('Stop'), [600])
        self.assertEqual(self.child_timeouts('UserPromptSubmit'), [30])
        self.assertEqual(self.child_timeouts('SessionEnd'), [1.5])

    def test_sequential_routes_receive_sum_not_maximum_budget(self):
        self.convert({'Stop': [{'timeout': 80}, {'timeout': 90}]})
        self.assertEqual(self.timeout('Stop'), 200)
        self.assertEqual(self.child_timeouts('Stop'), [80, 90])

    def test_post_tool_native_budget_includes_failure_route(self):
        self.convert({'PostToolUse': [{'timeout': 12}], 'PostToolUseFailure': [{'timeout': 86400}]})
        self.assertEqual(self.timeout('PostToolUse'), 86442)
        self.assertNotIn('PostToolUseFailure', self.hooks)
        self.assertEqual(self.child_timeouts('PostToolUseFailure'), [86400])

    def test_short_native_lifecycle_limit_is_explicitly_reported(self):
        self.convert({'SessionEnd': [{'timeout': 86400}], 'Interrupt': [{'timeout': 20}]})
        for event, source_seconds in [('SessionEnd', 86400), ('Interrupt', 20)]:
            self.assertEqual(self.timeout(event), 3)
            self.assertEqual(self.child_timeouts(event), [source_seconds])
            row = next(row for row in self.findings if row['category'] == 'hook-lifecycle-budget' and row['event'] == event)
            self.assertEqual(row['status'], 'manual')
            self.assertEqual(row['source_timeouts'], [source_seconds])

    def test_patch_repeated_view_deadlines_are_retained_and_gap_reported(self):
        self.convert({'PreToolUse': [{'timeout': 7}]})
        seen = self.child_timeouts('PreToolUse', tool='apply_patch', tool_input={
            'command': '*** Begin Patch\n*** Add File: a.txt\n+a\n*** Add File: b.txt\n+b\n*** End Patch'})
        self.assertEqual(seen, [7, 7])
        row = next(row for row in self.findings if row['category'] == 'hook-patch-budget')
        self.assertEqual(row['status'], 'manual')
        self.assertEqual(row['event'], 'PreToolUse')

    def test_invalid_or_unrepresentable_deadlines_reject_before_native_config(self):
        for index, value in enumerate([0, -1, True, '600', None, float('inf'), float('nan'), 10 ** 400, 2 ** 64]):
            with self.subTest(value=str(value)):
                settings = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo synthetic', 'timeout': value}]}]}}
                (self.source / '.claude/settings.json').write_text(json.dumps(settings))
                output = self.root / ('invalid-' + str(index))
                result = subprocess.run([sys.executable, str(CONVERTER), str(self.source), '--output', str(output),
                                         '--global-settings', str(self.global_settings)], env=self.env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('timeout', result.stderr.lower())
                self.assertFalse((output / '.codex/hooks.json').exists())

    def test_combined_deadlines_outside_native_range_reject(self):
        settings = {'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': 'echo synthetic', 'timeout': 2 ** 63},
            {'type': 'command', 'command': 'echo synthetic', 'timeout': 2 ** 63}]}]}}
        (self.source / '.claude/settings.json').write_text(json.dumps(settings))
        result = subprocess.run([sys.executable, str(CONVERTER), str(self.source), '--output', str(self.output),
                                 '--global-settings', str(self.global_settings)], env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Combined hook timeout', result.stderr)
        self.assertFalse((self.output / '.codex/hooks.json').exists())


if __name__ == '__main__':
    unittest.main()
