"""Native Codex source fixtures: no archive short-cut, no server execution."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

CODE = Path(__file__).resolve().parents[1] / 'converter'
sys.path.insert(0, str(CODE))
from codex_to_claude import stage_codex_to_claude, restore_codex_original


class ReverseMcpPermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='reverse-mcp-permissions-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'source'
        (self.source / '.codex').mkdir(parents=True)

    def stage(self, config, name='out'):
        path = self.source / '.codex/config.toml'
        path.write_text(config)
        before = path.read_bytes()
        self.out = self.base / name
        self.report = stage_codex_to_claude(self.source, self.out, strict=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.report['mode'], 'native-codex')
        return json.loads((self.out / '.claude/settings.json').read_text())

    def test_exact_modes_and_disabled_tools(self):
        settings = self.stage('''[mcp_servers.demo]
command="DO_NOT_EXECUTE"
disabled_tools=["delete"]
[mcp_servers.demo.tools.read]
approval_mode="approve"
[mcp_servers.demo.tools.write]
approval_mode="prompt"
''')
        self.assertEqual(settings['permissions'], {'defaultMode': 'plan',
                         'allow': ['mcp__demo__read'], 'ask': ['mcp__demo__write'],
                         'deny': ['mcp__demo__delete']})
        self.assertEqual(self.report['exit_code'], 2)  # Restrictive review default is explicit.
        self.assertFalse(any(f['category'] == 'mcp-setting' for f in self.report['findings']))
        self.assertNotIn('mcp__demo__*', json.dumps(settings))

    def test_disabled_beats_approval_override(self):
        settings = self.stage('''[mcp_servers.demo]
command="DO_NOT_EXECUTE"
disabled_tools=["read", "read", "write"]
tools={read={approval_mode="approve"},write={approval_mode="prompt"}}
''')
        self.assertEqual(settings['permissions'], {'defaultMode': 'plan',
                         'deny': ['mcp__demo__read', 'mcp__demo__write']})

    def test_unknown_and_default_modes_remain_gaps(self):
        settings = self.stage('''[mcp_servers.demo]
command="DO_NOT_EXECUTE"
default_tools_approval_mode="approve"
tools={one={approval_mode="auto"},two={approval_mode="writes"},three={approval_mode="future"}}
''')
        self.assertEqual(settings['permissions'], {'defaultMode': 'plan'})
        self.assertEqual(sum(f['category'] == 'mcp-permission' for f in self.report['findings']), 3)
        self.assertTrue(any(f['source'].endswith('/default_tools_approval_mode') for f in self.report['findings']))

    def test_only_default_mode_still_requires_review(self):
        settings = self.stage('[mcp_servers.demo]\ncommand="FIXTURE"\ndefault_tools_approval_mode="prompt"\n')
        self.assertEqual(settings['permissions'], {'defaultMode': 'plan'})

    def test_exclusive_list_cannot_accidentally_grant_excluded_tool(self):
        settings = self.stage('''[mcp_servers.demo]
command="DO_NOT_EXECUTE"
enabled_tools=["read"]
tools={read={approval_mode="approve"},write={approval_mode="approve"}}
''')
        self.assertEqual(settings['permissions']['allow'], ['mcp__demo__read'])
        self.assertTrue(any(f['source'].endswith('/enabled_tools') for f in self.report['findings']))
        self.assertTrue(any('excluded' in f['message'] for f in self.report['findings']))

    def test_exact_names_and_unrelated_servers(self):
        settings = self.stage('''[mcp_servers."my.server-x"]
command="DO_NOT_EXECUTE"
tools={"read.items-v2"={approval_mode="approve"}}
[mcp_servers.other]
url="https://invalid.example/mcp"
''')
        self.assertEqual(settings['permissions']['allow'], ['mcp__my.server-x__read.items-v2'])
        servers = json.loads((self.out / '.mcp.json').read_text())['mcpServers']
        self.assertEqual(set(servers), {'my.server-x', 'other'})
        self.assertNotIn('disabledMcpjsonServers', settings)

    def test_globs_and_ambiguous_identities_are_not_rules(self):
        settings = self.stage('''[mcp_servers.a]
command="FIXTURE"
disabled_tools=["*"]
tools={"b__read"={approval_mode="approve"}}
[mcp_servers.a__b]
command="FIXTURE"
tools={read={approval_mode="approve"}}
''')
        self.assertEqual(settings['permissions'], {'defaultMode': 'plan'})
        self.assertEqual(sum(f['category'] == 'mcp-permission' for f in self.report['findings']), 3)

    def test_missing_transport_never_creates_grant(self):
        settings = self.stage('[mcp_servers.demo.tools.read]\napproval_mode="approve"\n')
        self.assertNotIn('permissions', settings)
        self.assertTrue(any(f['category'] == 'mcp-transport' for f in self.report['findings']))

    def test_extra_tool_control_retained_as_gap(self):
        settings = self.stage('''[mcp_servers.demo]
command="FIXTURE"
tools={read={approval_mode="approve",output_token_limit=123}}
''')
        self.assertEqual(settings['permissions']['allow'], ['mcp__demo__read'])
        self.assertTrue(any(f['source'].endswith('/output_token_limit') for f in self.report['findings']))

    def test_malformed_controls_fail_without_activation(self):
        for index, field in enumerate(['tools=[]', 'tools={read="approve"}',
                                        'disabled_tools="read"', 'enabled_tools=[123]']):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.stage('[mcp_servers.demo]\ncommand="FIXTURE"\n'+field+'\n', name='bad'+str(index))

    def test_native_roundtrip_and_determinism(self):
        config = '[mcp_servers.demo]\ncommand="FIXTURE"\ndisabled_tools=["write"]\ntools={read={approval_mode="approve"}}\n'
        self.stage(config, name='one')
        restored = self.base / 'restored'
        restore_codex_original(self.out, restored)
        self.assertEqual((restored / '.codex/config.toml').read_bytes(), config.encode())
        first = {p.relative_to(self.out).as_posix(): p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        self.stage(config, name='two')
        second = {p.relative_to(self.out).as_posix(): p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
