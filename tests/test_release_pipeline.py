import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from converter import managed_update, native_runtime, release_pipeline as p


class ReleasePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.pkg=self.root/'package'
        for rel,data in [('bin/codex',' '.join(managed_update.REQUIRED_MARKERS)),('bin/codex-code-mode-host','helper'),('codex-path/rg','rg')]:
            f=self.pkg/rel;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(data);f.chmod(0o755)
        self.meta={'version':'0.153.4','target':'aarch64-apple-darwin'}
        managed_update.stage_manager(self.pkg,self.meta,target=self.root/'visible/codex',runtime_source=Path(native_runtime.__file__),release_id='test-release',sequence=2)
        p.write(self.pkg/'codex-package.json',self.meta)
        self.candidate=p.discover({'tag_name':'rust-v0.153.4','html_url':'https://github.com/openai/codex/releases/tag/rust-v0.153.4'},'a'*40)
        self.proof={'passed':True,'commit':'a'*40,'patches':[{'marker':m} for m in managed_update.REQUIRED_MARKERS]}
        self.evidence={'binding':{'candidateSha256':p.sha(p.canonical(self.candidate)),'patchProofSha256':p.sha(p.canonical(self.proof)),'inventorySha256':p.sha(p.canonical(p.inventory(self.pkg))),'target':self.meta['target'],'releaseId':'test-release','sequence':2},'checks':{k:{'passed':True,'logSha256':'b'*64} for k in p.CHECKS}}

    def pack(self,output='out'):
        return p.package(self.pkg,self.candidate,self.proof,self.evidence,self.root/output,'https://releases.example.invalid/runtime')

    def test_candidate_never_claims_compatibility(self):
        self.assertEqual(self.candidate['state'],'candidate');self.assertNotIn('compatibility',self.candidate)
        with self.assertRaises(ValueError):p.discover({'tag_name':'main'},'a'*40)

    def test_deterministic_package_and_feed(self):
        first=self.pack();second=self.pack('other');self.assertEqual(first,second)
        name='runtime-aarch64-apple-darwin-2.zip'
        self.assertEqual((self.root/'out'/name).read_bytes(),(self.root/'other'/name).read_bytes())
        hosted={first['archive']['location']:{'sha256':first['archive']['sha256'],'evidenceSha256':first['evidenceSha256'],'verified':True}}
        feed=self.root/'feed.json';p.advance(feed,[first],hosted);before=feed.read_bytes();p.advance(feed,[first],hosted);self.assertEqual(feed.read_bytes(),before)

    def test_failed_check_cannot_publish(self):
        self.evidence['checks']['question_ui']['passed']=False
        with self.assertRaisesRegex(ValueError,'checks'):self.pack()
        self.assertFalse((self.root/'out').exists())

    def test_changed_package_invalidates_evidence(self):
        (self.pkg/'codex-path/rg').write_text('changed')
        with self.assertRaisesRegex(ValueError,'evidence'):self.pack()

    def test_missing_companion_rejected(self):
        (self.pkg/'bin/codex-code-mode-host').unlink()
        with self.assertRaisesRegex(ValueError,'companions'):self.pack()

    def test_wrong_commit_proof_rejected(self):
        self.proof['commit']='c'*40
        with self.assertRaisesRegex(ValueError,'proof'):self.pack()

    def test_unverified_upload_preserves_feed(self):
        row=self.pack();feed=self.root/'feed.json';old={'schemaVersion':1,'manager':managed_update.MANAGER,'releases':[]};p.write(feed,old);before=feed.read_bytes()
        with self.assertRaisesRegex(ValueError,'Publication'):p.advance(feed,[row],{})
        self.assertEqual(feed.read_bytes(),before)

    def test_conflicting_sequence_preserves_feed(self):
        row=self.pack();hosted={row['archive']['location']:{'sha256':row['archive']['sha256'],'evidenceSha256':row['evidenceSha256'],'verified':True}};feed=self.root/'feed.json';p.advance(feed,[row],hosted);before=feed.read_bytes()
        changed=copy.deepcopy(row);changed['releaseId']='conflict'
        with self.assertRaisesRegex(ValueError,'sequence'):p.advance(feed,[changed],hosted)
        self.assertEqual(feed.read_bytes(),before)

    def test_http_publication_rejected(self):
        with self.assertRaisesRegex(ValueError,'HTTPS'):p.package(self.pkg,self.candidate,self.proof,self.evidence,self.root/'out','http://example.invalid')

    def test_symlink_package_rejected(self):
        (self.pkg/'extra').symlink_to(self.pkg/'bin/codex')
        with self.assertRaisesRegex(ValueError,'symlink'):self.pack()

    def test_actual_patch_application_and_hash_failure(self):
        repo=self.root/'repo';repo.mkdir()
        def git(*args):return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True,text=True).stdout.strip()
        git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture')
        (repo/'sample').write_text('one\n');git('add','sample');git('commit','-m','fixture');commit=git('rev-parse','HEAD')
        entries=[]
        for i,(old,new,marker) in enumerate(zip(['one','two','three'],['two','three','four'],managed_update.REQUIRED_MARKERS)):
            patch=f'--- a/sample\n+++ b/sample\n@@ -1 +1 @@\n-{old}\n+{new}\n'.encode();name=f'{i}.patch';(self.root/name).write_bytes(patch);entries.append({'patch_file':name,'patch_sha256':p.sha(patch),'feature_marker':marker})
        manifest={'upstream_commit':commit,**entries[0],'additional_patches':entries[1:]};path=self.root/'manifest.json';p.write(path,manifest)
        result=p.validate_patches(repo,path);self.assertTrue(result['passed']);self.assertEqual((repo/'sample').read_text(),'one\n')
        (self.root/'1.patch').write_text('corrupt')
        with self.assertRaisesRegex(ValueError,'digest'):p.validate_patches(repo,path)

    def test_normalization_runs_before_patches_and_requires_pinned_builtin(self):
        repo=self.root/'repo';repo.mkdir()
        def git(*args):return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True,text=True).stdout.strip()
        git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture')
        rust=repo/'codex-rs';(rust/'member').mkdir(parents=True)
        (rust/'Cargo.toml').write_text('[workspace]\nmembers=["member"]\n[workspace.package]\nversion="0.154.0"\n')
        (rust/'member/Cargo.toml').write_text('[package]\nname="sample"\nversion.workspace=true\n')
        (rust/'Cargo.lock').write_text('[[package]]\nname = "sample"\nversion = "0.0.0"\n')
        git('add','.');git('commit','-m','fixture');commit=git('rev-parse','HEAD')
        entries=[]
        for i,(old,new,marker) in enumerate(zip(['sample','second','third'],['second','third','fourth'],managed_update.REQUIRED_MARKERS)):
            data=f'--- a/codex-rs/Cargo.lock\n+++ b/codex-rs/Cargo.lock\n@@ -1,3 +1,3 @@\n [[package]]\n-name = "{old}"\n+name = "{new}"\n version = "0.154.0"\n'.encode();name=f'n{i}.patch';(self.root/name).write_bytes(data);entries.append({'patch_file':name,'patch_sha256':p.sha(data),'feature_marker':marker})
        normalizer=Path(p.normalize_workspace_lock.__file__).read_bytes();(self.root/'normalize_workspace_lock.py').write_bytes(normalizer)
        manifest={'upstream_commit':commit,**entries[0],'additional_patches':entries[1:],'sourcePreparation':{'program':'normalize_workspace_lock.py','sha256':p.sha(normalizer)}};path=self.root/'normalize-manifest.json';p.write(path,manifest)
        proof=p.validate_patches(repo,path);self.assertEqual(proof['sourcePreparation']['count'],1)
        self.assertIn('0.0.0',(rust/'Cargo.lock').read_text())
        manifest['sourcePreparation']['sha256']='0'*64;p.write(path,manifest)
        with self.assertRaisesRegex(ValueError,'digest'):p.validate_patches(repo,path)


if __name__=='__main__':unittest.main()
