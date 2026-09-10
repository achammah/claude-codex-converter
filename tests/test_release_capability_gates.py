"""Synthetic validator fixtures only: these are not actual native execution proof."""
import copy
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from converter import release_pipeline as p, managed_update, native_runtime
import test_release_pipeline as baseline
from test_release_runner import runner


def synthetic_reports(binary_sha):
    cases = []
    for tool, decision, sandbox, permission, reviewer, outcome in sorted(p.consent_cases()):
        denied = decision == 'never' or (sandbox == 'workspace-write' and reviewer == 'auto_review' and outcome == 'deny')
        reviewed = sandbox == 'workspace-write' and reviewer == 'auto_review' and decision != 'never'
        cases.append(dict(passed=True, tool=tool, decision=decision, sandbox=sandbox,
            permission_hook=permission, reviewer=reviewer, reviewer_outcome=outcome,
            hook_observed=True, native_ask_attested=True, finished=True,
            approval_visible=not denied, no_execution_before_approval=not denied,
            executed=decision == 'approve' and not denied, reviewer_requests=int(reviewed),
            reviewer_before_human_ui=reviewed and outcome == 'allow',
            permission_hook_observed=permission == 'allow' and decision != 'never'))
    return {
        'hook_ask': dict(passed=True, binary_sha256=binary_sha, cases=cases),
        'hook_deadlines': dict(passed=True, binary_sha256=binary_sha, cases=[
            dict(passed=True, configured_seconds=timeout, delay_seconds=delay,
                 hook_observed=True, hook_completed=timeout != 1, timeout_reported=timeout == 1,
                 finished=True, model_requests=2) for timeout, delay in ((1, 3), (130, 123))]),
        'resource_capacity': dict(passed=True, binary_sha256=binary_sha, initial_soft=256,
            initial_hard=4096, status_rendered=True, provider=dict(opened=700, soft=1024, hard=4096, error=None))}


class CapabilityGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = baseline.ReleasePipelineTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.tmp.cleanup)
        f = self.fixture
        binary = f.pkg/'bin/codex'
        binary.write_text(binary.read_text()+' CUE_FD_CAPACITY_V1 CUE_HOOK_ASK_V1')
        managed_update.stage_manager(f.pkg, f.meta, target=f.root/'visible/codex',
            runtime_source=Path(native_runtime.__file__), release_id='test-release', sequence=2)
        p.write(f.pkg/'codex-package.json', f.meta)
        f.evidence['binding']['inventorySha256'] = p.sha(p.canonical(p.inventory(f.pkg)))
        self.binary_sha = p.sha(binary.read_bytes())
        reports = synthetic_reports(self.binary_sha)
        self.subchecks = {name: dict(passed=True, binding=copy.deepcopy(f.evidence['binding']),
            binarySha256=self.binary_sha, report=report, reportSha256=p.sha(p.canonical(report)),
            logSha256='a'*64, scriptSha256='b'*64) for name, report in reports.items()}
        f.evidence['checks']['native_tests']['subchecks'] = self.subchecks

    def rehash(self, name):
        self.subchecks[name]['reportSha256'] = p.sha(p.canonical(self.subchecks[name]['report']))

    def test_complete_synthetic_validator_fixture_and_feed(self):
        row = self.fixture.pack()
        self.assertIn('CUE_HOOK_ASK_V1', row['compatibility']['markers'])
        hosted = {row['archive']['location']: dict(sha256=row['archive']['sha256'],
            evidenceSha256=row['evidenceSha256'], verified=True)}
        self.assertEqual(p.advance(self.fixture.root/'feed.json', [row], hosted)['releases'][0], row)

    def test_missing_each_capability_check_rejects(self):
        for name in list(self.subchecks):
            with self.subTest(name=name):
                original = self.subchecks.pop(name)
                with self.assertRaisesRegex(ValueError, 'capability evidence'): self.fixture.pack()
                self.subchecks[name] = original

    def test_cannot_remove_marker_metadata_to_evade_gate(self):
        f = self.fixture
        f.meta['cueUpdate']['requiredMarkers'] = list(managed_update.REQUIRED_MARKERS)
        p.write(f.pkg/'codex-package.json', f.meta)
        f.evidence['binding']['inventorySha256'] = p.sha(p.canonical(p.inventory(f.pkg)))
        self.subchecks.clear()
        with self.assertRaisesRegex(ValueError, 'capability evidence'): f.pack()

    def test_all_binding_fields_and_binary_hash_are_enforced(self):
        item = self.subchecks['hook_ask']
        for key in item['binding']:
            with self.subTest(key=key):
                original = item['binding'][key]; item['binding'][key] = 'wrong'
                with self.assertRaisesRegex(ValueError, 'unbound'): self.fixture.pack()
                item['binding'][key] = original
        item['binarySha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'unbound'): self.fixture.pack()

    def test_stale_report_digest_rejects(self):
        self.subchecks['hook_ask']['report']['cases'][0]['executed'] = not self.subchecks['hook_ask']['report']['cases'][0]['executed']
        with self.assertRaisesRegex(ValueError, 'unbound'): self.fixture.pack()

    def test_incomplete_duplicate_empty_and_failed_consent_cases_reject(self):
        report = self.subchecks['hook_ask']['report']; original = copy.deepcopy(report['cases'])
        for cases in ([], original[:-1], original[:-1]+[original[0]]):
            report['cases'] = cases; self.rehash('hook_ask')
            with self.assertRaisesRegex(ValueError, 'coverage'): self.fixture.pack()
        report['cases'] = original; report['cases'][0]['passed'] = False; self.rehash('hook_ask')
        with self.assertRaisesRegex(ValueError, 'observation'): self.fixture.pack()

    def test_constrained_reviewer_denial_must_be_observed(self):
        case = next(c for c in self.subchecks['hook_ask']['report']['cases']
            if c['sandbox']=='workspace-write' and c['reviewer']=='auto_review'
            and c['reviewer_outcome']=='deny' and c['decision']=='approve')
        case['reviewer_requests'] = 0; self.rehash('hook_ask')
        with self.assertRaisesRegex(ValueError, 'reviewer was not'): self.fixture.pack()

    def test_cancel_cannot_execute(self):
        case = next(c for c in self.subchecks['hook_ask']['report']['cases'] if c['decision']=='cancel')
        case['executed'] = True; self.rehash('hook_ask')
        with self.assertRaisesRegex(ValueError, 'consent ordering'): self.fixture.pack()

    def test_deadline_pair_and_observations_enforced(self):
        report = self.subchecks['hook_deadlines']['report']; original=copy.deepcopy(report['cases'])
        for cases in ([], original[:1], [original[0], dict(original[1], delay_seconds=119)],
                      [original[0], dict(original[1], hook_completed=False)]):
            report['cases'] = cases; self.rehash('hook_deadlines')
            with self.assertRaisesRegex(ValueError, 'deadline'): self.fixture.pack()

    def test_resource_capacity_hard_limit_preserved(self):
        self.subchecks['resource_capacity']['report']['provider']['hard'] = 8192
        self.rehash('resource_capacity')
        with self.assertRaisesRegex(ValueError, 'capacity'): self.fixture.pack()

    def test_embedded_report_binary_hash_must_match(self):
        self.subchecks['hook_deadlines']['report']['binary_sha256'] = '0'*64
        self.rehash('hook_deadlines')
        with self.assertRaisesRegex(ValueError, 'unbound'): self.fixture.pack()

    def test_never_and_permission_hook_observations_cannot_be_omitted(self):
        report = self.subchecks['hook_ask']['report']
        case = next(c for c in report['cases'] if c['decision']=='never')
        case['executed'] = True; self.rehash('hook_ask')
        with self.assertRaisesRegex(ValueError, 'Denied'): self.fixture.pack()
        case['executed'] = False
        case = next(c for c in report['cases'] if c['permission_hook']=='allow' and c['decision']=='approve')
        case['permission_hook_observed'] = False; self.rehash('hook_ask')
        with self.assertRaisesRegex(ValueError, 'PermissionRequest'): self.fixture.pack()

    def test_unknown_capability_cannot_be_advertised_without_policy(self):
        with self.assertRaisesRegex(ValueError, 'No release verification policy'):
            p.capability_markers({'requiredMarkers':['FUTURE_UNTESTED_CAPABILITY']}, b'')

    def test_reviewed_capability_remains_required(self):
        f = self.fixture
        f.proof['requiredCapabilities'] = ['CUE_HOOK_ASK_V1']
        f.evidence['binding']['patchProofSha256'] = p.sha(p.canonical(f.proof))
        for item in self.subchecks.values(): item['binding'] = copy.deepcopy(f.evidence['binding'])
        self.subchecks.pop('hook_ask')
        with self.assertRaisesRegex(ValueError, 'capability evidence'): f.pack()

    def test_runner_rejects_wrong_candidate_report_before_binding(self):
        with self.assertRaisesRegex(ValueError, 'candidate'):
            runner.bound_capability('hook_ask', dict(passed=True, binary_sha256='0'*64), [],
                self.fixture.evidence['binding'], self.binary_sha)

    def test_runner_dispatches_full_matrix_and_deadlines(self):
        work=self.fixture.root/'work'; work.mkdir(); reports=synthetic_reports(self.binary_sha)
        synthetic_root=self.fixture.root/'synthetic-runner'
        scripts=synthetic_root/'tests/native_smoke'; scripts.mkdir(parents=True)
        for filename in p.CAPABILITY_SCRIPTS.values():
            (scripts/filename).write_text('# Synthetic orchestration fixture; no native behavior.\n')
        invocations=[]
        def fake_smoke(name, argv, root, env=None):
            invocations.append(argv)
            if name.startswith('hook_ask-'):
                get=lambda flag: argv[argv.index(flag)+1]
                cases=[c for c in reports['hook_ask']['cases'] if c['sandbox']==get('--sandbox')
                    and c['permission_hook']==get('--permission-hook') and c['reviewer']==get('--reviewer')
                    and c['reviewer_outcome']==get('--reviewer-outcome')]
                report=dict(passed=True,binary_sha256=self.binary_sha,cases=cases)
            else: report=reports[name]
            p.write(root/name/'report.json', report)
            return dict(passed=True,logSha256='a'*64)
        with patch.object(runner,'ROOT',synthetic_root), patch.object(runner,'smoke',side_effect=fake_smoke):
            result=runner.run_capabilities(['CUE_HOOK_ASK_V1','CUE_FD_CAPACITY_V1'], self.fixture.pkg,
                work, {}, self.fixture.evidence['binding'], self.binary_sha)
        self.assertEqual(len(invocations),14)
        self.assertEqual(len(result['hook_ask']['report']['cases']),108)
        self.assertEqual(set(result),{'hook_ask','hook_deadlines','resource_capacity'})

    def test_missing_required_script_rejects_actual_runner_dispatch(self):
        work=self.fixture.root/'missing-script-work'; work.mkdir()
        with patch.object(runner,'ROOT',self.fixture.root):
            with self.assertRaises(subprocess.CalledProcessError):
                runner.run_capabilities(['CUE_HOOK_ASK_V1'], self.fixture.pkg,
                    work, {}, self.fixture.evidence['binding'], self.binary_sha)
        self.assertIn('hook_ask.py', (work/'hook_ask-0.log').read_text())
        self.assertFalse((work/'hook_ask-0/report.json').exists())


if __name__=='__main__': unittest.main()
