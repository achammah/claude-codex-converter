"""Runner control-flow tests with scratch tools; not native compatibility evidence."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('release_runner', Path(__file__).resolve().parents[1]/'scripts/release_runner.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_source_notices_and_internal_license_link_are_materialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'source'; source.mkdir(); package = root/'package'; package.mkdir()
            for name in ('LICENSE', 'NOTICE', 'COPYING'):
                (source/name).write_text('original '+name)
            (source/'LICENSE.vendor').symlink_to('COPYING')
            def archive(contract, path, algorithm):
                with tarfile.open(path, 'w:gz'): pass
                return {'url':'https://fixture.invalid/archive', 'integrity':'fixture'}
            with patch.object(runner.subprocess, 'check_output', return_value=b'LICENSE\0NOTICE\0COPYING\0LICENSE.vendor\0'), patch.object(runner, 'download_attribution_archive', side_effect=archive):
                result = runner.preserve_attributions(source, package, {}, {}, root)
            self.assertEqual((package/'LICENSE').read_text(), 'original LICENSE')
            self.assertEqual((package/'NOTICE').read_text(), 'original NOTICE')
            vendor = package/'licenses/codex/LICENSE.vendor'
            self.assertEqual(vendor.read_text(), 'original COPYING')
            self.assertFalse(vendor.is_symlink())
            self.assertFalse(result['archives']['official-companions']['attributionPresent'])

    def test_attribution_paths_and_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root/'fixture.tar.gz'
            files = {'package/LICENSE':'exact upstream text\n', 'package/vendor/NOTICE.txt':'third party text',
                     'package/bin/program':'not attribution'}
            with tarfile.open(archive, 'w:gz') as tar:
                for name, content in files.items():
                    member = tarfile.TarInfo(name); data = content.encode(); member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
            records = runner.copy_archive_attributions(archive, root/'licenses')
            self.assertEqual({row['path'] for row in records}, {'package/LICENSE', 'package/vendor/NOTICE.txt'})
            self.assertEqual((root/'licenses/package/LICENSE').read_text(), files['package/LICENSE'])
            self.assertFalse((root/'licenses/package/bin/program').exists())

    def test_unsafe_attribution_archive_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, symlink in (('../LICENSE', False), ('package/LICENSE', True)):
                archive = root/'unsafe.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    member = tarfile.TarInfo(name)
                    if symlink: member.type = tarfile.SYMTYPE; member.linkname = '/etc/passwd'
                    tar.addfile(member)
                with self.assertRaises(ValueError):
                    runner.copy_archive_attributions(archive, root/'licenses')

    def test_missing_archive_attribution_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root/'empty.tar.gz'
            with tarfile.open(archive, 'w:gz'): pass
            self.assertEqual(runner.copy_archive_attributions(archive, root/'licenses'), [])

    def test_complete_release_identity_reaches_real_manager_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = 'cue-native-0.3.5-aarch64-apple-darwin'
            metadata = {'version': '0.154.0', 'target': 'aarch64-apple-darwin'}
            runner.stage_manager(root, metadata, target=root/'installed/codex',
                release_id=identity, sequence=3, feed_url='https://example.test/releases.json')
            self.assertEqual(metadata['cueUpdate']['releaseId'], identity)
            descriptor = json.loads((root/'codex-resources/compatible-releases.json').read_text())
            self.assertEqual(descriptor['releases'][0]['releaseId'], identity)

    def test_real_subprocess_failure_preserves_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp)/'failure.log'
            with self.assertRaises(subprocess.CalledProcessError):
                runner.execute([sys.executable, '-c', 'print("failed step");raise SystemExit(7)'], tmp, log)
            self.assertIn('failed step', log.read_text())

    def test_success_log_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp)/'success.log'
            digest = runner.execute([sys.executable, '-c', 'print("fixture only")'], tmp, log)
            self.assertEqual(digest, runner.pipeline.sha(log.read_bytes()))

    def test_smoke_requires_observed_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                runner.smoke('missing', [sys.executable, '-c', 'pass'], work)
            self.assertFalse((work/'evidence.json').exists())

    def test_smoke_rejects_false_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = ('import json,pathlib,sys; p=pathlib.Path(sys.argv[-1]);'
                    'p.mkdir();(p/"report.json").write_text(json.dumps({"passed":False}))')
            with self.assertRaisesRegex(ValueError, 'did not report'):
                runner.smoke('false', [sys.executable, '-c', code], Path(tmp))

    def test_smoke_records_real_scratch_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = ('import json,pathlib,sys; p=pathlib.Path(sys.argv[-1]);'
                    'p.mkdir();(p/"report.json").write_text(json.dumps({"passed":True}))')
            result = runner.smoke('fixture', [sys.executable, '-c', code], Path(tmp))
            self.assertTrue(result['passed'])
            self.assertEqual(len(result['reportSha256']), 64)

    def test_wrong_candidate_holds_without_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidate = root/'candidate.json'
            candidate.write_text('{"state":"candidate","version":"x"}')
            with patch.object(runner, 'execute') as execute:
                self.assertEqual(runner.main(['--candidate', str(candidate), '--manifest', str(root/'manifest'),
                    '--work', str(root/'work'), '--output', str(root/'output'), '--release-id', 'fixture', '--sequence', '1']), 1)
                execute.assert_not_called()
            self.assertFalse((root/'output/evidence.json').exists())

    def test_companion_rejects_wrong_version_and_origin(self):
        class Response:
            url = 'https://registry.npmjs.org/@openai%2fcodex/0.154.0-darwin-arm64'
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps({'name':'@openai/codex','version':'wrong'}).encode()
        with patch.object(runner.urllib.request, 'urlopen', return_value=Response()):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                runner.companion_contract('0.154.0', 'aarch64-apple-darwin')
        Response.url = 'https://example.invalid/metadata'
        with patch.object(runner.urllib.request, 'urlopen', return_value=Response()):
            with self.assertRaisesRegex(ValueError, 'redirect'):
                runner.companion_contract('0.154.0', 'aarch64-apple-darwin')

    def test_fetch_failure_holds_without_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidate = root/'candidate.json'
            candidate.write_text(json.dumps({'state':'candidate','version':'0.154.0',
                'tag':'rust-v0.154.0','commit':'a'*40}))
            def fail_fetch(argv, cwd, log, env=None):
                Path(log).write_text(json.dumps(argv))
                if 'fetch' in argv:
                    raise subprocess.CalledProcessError(1, argv)
                return 'a'*64
            with patch.object(runner, 'execute', side_effect=fail_fetch) as execute:
                self.assertEqual(runner.main(['--candidate',str(candidate),'--manifest',str(root/'manifest'),
                    '--work',str(root/'work'),'--output',str(root/'output'),'--release-id','fixture','--sequence','1']),1)
                self.assertEqual(execute.call_count, 2)
            self.assertTrue((root/'work/git-fetch.log').exists())
            self.assertFalse((root/'output/evidence.json').exists())


if __name__ == '__main__': unittest.main()
