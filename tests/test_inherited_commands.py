"""Inherited command definitions and their relative resources must stay usable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class InheritedCommands(unittest.TestCase):
    def test_user_command_registers_with_sibling_resource_and_host_mapping(self):
        with tempfile.TemporaryDirectory(prefix='cue-user-command-') as temporary:
            root = Path(temporary).resolve()
            project, user, out = root / 'project', root / 'user', root / 'out'
            (project / '.claude').mkdir(parents=True)
            (user / 'commands').mkdir(parents=True)
            (user / 'settings.json').write_text('{}')
            (user / 'commands/inspect.md').write_text('---\ndescription: Inspect a local item\n---\nRead help.txt and $ARGUMENTS.\n')
            (user / 'commands/help.txt').write_text('Preserve this help resource.\n')
            source = Path(__file__).resolve().parents[1] / 'converter/claude_to_codex.py'
            result = subprocess.run([sys.executable, str(source), str(project), '--output', str(out),
                                     '--global-settings', str(user / 'settings.json'), '--include-user-resources'],
                                    capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out / '.agents/skills/inspect/SKILL.md').is_file())
            self.assertEqual((out / '.cue/skills/inspect/help.txt').read_text(), 'Preserve this help resource.\n')
            mapping = json.loads((out / '.cue/command-skill-map.json').read_text())
            self.assertEqual(mapping['inspect'], '.cue/vendor/user/commands/inspect.md')
            self.assertIn('command-expansion', (out / '.cue/conversion-findings.json').read_text())


if __name__ == '__main__':
    unittest.main()
