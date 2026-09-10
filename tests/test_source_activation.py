"""Explicit source disablement must survive conversion; no hooks are executed."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

CONVERTER = Path(__file__).resolve().parents[1] / 'converter/claude_to_codex.py'


class SourceActivation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cue-source-activation-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'project'
        self.output = self.base / 'out'
        (self.source / '.claude').mkdir(parents=True)
        self.global_settings = self.base / 'global.json'
        self.global_settings.write_text('{}')

    def convert(self, settings):
        (self.source / '.claude/settings.json').write_text(json.dumps(settings))
        result = subprocess.run([sys.executable, str(CONVERTER), str(self.source), '--output', str(self.output),
                                 '--global-settings', str(self.global_settings)],
                                capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disable_all_hooks_suppresses_settings_and_skill_routes(self):
        skill = self.source / '.claude/skills/synthetic/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('''---
name: synthetic
description: Synthetic source skill.
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: echo synthetic-disabled
---
Synthetic instructions.
''')
        self.convert({'disableAllHooks': True, 'hooks': {'SessionStart': [
            {'hooks': [{'type': 'command', 'command': 'echo synthetic-disabled'}]}]}})
        routes = json.loads((self.output / '.cue/hook-routes.json').read_text())
        self.assertFalse(any(routes.values()), routes)

    def test_disabled_mcp_server_is_omitted_or_disabled(self):
        (self.source / '.mcp.json').write_text(json.dumps({'mcpServers': {
            'disabled-server': {'command': 'echo', 'args': ['synthetic-disabled']},
            'active-server': {'command': 'echo', 'args': ['synthetic-active']}}}))
        self.convert({'disabledMcpjsonServers': ['disabled-server']})
        servers = tomllib.loads((self.output / '.codex/config.toml').read_text())['mcp_servers']
        self.assertTrue('disabled-server' not in servers or servers['disabled-server'].get('enabled') is False, servers)
        self.assertIn('active-server', servers)


if __name__ == '__main__':
    unittest.main()
