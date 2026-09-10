import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path=Path(__file__).resolve().parents[1]/'scripts/export_public.py'
spec=importlib.util.spec_from_file_location('export_public',path);exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source';self.source.mkdir()
        (self.source/'LICENSE').write_text('Fixture license\n')
        (self.source/'code.py').write_text('value = 1\n')
        self.allow=('LICENSE','code.py')

    def run_export(self,name='out'):
        return exporter.export(self.source,self.root/name,self.root/(name+'.scan.json'),allowlist=self.allow)

    def test_excludes_unlisted_private_and_new_files(self):
        for name in ['verification/private.json','.git/config','new.py','dist/binary']:
            p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('unreviewed')
        self.run_export();files={p.relative_to(self.root/'out').as_posix() for p in (self.root/'out').rglob('*') if p.is_file()}
        self.assertEqual(files,{'LICENSE','code.py','PUBLIC-SOURCE-MANIFEST.json'})

    def test_deterministic_manifest_and_contents(self):
        a=self.run_export('a');b=self.run_export('b');self.assertEqual(a,b)
        self.assertEqual((self.root/'a/PUBLIC-SOURCE-MANIFEST.json').read_bytes(),(self.root/'b/PUBLIC-SOURCE-MANIFEST.json').read_bytes())

    def test_secret_blocks_export_and_report_does_not_echo_it(self):
        secret='ghp_'+'x'*30;(self.source/'code.py').write_text(secret)
        with self.assertRaisesRegex(ValueError,'scan failed'):self.run_export()
        self.assertFalse((self.root/'out').exists());report=(self.root/'out.scan.json').read_text();self.assertNotIn(secret,report);self.assertIn('credential-token',report)

    def test_private_evidence_link_blocks_export(self):
        (self.source/'code.py').write_text('.cue/'+'runtime-fixes/private.json')
        with self.assertRaisesRegex(ValueError,'scan failed'):self.run_export()

    def test_symlink_rejected(self):
        (self.source/'code.py').unlink();(self.source/'code.py').symlink_to(self.source/'LICENSE')
        with self.assertRaisesRegex(ValueError,'Symlink'):self.run_export()

    def test_binary_rejected(self):
        (self.source/'code.py').write_bytes(b'prefix\0binary')
        with self.assertRaisesRegex(ValueError,'Binary'):self.run_export()

    def test_missing_allowlisted_file_rejected(self):
        (self.source/'LICENSE').unlink()
        with self.assertRaises(FileNotFoundError):self.run_export()
        self.assertFalse((self.root/'out').exists())

    def test_existing_destination_untouched(self):
        self.run_export();before=(self.root/'out/code.py').read_bytes()
        with self.assertRaisesRegex(ValueError,'already exists'):self.run_export()
        self.assertEqual((self.root/'out/code.py').read_bytes(),before)

    def test_private_literal_scan_is_configurable(self):
        (self.source/'code.py').write_text('private-customer-fixture')
        with self.assertRaisesRegex(ValueError,'scan failed'):
            exporter.export(self.source,self.root/'out',self.root/'scan.json',forbidden=['private-customer-fixture'],allowlist=self.allow)


if __name__=='__main__':unittest.main()
