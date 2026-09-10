"""Publication failures must not promote a release or hide missing targets."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from converter import release_pipeline as pipeline


def load(name):
    path=Path(__file__).resolve().parents[1]/'scripts'/(name+'.py')
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class CIReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.work=self.root/'work';self.work.mkdir();self.artifacts=self.root/'artifacts';self.artifacts.mkdir()
        self.ci=load('ci_release');self.repo='fixture/runtime-releases'
        owned=patch.object(self.ci,'existing_release',return_value=None);owned.start();self.addCleanup(owned.stop)
        self.target='aarch64-apple-darwin';self.base='https://github.com/'+self.repo+'/releases/download/fixture-release'
        self.context={'repository':self.repo,'releaseId':'fixture-release','sequence':4,'targets':sorted(self.ci.RUNNERS),
                      'sourceCommit':'a'*40,'baseUrl':self.base,'feedUrl':'https://github.com/'+self.repo+'/releases/latest/download/compatible-releases.json'}
        pipeline.write(self.work/'context.json',self.context)
        pipeline.write(self.work/'prior-feed.json',{'schemaVersion':1,'manager':pipeline.managed_update.MANAGER,'releases':[]})

    def artifact(self, target=None):
        if target is None:
            results={t:self.artifact(t) for t in self.ci.RUNNERS}
            return results[self.target]
        directory=self.artifacts/('runtime-'+target);directory.mkdir()
        archive=directory/('runtime-'+target+'.zip');archive.write_bytes(b'fixture archive')
        evidence={'fixture':'evidence'};pipeline.write(directory/'evidence.json',evidence)
        row={'target':target,'releaseId':'fixture-release-'+target,'sequence':4,'version':'0.154.0',
             'compatibility':{'validated':True,'markers':list(pipeline.managed_update.REQUIRED_MARKERS)},
             'archive':{'location':self.base+'/'+archive.name,'sha256':pipeline.sha(archive.read_bytes())},
             'evidenceSha256':pipeline.sha(pipeline.canonical(evidence))}
        pipeline.write(directory/'release.json',row);return directory,row

    def test_missing_target_prevents_any_publication(self):
        self.artifact();(self.artifacts/'runtime-x86_64-apple-darwin/release.json').unlink()
        with patch.object(self.ci,'gh') as gh:
            with self.assertRaises(FileNotFoundError):self.ci.publish(self.work,self.artifacts,self.repo)
        gh.assert_not_called()

    def test_archive_digest_mismatch_prevents_any_publication(self):
        directory,row=self.artifact();(directory/('runtime-'+self.target+'.zip')).write_bytes(b'changed')
        with patch.object(self.ci,'gh') as gh:
            with self.assertRaisesRegex(ValueError,'digest'):self.ci.publish(self.work,self.artifacts,self.repo)
        gh.assert_not_called()

    def test_evidence_digest_mismatch_prevents_any_publication(self):
        directory,row=self.artifact();pipeline.write(directory/'evidence.json',{'changed':True})
        with patch.object(self.ci,'gh') as gh:
            with self.assertRaisesRegex(ValueError,'digest'):self.ci.publish(self.work,self.artifacts,self.repo)
        gh.assert_not_called()

    def test_hosted_mismatch_never_promotes(self):
        self.artifact()
        with patch.object(self.ci,'gh') as gh,patch.object(self.ci,'hosted_digest',return_value='0'*64):
            with self.assertRaisesRegex(ValueError,'Hosted'):self.ci.publish(self.work,self.artifacts,self.repo)
        calls=[c.args for c in gh.call_args_list]
        self.assertEqual(calls[0][:2],('release','create'));self.assertIn('--prerelease',calls[0])
        self.assertFalse(any(c[:2]==('release','edit') for c in calls))

    def test_success_promotes_only_verified_feed_and_pins_source_commit(self):
        directory,row=self.artifact()
        def fetched(url):
            if '/evidence-' in url:return pipeline.canonical({'fixture':'evidence'})
            return (self.work/'compatible-releases.json').read_bytes()
        with patch.object(self.ci,'gh') as gh,patch.object(self.ci,'hosted_digest',return_value=row['archive']['sha256']),patch.object(self.ci,'fetch',side_effect=fetched),contextlib.redirect_stdout(io.StringIO()):
            self.ci.publish(self.work,self.artifacts,self.repo)
        calls=[call.args for call in gh.call_args_list]
        self.assertEqual(calls[0][1],'create');self.assertEqual(calls[-1][1],'edit')
        self.assertEqual(sum(c[1]=='upload' for c in calls),9)
        self.assertIn('--target',calls[0]);self.assertEqual(calls[0][calls[0].index('--target')+1],'a'*40)
        self.assertIn('--latest',calls[-1])

    def test_retry_resumes_prerelease_after_transient_verification_failure(self):
        directory,row=self.artifact()
        with patch.object(self.ci,'gh') as gh,patch.object(self.ci,'hosted_digest',side_effect=OSError('temporary download failure')):
            with self.assertRaisesRegex(OSError,'temporary'):self.ci.publish(self.work,self.artifacts,self.repo)
        self.assertFalse(any(c.args[:2]==('release','edit') for c in gh.call_args_list))
        assets=[]
        for target in self.ci.RUNNERS:assets.extend([{'name':'runtime-'+target+'.zip'},{'name':'evidence-'+target+'.json'}])
        existing={'target_commitish':'a'*40,'draft':False,'prerelease':True,'assets':assets}
        def digest(url):return row['evidenceSha256'] if '/evidence-' in url else row['archive']['sha256']
        def fetch(url):return pipeline.canonical({'fixture':'evidence'}) if '/evidence-' in url else (self.work/'compatible-releases.json').read_bytes()
        with patch.object(self.ci,'existing_release',return_value=existing),patch.object(self.ci,'gh') as gh,patch.object(self.ci,'hosted_digest',side_effect=digest),patch.object(self.ci,'fetch',side_effect=fetch),contextlib.redirect_stdout(io.StringIO()):
            self.ci.publish(self.work,self.artifacts,self.repo)
        self.assertEqual([c.args[1] for c in gh.call_args_list],['upload','edit'])

    def test_existing_mismatched_asset_never_overwritten_or_promoted(self):
        self.artifact();existing={'target_commitish':'a'*40,'draft':False,'assets':[{'name':'runtime-'+self.target+'.zip'}]}
        with patch.object(self.ci,'existing_release',return_value=existing),patch.object(self.ci,'gh') as gh,patch.object(self.ci,'hosted_digest',return_value='0'*64):
            with self.assertRaisesRegex(ValueError,'Existing release asset differs'):self.ci.publish(self.work,self.artifacts,self.repo)
        gh.assert_not_called()

    def discovery_fixture(self, preparation=False):
        root=self.root/'source';native=root/'native';native.mkdir(parents=True)
        entries=[]
        for i,marker in enumerate(pipeline.managed_update.REQUIRED_MARKERS):
            name=str(i)+'.patch';data=('patch '+str(i)).encode();(native/name).write_bytes(data)
            entries.append({'patch_file':name,'patch_sha256':pipeline.sha(data),'feature_marker':marker})
        manifest={'upstream_commit':'b'*40,'release_sequence':9,**entries[0],'additional_patches':entries[1:]}
        if preparation:
            data=Path(pipeline.normalize_workspace_lock.__file__).read_bytes();(native/'normalize_workspace_lock.py').write_bytes(data)
            manifest['sourcePreparation']={'program':'normalize_workspace_lock.py','sha256':pipeline.sha(data)}
        pipeline.write(native/'manifest.json',manifest)
        metadata={'tag_name':'rust-v0.154.0','html_url':'https://github.com/openai/codex/releases/tag/rust-v0.154.0'}
        prior={'schemaVersion':1,'manager':pipeline.managed_update.MANAGER,'releases':[{'releaseId':'older','target':self.target,'sequence':3}]}
        return root,metadata,prior

    def discover(self,root,metadata,prior):
        with patch.object(self.ci,'api',side_effect=[metadata,{'object':{'type':'commit','sha':'b'*40}}]),patch.object(self.ci,'fetch',return_value=pipeline.canonical(prior)),patch.object(self.ci,'outputs'),patch.object(self.ci.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'a'*40+'\n','')),contextlib.redirect_stdout(io.StringIO()):
            self.ci.discover(root,self.work,self.repo)

    def test_discovery_sequence_uses_manifest_floor(self):
        root,metadata,prior=self.discovery_fixture();self.discover(root,metadata,prior)
        self.assertEqual(pipeline.read(self.work/'context.json')['sequence'],10)

    def test_build_attempt_changes_release_identity_without_changing_source(self):
        root,metadata,prior=self.discovery_fixture()
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}):self.discover(root,metadata,prior)
        first=pipeline.read(self.work/'context.json')['releaseId']
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'2'}):self.discover(root,metadata,prior)
        second=pipeline.read(self.work/'context.json')['releaseId']
        self.assertNotEqual(first,second);self.assertEqual(first.rsplit('-run-',1)[0],second.rsplit('-run-',1)[0])

    def test_published_same_source_attempt_suppresses_rebuild(self):
        root,metadata,prior=self.discovery_fixture()
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}):self.discover(root,metadata,prior)
        first=pipeline.read(self.work/'context.json')['releaseId']
        prior['releases']=[{'target':t,'releaseId':first+'-'+t,'sequence':10} for t in self.ci.RUNNERS]
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'456','GITHUB_RUN_ATTEMPT':'1'}):self.discover(root,metadata,prior)
        self.assertEqual(pipeline.read(self.work/'context.json')['state'],'current')
        (root/'converter').mkdir();(root/'converter/changed.py').write_text('changed = True\n')
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'789','GITHUB_RUN_ATTEMPT':'1'}):self.discover(root,metadata,prior)
        self.assertEqual(pipeline.read(self.work/'context.json')['state'],'candidate')

    def test_metadata_only_change_does_not_change_source_identity(self):
        root,metadata,prior=self.discovery_fixture();self.discover(root,metadata,prior)
        first=pipeline.read(self.work/'context.json')['releaseId']
        prior['releases']=[{'target':t,'releaseId':first+'-'+t,'sequence':10} for t in self.ci.RUNNERS]
        metadata['assets']=[{'download_count':987}];self.discover(root,metadata,prior)
        context=pipeline.read(self.work/'context.json');self.assertEqual(context['releaseId'],first);self.assertEqual(context['state'],'current')

    def test_reverting_source_after_newer_release_requires_new_build(self):
        root,metadata,prior=self.discovery_fixture();self.discover(root,metadata,prior)
        original=pipeline.read(self.work/'context.json')['releaseId']
        prior['releases']=[{'target':t,'releaseId':original+'-'+t,'sequence':10} for t in self.ci.RUNNERS]
        prior['releases'] += [{'target':t,'releaseId':'different-newer-'+t,'sequence':11} for t in self.ci.RUNNERS]
        self.discover(root,metadata,prior);context=pipeline.read(self.work/'context.json')
        self.assertEqual(context['state'],'candidate');self.assertEqual(context['sequence'],12)

    def test_release_id_suffix_cannot_substitute_actual_target(self):
        root,metadata,prior=self.discovery_fixture();self.discover(root,metadata,prior)
        original=pipeline.read(self.work/'context.json')['releaseId']
        prior['releases']=[{'target':'wrong-target','releaseId':original+'-'+t,'sequence':10} for t in self.ci.RUNNERS]
        self.discover(root,metadata,prior);self.assertEqual(pipeline.read(self.work/'context.json')['state'],'candidate')

    def test_discovery_records_exact_converter_source_commit(self):
        root,metadata,prior=self.discovery_fixture();self.discover(root,metadata,prior)
        self.assertEqual(pipeline.read(self.work/'context.json')['sourceCommit'],'a'*40)
        self.assertEqual(pipeline.read(self.work/'candidate.json')['commit'],'b'*40)

    def test_discovery_copies_pinned_source_preparation(self):
        root,metadata,prior=self.discovery_fixture(preparation=True);self.discover(root,metadata,prior)
        self.assertEqual((self.work/'native/normalize_workspace_lock.py').read_bytes(),(root/'native/normalize_workspace_lock.py').read_bytes())

    def test_discovery_rejects_changed_preparation(self):
        root,metadata,prior=self.discovery_fixture(preparation=True);(root/'native/normalize_workspace_lock.py').write_text('changed')
        with self.assertRaisesRegex(ValueError,'preparation|Preparation|Frozen'):
            self.discover(root,metadata,prior)

    def test_ci_package_rejects_unrequested_target_before_packaging(self):
        module=load('ci_package');result=self.root/'result';result.mkdir()
        pipeline.write(self.work/'candidate.json',{});pipeline.write(result/'evidence.json',{'binding':{'target':'unknown','releaseId':'fixture-release-unknown','sequence':4}})
        with patch('sys.argv',['ci_package','--work',str(self.work),'--result',str(result)]),patch.object(module.pipeline,'package') as package:
            with self.assertRaisesRegex(ValueError,'differs'):module.main()
        package.assert_not_called()


if __name__=='__main__':unittest.main()
