"""Filesystem-only shape checks; these do not execute Linux binaries."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('update_package_probe', Path(__file__).parent / 'native_smoke/update_package.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class NativeSmokeLayoutTests(unittest.TestCase):
    def test_linux_both_libcs_require_sandbox_file_and_link(self):
        for target in ('x86_64-unknown-linux-musl', 'aarch64-unknown-linux-gnu'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                package, visible = root / 'package', root / 'visible'
                visible.mkdir()
                names, files = probe.package_layout({'target': target})
                self.assertIn('codex-linux-sandbox', names)
                self.assertIn('bin/codex-linux-sandbox', files)
                for relative in files:
                    path = package / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(relative.encode())
                for name in names:
                    (visible / name).symlink_to(package / 'bin' / name)
                probe.assert_links(visible, package, names)
                before = probe.file_inventory(package)
                sandbox = package / 'bin/codex-linux-sandbox'
                sandbox.write_bytes(b'changed')
                self.assertNotEqual(probe.file_inventory(package), before)
                (visible / 'codex-linux-sandbox').unlink()
                with self.assertRaises(AssertionError):
                    probe.assert_links(visible, package, names)

    def test_macos_omits_linux_helper(self):
        names, files = probe.package_layout({'target': 'aarch64-apple-darwin'})
        self.assertEqual(names, ('codex', 'codex-code-mode-host'))
        self.assertEqual(files, ('bin/codex', 'bin/codex-code-mode-host', 'codex-path/rg'))

    def test_inventory_detects_extra_resource_and_mode_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resource = root / 'NOTICE'
            resource.write_text('notice')
            original = probe.file_inventory(root)
            resource.chmod(0o700)
            self.assertNotEqual(probe.file_inventory(root), original)
            original = probe.file_inventory(root)
            (root / 'extra').write_text('new')
            self.assertNotEqual(probe.file_inventory(root), original)


if __name__ == '__main__':
    unittest.main()
