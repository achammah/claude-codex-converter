"""Required installed features survive newer version/sequence updates."""
import json
from pathlib import Path
import unittest
import test_managed_update as fixtures
from converter import managed_update as update

CAP = 'CUE_HOOK_ASK_V1'

class CapabilityUpdates(unittest.TestCase):
    setUp = fixtures.ManagedUpdateTests.setUp
    package = fixtures.ManagedUpdateTests.package
    feed = fixtures.ManagedUpdateTests.feed

    def require(self):
        p=self.installation;m=json.loads(p.read_text());m['cueUpdate']['requiredMarkers']=[CAP];p.write_text(json.dumps(m))
        binary=self.current/'bin/codex';binary.write_bytes(binary.read_bytes()+b'# CUE_HOOK_ASK_V1\n')

    def advertise(self, source):
        d=json.loads(source.read_text());d['releases'][0]['compatibility']['markers'].append(CAP);source.write_text(json.dumps(d))

    def candidate(self, root, marker=True, policy=True):
        p=root/'codex-package.json';m=json.loads(p.read_text());m['cueUpdate']['requiredMarkers']=[CAP]
        if not policy:m['cueUpdate'].pop('capabilityPolicyVersion',None)
        p.write_text(json.dumps(m))
        if marker:
            p=root/'bin/codex';p.write_bytes(p.read_bytes()+b'# CUE_HOOK_ASK_V1\n')

    def test_newer_release_without_installed_capability_is_ineligible(self):
        self.require();source=self.feed(sequence=5)
        self.assertEqual(update.update(self.installation,source)['state'],'no_compatible_update')
        self.assertEqual(self.target.resolve(),self.current/'bin/codex')

    def test_forged_advertisement_cannot_drop_metadata(self):
        self.require();source=self.feed();self.advertise(source)
        with self.assertRaisesRegex(ValueError,'drops required'):update.update(self.installation,source)
        self.assertEqual(self.target.resolve(),self.current/'bin/codex')

    def test_missing_binary_marker_rejected(self):
        self.require();source=self.feed(mutate=lambda r:self.candidate(r,marker=False));self.advertise(source)
        with self.assertRaisesRegex(ValueError,'required native patch'):update.update(self.installation,source)
        self.assertEqual(self.target.resolve(),self.current/'bin/codex')

    def test_legacy_policy_rejected(self):
        self.require();source=self.feed(mutate=lambda r:self.candidate(r,policy=False));self.advertise(source)
        with self.assertRaisesRegex(ValueError,'cannot preserve'):update.update(self.installation,source)

    def test_success_preserves_future_requirement_and_rollback(self):
        self.require();before=self.installation.read_bytes();source=self.feed(mutate=self.candidate);self.advertise(source)
        result=update.update(self.installation,source);new=Path(result['installation'])
        self.assertIn(CAP,update.required_markers(json.loads(new.read_text())['cueUpdate']))
        d=json.loads(source.read_text());d['releases'][0]['sequence']=5;d['releases'][0]['compatibility']['markers'].remove(CAP);source.write_text(json.dumps(d))
        self.assertEqual(update.selection(new,source)[0]['state'],'no_compatible_update')
        update.rollback(new,Path(result['receipt']));self.assertEqual(self.installation.read_bytes(),before)

    def test_legacy_installed_fd_marker_is_preserved_without_metadata(self):
        p=self.current/'bin/codex';p.write_bytes(p.read_bytes()+b'# CUE_FD_CAPACITY_V1\n')
        before=self.installation.read_bytes();source=self.feed(sequence=5)
        self.assertEqual(update.selection(self.installation,source)[0]['state'],'no_compatible_update')
        self.assertEqual(self.installation.read_bytes(),before)

    def test_restaging_keeps_previously_declared_capabilities(self):
        self.require();meta=json.loads(self.installation.read_text())
        update.stage_manager(self.current,meta,target=self.target,runtime_source=Path(update.__file__).with_name('native_runtime.py'),release_id='restaged',sequence=2)
        self.assertIn(CAP,meta['cueUpdate']['requiredMarkers'])
        bundled=json.loads((self.current/'codex-resources/compatible-releases.json').read_text())
        self.assertIn(CAP,bundled['releases'][0]['compatibility']['markers'])
        self.assertEqual(bundled['releases'][0]['compatibility']['capabilityPolicyVersion'],1)

    def test_invalid_required_capability_metadata_rejected(self):
        for value in ['text',[True],[''],['a/b']]:
            with self.subTest(value=value),self.assertRaises(ValueError):update.required_markers({'requiredMarkers':value})

if __name__=='__main__':unittest.main()
