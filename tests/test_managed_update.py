import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from converter import managed_update as update
from converter import native_runtime as runtime

class ManagedUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cue-managed-update-test-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.bin=self.root/'visible';self.bin.mkdir();self.target=self.bin/'codex'
        self.current=self.package('installed','release-1',1)
        for name in ('codex','codex-code-mode-host'):(self.bin/name).symlink_to(self.current/'bin'/name)
        self.installation=self.current/'codex-package.json'
        self.config=self.root/'project/.codex/config.toml';self.config.parent.mkdir(parents=True);self.config.write_text('user-setting = true\n')

    def package(self,folder,release,sequence,markers=True,target='aarch64-apple-darwin'):
        root=self.root/folder;(root/'bin').mkdir(parents=True);(root/'codex-path').mkdir()
        for name in ('codex','codex-code-mode-host'):
            path=root/'bin'/name;path.write_text('#!/bin/sh\n# '+(' '.join(update.REQUIRED_MARKERS) if markers else '')+'\necho codex-cli 0.153.4\n');path.chmod(0o755)
        rg=root/'codex-path/rg';rg.write_text('#!/bin/sh\necho ripgrep\n');rg.chmod(0o755)
        meta={'layoutVersion':1,'version':'0.153.4','target':target,'entrypoint':'bin/codex','resourcesDir':'codex-resources','pathDir':'codex-path'}
        update.stage_manager(root,meta,target=self.target,runtime_source=Path(runtime.__file__),release_id=release,sequence=sequence)
        (root/'codex-package.json').write_text(json.dumps(meta));return root

    def feed(self,markers=True,sequence=2,target='aarch64-apple-darwin',mutate=None):
        root=self.package('candidate','release-'+str(sequence),sequence,markers,target)
        if mutate:mutate(root)
        archive=self.root/'release.zip'
        with zipfile.ZipFile(archive,'w') as z:
            for p in sorted(root.rglob('*')):
                if p.is_file():z.write(p,p.relative_to(root))
        row={'releaseId':'release-'+str(sequence),'sequence':sequence,'version':'0.153.4','target':target,'compatibility':{'validated':True,'markers':list(update.REQUIRED_MARKERS)},'archive':{'location':str(archive),'sha256':update.digest(archive.read_bytes())}}
        path=self.root/'releases.json';path.write_text(json.dumps({'schemaVersion':1,'manager':update.MANAGER,'releases':[row]}));return path

    def test_bundled_check_is_current_offline_and_deterministic(self):
        with patch.object(update.urllib.request,'urlopen',side_effect=AssertionError('network')):
            first=update.selection(self.installation)[0];self.assertEqual(first,update.selection(self.installation)[0])
        self.assertEqual(first['state'],'current');self.assertEqual(first['version'],'0.153.4')

    def configure_feed(self, url='https://updates.example.invalid/feed.json'):
        meta=json.loads(self.installation.read_text());meta['cueUpdate']['feedUrl']=url
        self.installation.write_text(json.dumps(meta));return url

    def test_configured_feed_is_default(self):
        url=self.configure_feed();source=self.feed()
        response=io.BytesIO(source.read_bytes());response.geturl=lambda:url
        with patch.object(update.urllib.request,'urlopen',return_value=response) as fetch:
            self.assertEqual(update.selection(self.installation)[0]['state'],'update_available')
        fetch.assert_called_once_with(url,timeout=30)

    def test_configured_feed_failure_never_falls_back_to_current(self):
        self.configure_feed()
        with patch.object(update.urllib.request,'urlopen',side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(OSError,'unavailable'):update.selection(self.installation)

    def test_explicit_source_overrides_configured_feed(self):
        self.configure_feed();source=self.feed()
        with patch.object(update.urllib.request,'urlopen',side_effect=AssertionError('network')):
            self.assertEqual(update.selection(self.installation,source)[0]['state'],'update_available')

    def test_feed_redirect_http_rejected(self):
        self.configure_feed();response=io.BytesIO(b'{}');response.geturl=lambda:'http://example.invalid/feed'
        with patch.object(update.urllib.request,'urlopen',return_value=response):
            with self.assertRaisesRegex(ValueError,'HTTPS'):update.selection(self.installation)

    def test_feed_credentials_and_invalid_schemes_rejected(self):
        for url in ['https://name:secret@example.invalid/feed','http://example.invalid','file:///tmp/feed','https:///missing','https://example.invalid/#fragment']:
            with self.subTest(url=url):
                self.configure_feed(url)
                with self.assertRaisesRegex(ValueError,'HTTPS'):update.selection(self.installation)

    def test_update_preserves_feed_and_rollback(self):
        url=self.configure_feed();source=self.feed(mutate=lambda root:self.change_package_feed(root,'https://other.example.invalid/feed'))
        result=update.update(self.installation,source);new=Path(result['installation'])
        self.assertEqual(json.loads(new.read_text())['cueUpdate']['feedUrl'],url)
        update.rollback(new,Path(result['receipt']))
        self.assertEqual(json.loads(self.installation.read_text())['cueUpdate']['feedUrl'],url)

    def change_package_feed(self,root,url):
        path=root/'codex-package.json';meta=json.loads(path.read_text());meta['cueUpdate']['feedUrl']=url;path.write_text(json.dumps(meta))

    def test_stage_manager_persists_validated_feed(self):
        metadata={'version':'0.153.4','target':'aarch64-apple-darwin'};root=self.root/'staged';root.mkdir()
        update.stage_manager(root,metadata,target=self.target,runtime_source=Path(runtime.__file__),release_id='staged',sequence=2,feed_url='https://example.invalid/feed')
        self.assertEqual(metadata['cueUpdate']['feedUrl'],'https://example.invalid/feed')
        with self.assertRaisesRegex(ValueError,'HTTPS'):
            update.stage_manager(root,metadata,target=self.target,runtime_source=Path(runtime.__file__),release_id='staged',feed_url='http://example.invalid/feed')

    def test_adopt_feed_requires_explicit_safe_https_source(self):
        for source in [None,'/tmp/feed.json','http://example.invalid/feed','https://user:secret@example.invalid/feed']:
            with self.subTest(source=source),patch.object(update,'selection') as select:
                with self.assertRaisesRegex(ValueError,'HTTPS'):update.update(self.installation,source,adopt_feed=True)
                select.assert_not_called()

    def test_adopt_feed_current_does_not_change_metadata(self):
        before=self.installation.read_bytes();selected=update.selection(self.installation)
        with patch.object(update,'selection',return_value=selected):
            result=update.update(self.installation,'https://example.invalid/feed',adopt_feed=True)
        self.assertEqual(result['state'],'current');self.assertFalse(result['feedAdopted'])
        self.assertEqual(self.installation.read_bytes(),before)

    def test_adopt_feed_update_rollback_and_retry(self):
        source=self.feed();selected=update.selection(self.installation,source);url='https://approved.example.invalid/feed';before=self.installation.read_bytes()
        with patch.object(update,'selection',return_value=selected):
            first=update.update(self.installation,url,adopt_feed=True)
        self.assertTrue(first['feedAdopted']);new=Path(first['installation'])
        self.assertEqual(json.loads(new.read_text())['cueUpdate']['feedUrl'],url)
        update.rollback(new,Path(first['receipt']));self.assertEqual(self.installation.read_bytes(),before)
        with patch.object(update,'selection',return_value=selected):
            second=update.update(self.installation,url,adopt_feed=True)
        self.assertNotEqual(first['receipt'],second['receipt'])
        self.assertEqual(json.loads(Path(second['installation']).read_text())['cueUpdate']['feedUrl'],url)

    def test_failed_adoption_leaves_prior_feed_unchanged(self):
        self.configure_feed();before=self.installation.read_bytes();source=self.feed();selected=update.selection(self.installation,source)
        archive=Path(selected[1]['archive']['location']);archive.write_bytes(b'corrupt')
        with patch.object(update,'selection',return_value=selected):
            with self.assertRaisesRegex(ValueError,'integrity'):update.update(self.installation,'https://new.example.invalid/feed',adopt_feed=True)
        self.assertEqual(self.installation.read_bytes(),before);self.assertEqual(self.target.resolve(),self.current/'bin/codex')

    def test_cli_adopt_feed_only_allowed_for_update(self):
        with patch.object(update,'selection') as select,patch('sys.stderr',new=io.StringIO()):
            code=update.main(['check','--installation',str(self.installation),'--source','https://example.invalid/feed','--adopt-feed'])
        self.assertEqual(code,1);select.assert_not_called()

    def test_cli_passes_explicit_adopt_flag(self):
        with patch.object(update,'update',return_value={'state':'current','feedAdopted':False}) as apply,patch('sys.stdout',new=io.StringIO()):
            code=update.main(['update','--installation',str(self.installation),'--source','https://example.invalid/feed','--adopt-feed'])
        self.assertEqual(code,0);apply.assert_called_once_with(self.installation,'https://example.invalid/feed',adopt_feed=True)

    def test_patch_only_update_replaces_complete_package_and_rolls_back(self):
        source=self.feed();self.assertEqual(update.selection(self.installation,source)[0]['state'],'update_available')
        result=update.update(self.installation,source);self.assertEqual(result['state'],'installed');new=Path(result['installation']).parent
        self.assertEqual(self.target.resolve(),new/'bin/codex');self.assertEqual((self.bin/'codex-code-mode-host').resolve(),new/'bin/codex-code-mode-host');self.assertTrue((new/'codex-path/rg').is_file())
        self.assertEqual(self.config.read_text(),'user-setting = true\n');self.assertEqual(update.selection(new/'codex-package.json')[0]['state'],'current')
        runtime.rollback_install(Path(result['receipt']));self.assertEqual(self.target.resolve(),self.current/'bin/codex');self.assertFalse(new.exists())

    def test_wrong_platform_is_no_compatible_update(self):
        self.assertEqual(update.update(self.installation,self.feed(target='x86_64-unknown-linux-musl'))['state'],'no_compatible_update');self.assertEqual(self.target.resolve(),self.current/'bin/codex')

    def test_unvalidated_release_is_not_compatible(self):
        source=self.feed();d=json.loads(source.read_text());d['releases'][0]['compatibility']['validated']=False;source.write_text(json.dumps(d));self.assertEqual(update.selection(self.installation,source)[0]['state'],'no_compatible_update')

    def test_archive_hash_mismatch_preserves_targets(self):
        source=self.feed();(self.root/'release.zip').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'integrity'):update.update(self.installation,source)
        self.assertEqual(self.target.resolve(),self.current/'bin/codex')

    def test_missing_marker_rejects_before_replacement(self):
        source=self.feed(markers=False)
        with self.assertRaisesRegex(ValueError,'required native patch'):update.update(self.installation,source)
        self.assertEqual(self.target.resolve(),self.current/'bin/codex')

    def test_missing_companion_rejects(self):
        source=self.feed(mutate=lambda p:(p/'bin/codex-code-mode-host').unlink())
        with self.assertRaisesRegex(ValueError,'Incomplete candidate'):update.update(self.installation,source)

    def test_updater_tamper_fails_closed(self):
        (self.current/'codex-resources/cue-update').write_text('changed')
        with self.assertRaisesRegex(ValueError,'asset changed'):update.selection(self.installation)

    def test_installed_target_drift_rejects(self):
        source=self.feed();self.target.unlink();self.target.write_text('different owner')
        with self.assertRaisesRegex(ValueError,'target drift'):update.update(self.installation,source)
        self.assertEqual(self.target.read_text(),'different owner')

    def test_conflicting_sequence_rejects(self):
        source=self.feed(sequence=1);d=json.loads(source.read_text());d['releases'][0]['releaseId']='conflict';source.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'Conflicting'):update.selection(self.installation,source)

    def test_archive_paths_and_symlinks_reject(self):
        for name,mode in [('../escape',0o100644),('symlink',stat.S_IFLNK|0o777),('/absolute',0o100644)]:
            with self.subTest(name=name):
                archive=self.root/'unsafe.zip'
                with zipfile.ZipFile(archive,'w') as z:
                    info=zipfile.ZipInfo(name);info.external_attr=mode<<16;z.writestr(info,'x')
                with tempfile.TemporaryDirectory(dir=self.root) as temp:
                    with self.assertRaises(ValueError):update._extract(archive,Path(temp))

    def test_http_feed_rejected(self):
        with self.assertRaisesRegex(ValueError,'HTTPS'):update.selection(self.installation,'http://example.test/feed.json')

    def test_https_feed_readonly_probe(self):
        data=self.feed().read_bytes()
        class Response(io.BytesIO):
            def geturl(self):return 'https://example.test/releases.json'
        with patch.object(update.urllib.request,'urlopen',return_value=Response(data)) as fetch:result=update.selection(self.installation,'https://example.test/releases.json')[0]
        self.assertEqual(result['state'],'update_available');fetch.assert_called_once()

    def test_concurrent_update_and_rollback_reject_held_lock(self):
        source=self.feed();receipt=self.root/'receipt.json';receipt.write_text(json.dumps({'target':str(self.target)}))
        with update.installation_lock(self.target):
            with self.assertRaisesRegex(ValueError,'installation lock'):update.update(self.installation,source)
            with self.assertRaisesRegex(ValueError,'installation lock'):update.rollback(self.installation,receipt)
            with self.assertRaisesRegex(ValueError,'installation lock'):runtime.rollback_install(receipt)
        self.assertEqual(self.target.resolve(),self.current/'bin/codex')

    def test_process_death_releases_advisory_lock(self):
        import subprocess,sys
        script="from converter.managed_update import installation_lock; import sys,time; from pathlib import Path\nwith installation_lock(Path(sys.argv[1])):\n print('locked',flush=True)\n time.sleep(60)\n"
        process=subprocess.Popen([sys.executable,'-c',script,str(self.target)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            with self.assertRaisesRegex(ValueError,'installation lock'):
                with update.installation_lock(self.target):pass
            process.kill();process.wait(timeout=10)
            with update.installation_lock(self.target):pass
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=10)
            process.stdout.close();process.stderr.close()

    def test_lock_symlink_is_rejected_without_touching_target(self):
        other=self.root/'not-a-lock';other.write_text('preserve')
        (self.bin/'.cue-codex-update.lock').symlink_to(other)
        with self.assertRaises(OSError):
            with update.installation_lock(self.target):pass
        self.assertEqual(other.read_text(),'preserve')

    def test_partial_link_failure_restores_complete_prior_package(self):
        source=self.feed();real=runtime._atomic_symlink_install
        def fail_main(link,target):
            if target.name=='codex':raise OSError('injected main replacement failure')
            return real(link,target)
        with patch.object(update,'_runtime',return_value=runtime), patch.object(runtime,'_atomic_symlink_install',side_effect=fail_main):
            with self.assertRaisesRegex(OSError,'injected'):update.update(self.installation,source)
        self.assertEqual(self.target.resolve(),self.current/'bin/codex')
        self.assertEqual((self.bin/'codex-code-mode-host').resolve(),self.current/'bin/codex-code-mode-host')
        receipts=list(self.bin.glob('*-receipt-*.json'));self.assertEqual(len(receipts),1)
        self.assertEqual(json.loads(receipts[0].read_text())['state'],'failed-rolled-back')

    def test_restore_failure_retains_truthful_receipt_and_recoverable_packages(self):
        source=self.feed();real=runtime._atomic_symlink_install
        def fail_main(link,target):
            if target.name=='codex':raise OSError('injected main replacement failure')
            return real(link,target)
        with patch.object(update,'_runtime',return_value=runtime), patch.object(runtime,'_atomic_symlink_install',side_effect=fail_main), patch.object(runtime,'_restore_target',side_effect=OSError('injected restore failure')):
            with self.assertRaisesRegex(RuntimeError,'rollback incomplete'):update.update(self.installation,source)
        receipt=list(self.bin.glob('*-receipt-*.json'))[0];record=json.loads(receipt.read_text())
        self.assertEqual(record['state'],'failed')
        self.assertIn('injected main replacement failure',record['error'])
        self.assertEqual(record['rollback_errors'],['injected restore failure'])
        self.assertTrue(self.current.is_dir());self.assertTrue(Path(record['runtime_dir']).is_dir())
        runtime.rollback_install(receipt)
        self.assertEqual((self.bin/'codex-code-mode-host').resolve(),self.current/'bin/codex-code-mode-host')

    def test_updater_launch_from_unrelated_directory_is_current(self):
        import subprocess,sys
        result=subprocess.run([sys.executable,str(self.current/'codex-resources/cue-update'),'check','--installation',str(self.installation)],cwd='/',capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'],'current')

    def test_update_rollback_retry_preserves_distinct_receipts(self):
        source=self.feed();first=update.update(self.installation,source)
        runtime.rollback_install(Path(first['receipt']))
        second=update.update(self.installation,source)
        self.assertEqual(second['state'],'installed')
        self.assertNotEqual(first['receipt'],second['receipt'])
        self.assertEqual(json.loads(Path(first['receipt']).read_text())['state'],'rolled-back')
        self.assertEqual(json.loads(Path(second['receipt']).read_text())['state'],'installed')

    def test_missing_previous_package_blocks_rollback_before_mutation(self):
        result=update.update(self.installation,self.feed());new=Path(result['installation']).parent
        import shutil
        shutil.rmtree(self.current)
        with self.assertRaisesRegex(ValueError,'Previous runtime package'):runtime.rollback_install(Path(result['receipt']))
        self.assertEqual(self.target.resolve(),new/'bin/codex');self.assertTrue(new.is_dir())
        self.assertEqual(json.loads(Path(result['receipt']).read_text())['state'],'installed')

    def test_tampered_previous_package_blocks_rollback_before_mutation(self):
        result=update.update(self.installation,self.feed());new=Path(result['installation']).parent
        (self.current/'bin/codex').write_text('changed old binary')
        with self.assertRaisesRegex(ValueError,'Previous runtime package'):runtime.rollback_install(Path(result['receipt']))
        self.assertEqual(self.target.resolve(),new/'bin/codex');self.assertTrue(new.is_dir())

    def test_rollback_refuses_later_package_edits(self):
        result=update.update(self.installation,self.feed());new=Path(result['installation']).parent;(new/'codex-path/rg').write_text('changed')
        with self.assertRaisesRegex(ValueError,'package changed'):runtime.rollback_install(Path(result['receipt']))
        self.assertEqual(self.target.resolve(),new/'bin/codex')

if __name__=='__main__':unittest.main()
