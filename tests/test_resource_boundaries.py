"""Distinct selected packages must not silently share a converted namespace."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ResourceBoundaries(unittest.TestCase):
    def test_distinct_plugin_ids_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory(prefix='cue-plugin-boundary-') as temporary:
            base = Path(temporary).resolve()
            project = base / 'project'
            (project / '.claude').mkdir(parents=True)
            user = base / 'selected-user'
            (user / 'plugins').mkdir(parents=True)
            ids = ['foo@market-one', 'foo-market@one']
            registry = {}
            expected = {}
            for number, plugin_id in enumerate(ids):
                source = base / ('plugin-' + str(number))
                source.mkdir()
                marker = 'SYNTHETIC_PLUGIN_' + str(number)
                (source / 'README.md').write_text(marker)
                expected[str(source / 'README.md')] = marker
                registry[plugin_id] = [{'scope': 'user', 'installPath': str(source)}]
            (user / 'plugins/installed_plugins.json').write_text(json.dumps({'plugins': registry}))
            settings = user / 'settings.json'
            settings.write_text(json.dumps({'enabledPlugins': dict.fromkeys(ids, True)}))
            output = base / 'out'
            converter = Path(__file__).resolve().parents[1] / 'converter/claude_to_codex.py'
            result = subprocess.run([sys.executable, str(converter), str(project), '--output', str(output),
                                     '--global-settings', str(settings), '--include-user-resources'],
                                    text=True, capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            if result.returncode:
                self.assertIn('collision', result.stderr.lower())
                return
            manifest = json.loads((output / '.cue/file-manifest.json').read_text())
            rows = [row for row in manifest if row['source'] in expected]
            self.assertEqual(len(rows), 2)
            self.assertEqual(len({row['target'] for row in rows}), 2)
            for row in rows:
                self.assertEqual((output / row['target']).read_text(), expected[row['source']])


if __name__ == '__main__':
    unittest.main()
