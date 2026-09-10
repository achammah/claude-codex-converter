"""Public command dispatch and round trips over synthetic local files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BidirectionalCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cue-cli-roundtrip-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def cli(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT) + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.run(
            [sys.executable, '-c', 'from converter.cli import main; raise SystemExit(main())', *map(str, args)],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=20)

    def test_native_setup_roundtrip_through_public_commands(self):
        source = self.root/'codex'
        (source/'.codex').mkdir(parents=True)
        originals = {'.codex/config.toml': b'', 'AGENTS.md': b'Exact instructions\r\n'}
        for name, raw in originals.items():
            (source/name).write_bytes(raw)
        reverse = self.cli('reverse', source, '--output', self.root/'claude', '--strict')
        self.assertEqual(reverse.returncode, 0, reverse.stderr)
        forward = self.cli('convert', self.root/'claude', '--output', self.root/'restored')
        self.assertEqual(forward.returncode, 0, forward.stderr)
        for name, raw in originals.items():
            self.assertEqual((self.root/'restored'/name).read_bytes(), raw)

    def test_conversation_pair_through_public_commands(self):
        source = self.root/'source.jsonl'
        row = {'type': 'user', 'uuid': 'u1', 'parentUuid': None, 'sessionId': 'fixture',
               'timestamp': '2026-01-01T00:00:00Z', 'cwd': '/synthetic',
               'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'Exact café\n'}]}}
        raw = (json.dumps(row, ensure_ascii=False) + '\n').encode()
        source.write_bytes(raw)
        converted = self.cli('conversation', '--from', 'claude', '--to', 'codex',
                             '--input', source, '--output', self.root/'codex-bundle')
        self.assertEqual(converted.returncode, 0, converted.stderr)
        restored = self.cli('conversation', '--from', 'codex', '--to', 'claude',
                            '--input', self.root/'codex-bundle/generated/rollout.jsonl',
                            '--output', self.root/'restored-bundle')
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((self.root/'restored-bundle/generated/claude-session.jsonl').read_bytes(), raw)

    def test_modified_generated_setup_is_not_silently_restored(self):
        source = self.root/'codex'; (source/'.codex').mkdir(parents=True)
        (source/'.codex/config.toml').write_text('')
        self.assertEqual(self.cli('reverse', source, '--output', self.root/'claude').returncode, 0)
        (self.root/'claude/.claude/settings.json').write_text('{"changed":true}')
        result = self.cli('convert', self.root/'claude', '--output', self.root/'restored')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'restored/.codex/config.toml').exists())
