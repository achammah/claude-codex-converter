"""Exact MCP permissions: isolated conversion only, no MCP server executes."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

CODE = Path(__file__).resolve().parents[1] / 'converter'
sys.path.insert(0, str(CODE))
from claude_to_codex import Converter
from codex_to_claude import stage_codex_to_claude


class McpPermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mcp-permissions-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'source'
        (self.source / '.claude').mkdir(parents=True)
        self.global_settings = self.base / 'global.json'
        self.global_settings.write_text('{}')
        self.out = self.base / 'out'

    def convert(self, permissions, servers=None, local=None):
        if servers is None:
            servers = {'demo': {'command': 'DO_NOT_EXECUTE_FIXTURE'}}
        (self.source / '.claude/settings.json').write_text(json.dumps({'permissions': permissions}))
        (self.source / '.mcp.json').write_text(json.dumps({'mcpServers': servers}))
        if local is not None:
            (self.source / '.claude/settings.local.json').write_text(json.dumps({'permissions': local}))
        self.before = self.snapshot(self.source)
        result = subprocess.run([sys.executable, str(CODE / 'claude_to_codex.py'), str(self.source),
                                 '--output', str(self.out), '--global-settings', str(self.global_settings)],
                                capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.before, self.snapshot(self.source))
        self.findings = json.loads((self.out / '.cue/conversion-findings.json').read_text())
        return tomllib.loads((self.out / '.codex/config.toml').read_text())['mcp_servers']

    @staticmethod
    def snapshot(root):
        return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in root.rglob('*') if p.is_file()}

    def test_exact_three_decisions_preserve_unmentioned_tools(self):
        servers = self.convert({'allow': ['mcp__demo__read'], 'ask': ['mcp__demo__write'],
                                'deny': ['mcp__demo__delete']})
        self.assertEqual(servers['demo']['tools'], {'read': {'approval_mode': 'approve'},
                                                  'write': {'approval_mode': 'prompt'}})
        self.assertEqual(servers['demo']['disabled_tools'], ['delete'])
        self.assertNotIn('enabled_tools', servers['demo'])
        self.assertNotIn('default_tools_approval_mode', servers['demo'])
        self.assertNotIn('tools', servers['cue_questions'])
        self.assertFalse(any(f['status'] == 'manual' and f['category'] == 'mcp-permission'
                             for f in self.findings))

    def test_most_restrictive_across_settings_layers(self):
        servers = self.convert({'allow': ['mcp__demo__one', 'mcp__demo__two'],
                                'deny': ['mcp__demo__three']},
                               local={'ask': ['mcp__demo__one', 'mcp__demo__two'],
                                      'deny': ['mcp__demo__two'], 'allow': ['mcp__demo__three']})
        self.assertEqual(servers['demo']['tools'], {'one': {'approval_mode': 'prompt'}})
        self.assertEqual(servers['demo']['disabled_tools'], ['three', 'two'])

    def test_unknown_wildcard_unsupported_and_reserved_do_not_grant(self):
        rules = ['mcp__unknown__read', 'mcp__demo__*', 'mcp__demo',
                 'mcp__old__read', 'mcp__cue_questions__AskUserQuestion', 'mcp__demo__read(*)']
        servers = self.convert({'allow': rules}, servers={
            'demo': {'command': 'DO_NOT_EXECUTE_FIXTURE'}, 'old': {'type': 'sse', 'url': 'https://invalid.example'}})
        self.assertNotIn('tools', servers['demo'])
        self.assertNotIn('old', servers)
        self.assertNotIn('tools', servers['cue_questions'])
        gaps = [f for f in self.findings if f['category'] == 'mcp-permission' and f['status'] == 'manual']
        self.assertEqual({f['source'] for f in gaps}, set(rules))

    def test_overlapping_wildcard_restriction_withholds_approval(self):
        servers = self.convert({'allow': ['mcp__demo__read'], 'ask': ['mcp__demo__write'],
                                'deny': ['mcp__demo__*', 'mcp__demo__delete']})
        self.assertNotIn('tools', servers['demo'])
        self.assertEqual(servers['demo']['disabled_tools'], ['delete'])
        self.assertEqual(sum('overlaps' in f['rule'] for f in self.findings), 2)

    def test_bare_server_ask_withholds_allow_without_affecting_other_server(self):
        servers = self.convert({'allow': ['mcp__demo__read', 'mcp__other__read'],
                                'ask': ['mcp__demo']}, servers={
                                    'demo': {'command': 'FIXTURE'}, 'other': {'command': 'FIXTURE'}})
        self.assertNotIn('tools', servers['demo'])
        self.assertEqual(servers['other']['tools']['read']['approval_mode'], 'approve')

    def test_generic_tool_pattern_deny_withholds_approval(self):
        servers = self.convert({'allow': ['mcp__demo__read'], 'deny': ['*']})
        self.assertNotIn('tools', servers['demo'])

    def test_ambiguous_separator_identity_is_not_guessed(self):
        servers = self.convert({'allow': ['mcp__a__b__read']}, servers={
            'a': {'command': 'FIXTURE'}, 'a__b': {'command': 'FIXTURE'}})
        self.assertNotIn('tools', servers['a'])
        self.assertNotIn('tools', servers['a__b'])

    def test_dotted_and_hyphenated_names_are_literal_toml_keys(self):
        servers = self.convert({'ask': ['mcp__my.server-x__read.items-v2']}, servers={
            'my.server-x': {'command': 'FIXTURE'}})
        self.assertEqual(servers['my.server-x']['tools'], {'read.items-v2': {'approval_mode': 'prompt'}})

    def test_roundtrip_preserves_original_source_bytes(self):
        self.convert({'allow': ['mcp__demo__read'], 'ask': ['mcp__demo__write'],
                      'deny': ['mcp__demo__delete']})
        with contextlib.redirect_stdout(io.StringIO()):
            report = stage_codex_to_claude(self.out, self.base / 'restored', strict=True)
        self.assertEqual(report['exit_code'], 0)
        for path, (data, _) in self.before.items():
            self.assertEqual((self.base / 'restored' / path).read_bytes(), data)

    def test_native_prompt_setting_does_not_silently_release_source_ask(self):
        self.convert({'ask': ['mcp__demo__write']})
        event = {'hook_event_name': 'PreToolUse', 'tool_name': 'mcp__demo__write',
                 'tool_input': {}, 'session_id': 'isolated-mcp-ask', 'cwd': str(self.out)}
        result = subprocess.run([sys.executable, str(self.out / '.cue/scripts/converted_hook.py')],
                                input=json.dumps(event), capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                                         CUE_STATE_ROOT=str(self.base / 'state')))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertTrue(any(f['category'] == 'mcp-permission-runtime' and f['status'] == 'manual'
                            for f in self.findings))

    def test_mapping_deterministic_under_rule_order(self):
        rows = [{'original_rule': 'mcp__demo__' + tool, 'action': action}
                for tool, action in [('z', 'deny'), ('a', 'allow'), ('b', 'ask'), ('a', 'ask')]]
        def policy(order):
            converter = Converter.__new__(Converter)
            converter.permission_rules, converter.findings = order, []
            return converter.mcp_permissions({'demo': {'command': 'FIXTURE'}})
        self.assertEqual(policy(rows), policy(list(reversed(rows))))

    def test_repeat_conversion_produces_identical_bytes(self):
        permissions = {'allow': ['mcp__demo__read'], 'ask': ['mcp__demo__write'],
                       'deny': ['mcp__demo__delete']}
        self.convert(permissions)
        before = self.snapshot(self.out)
        shutil.rmtree(self.out)  # Only this test's disposable conversion output.
        self.convert(permissions)
        self.assertEqual(before, self.snapshot(self.out))

    @unittest.skipUnless(os.environ.get('CUE_TEST_NATIVE_CODEX'), 'Opt-in installed native config parser')
    def test_native_config_parser_accepts_controls_and_rejects_invalid_mode(self):
        self.convert({'allow': ['mcp__demo__read'], 'ask': ['mcp__demo__write'],
                      'deny': ['mcp__demo__delete']})
        home = self.base / 'native-home'
        home.mkdir()
        shutil.copy2(self.out / '.codex/config.toml', home / 'config.toml')
        result = subprocess.run([os.environ['CUE_TEST_NATIVE_CODEX'], 'mcp', 'get', 'demo', '--json'],
                                cwd=self.base, capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, CODEX_HOME=str(home)))
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed['disabled_tools'], ['delete'])
        self.assertIsNone(parsed['enabled_tools'])
        # mcp get's JSON omits per-tool modes. A malformed enum establishes that
        # the installed parser actually consumes that field rather than ignores it.
        config_path = home / 'config.toml'
        config_path.write_text(config_path.read_text().replace('"approval_mode" = "approve"',
                                                              '"approval_mode" = "INVALID_FIXTURE_MODE"'))
        invalid = subprocess.run([os.environ['CUE_TEST_NATIVE_CODEX'], 'mcp', 'get', 'demo', '--json'],
                                 cwd=self.base, capture_output=True, text=True, timeout=30,
                                 env=dict(os.environ, CODEX_HOME=str(home)))
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn('INVALID_FIXTURE_MODE', invalid.stderr)


if __name__ == '__main__':
    unittest.main()
