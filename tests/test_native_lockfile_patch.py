"""Regress the status provider's matching Cargo manifest/lock dependency."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest


class NativeLockfilePatchTests(unittest.TestCase):
    def test_status_provider_patches_pty_lock_edge(self):
        package = Path(__file__).resolve().parents[1]
        folder = package / 'native/manifests/0.154.0'
        manifest = json.loads((folder / 'manifest.json').read_text())
        patch = folder / manifest['patch_file']
        self.assertEqual(hashlib.sha256(patch.read_bytes()).hexdigest(), manifest['patch_sha256'])
        baseline = '''version = 4

[[package]]
name = "codex-tui"
version = "0.154.0"
dependencies = [
 "codex-utils-path",
 "codex-utils-path-uri",
 "codex-utils-plugins",
 "codex-utils-sandbox-summary",
 "codex-utils-sleep-inhibitor",
 "codex-utils-string",
]
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'codex-rs').mkdir()
            lock = root / 'codex-rs/Cargo.lock'
            lock.write_text(baseline)
            result = subprocess.run(['git', 'apply', '--include=codex-rs/Cargo.lock', str(patch)], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = tomllib.loads(lock.read_text())
            expected = tomllib.loads(baseline)
            expected['package'][0]['dependencies'].insert(3, 'codex-utils-pty')
            self.assertEqual(actual, expected)
        self.assertIn('+codex-utils-pty = { workspace = true }', patch.read_text())


if __name__ == '__main__':
    unittest.main()
