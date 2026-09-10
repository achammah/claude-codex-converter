"""Synthetic installer and fake app-server regressions. No native writes occur."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CONVERTER_DIR = Path(__file__).resolve().parents[1] / 'converter'


def load_module(name):
    spec = importlib.util.spec_from_file_location('delivery_' + name, CONVERTER_DIR / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProtocolRegressions(unittest.TestCase):
    def test_native_command_response_text_survives_json_parsing(self):
        protocol = load_module('protocol')
        row_id = '53853f58-3556-4cbd-8265-1537a32e2888'
        event = {
            'hook_event_name': 'PostToolUse',
            'tool_name': 'exec_command',
            'tool_input': {'cmd': 'nexus tracks task get ' + row_id + ' --json'},
            'tool_response': json.dumps({'id': row_id}),
        }
        normalized = protocol.normalize(event)
        self.assertEqual(normalized['tool_name'], 'Bash')
        self.assertIn(row_id, normalized['tool_response']['stdout'])
        event['tool_response'] = {'exit_code': 0, 'content': [
            {'type': 'input_text', 'text': json.dumps({'id': row_id})},
        ]}
        normalized = protocol.normalize(event)
        self.assertIn(row_id, normalized['tool_response']['stdout'])


class InstallerRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cue-install-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.stage = self.base / 'staging/stage'
        self.target = self.base / 'live/target'
        self.stage.mkdir(parents=True)
        self.target.mkdir(parents=True)
        self.plan = self.base / 'plan.json'
        self.receipt = self.base / 'receipt.json'
        self.installer = load_module('install')
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def make_plan(self, old=None):
        (self.stage / 'synthetic.txt').write_text('new')
        if old is not None:
            (self.target / 'synthetic.txt').write_text(old)
        self.installer.plan(self.stage, self.target, self.plan)

    def test_source_drift_rejected_without_target_mutation(self):
        self.make_plan('old')
        (self.stage / 'synthetic.txt').write_text('drift')
        with self.assertRaises(ValueError):
            self.installer.apply(self.plan, self.receipt)
        self.assertEqual((self.target / 'synthetic.txt').read_text(), 'old')

    def test_destination_drift_rejected(self):
        self.make_plan('old')
        (self.target / 'synthetic.txt').write_text('user-edit')
        with self.assertRaises(ValueError):
            self.installer.apply(self.plan, self.receipt)
        self.assertEqual((self.target / 'synthetic.txt').read_text(), 'user-edit')

    def test_rollback_restores_bytes_and_rejects_later_edits(self):
        self.make_plan('old')
        self.installer.apply(self.plan, self.receipt)
        (self.target / 'synthetic.txt').write_text('user-edit')
        with self.assertRaises(ValueError):
            self.installer.rollback(self.receipt)
        self.assertEqual((self.target / 'synthetic.txt').read_text(), 'user-edit')
        (self.target / 'synthetic.txt').write_text('new')
        self.installer.rollback(self.receipt)
        self.assertEqual((self.target / 'synthetic.txt').read_text(), 'old')
        self.assertEqual(json.loads(self.receipt.read_text())['state'], 'rolled-back')

    def test_partial_write_failure_retains_rollback_evidence(self):
        self.make_plan('old')
        with mock.patch.object(self.installer, 'write_atomic', side_effect=OSError('synthetic write error')):
            with self.assertRaises(OSError):
                self.installer.apply(self.plan, self.receipt)
        self.assertNotEqual(json.loads(self.receipt.read_text())['state'], 'installed')
        self.installer.rollback(self.receipt)
        self.assertEqual((self.target / 'synthetic.txt').read_text(), 'old')

    def test_target_root_symlink_drift_is_rejected(self):
        self.make_plan()
        external = self.base / 'external'
        external.mkdir()
        self.target.rmdir()
        self.target.symlink_to(external, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.installer.apply(self.plan, self.receipt)
        self.assertFalse((external / 'synthetic.txt').exists())

    def test_parent_traversal_in_plan_is_rejected(self):
        source = self.stage.parent / 'escape.txt'
        source.write_text('external-source')
        entry = {'path': '../escape.txt', 'source': self.installer.record(source),
                 'before': {'kind': 'absent'}, 'after': self.installer.record(source)}
        self.plan.write_text(json.dumps({'version': 1, 'stage': str(self.stage),
                                         'target': str(self.target), 'entries': [entry]}))
        with self.assertRaises((ValueError, OSError)):
            self.installer.apply(self.plan, self.receipt)
        self.assertFalse((self.target.parent / 'escape.txt').exists())

    def test_receipt_cannot_alias_installed_file(self):
        self.make_plan()
        with self.assertRaises((ValueError, OSError)):
            self.installer.apply(self.plan, self.target / 'synthetic.txt')
        self.assertFalse((self.target / 'synthetic.txt').exists())

    def test_installed_manifest_attests_relocated_bytes_and_archive_is_immutable(self):
        cue = self.stage / '.cue'; cue.mkdir()
        script = cue / 'script.txt'; script.write_text(str(self.stage) + '/resource')
        manifest = cue / 'file-manifest.json'
        manifest.write_text(json.dumps([{'target': '.cue/script.txt', 'target_sha256': self.installer.sha(script.read_bytes())}]))
        archive = self.stage / '.cue-source-archive/original.txt'; archive.parent.mkdir()
        archive.write_text(str(self.stage))
        self.installer.plan(self.stage, self.target, self.plan)
        self.installer.apply(self.plan, self.receipt)
        row = json.loads((self.target / '.cue/file-manifest.json').read_text())[0]
        actual = (self.target / '.cue/script.txt').read_bytes()
        self.assertEqual(row['target_sha256'], self.installer.sha(actual))
        self.assertIn(str(self.target).encode(), actual)
        self.assertEqual((self.target / '.cue-source-archive/original.txt').read_text(), str(self.stage))


class NativeDeliveryRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cue-native-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.plan = self.base / 'plan.json'
        self.receipt = self.base / 'receipt.json'
        self.native = load_module('native_import')
        self.items = [{'itemType': 'SESSIONS', 'sourcePath': str(self.base / 'synthetic-session.jsonl')}]
        self.plan.write_text(json.dumps({'version': 1, 'detect_params': {'migrationSource': 'claude-code'},
                                        'detection': {'items': self.items}, 'items_sha256': self.native.fingerprint(self.items)}))

    def fake_server(self, mode):
        script = self.base / 'fake-codex'
        script.write_text('#!' + sys.executable + '\n' + '''import json, sys
items = ITEMS
mode = MODE

def emit(x):
    print(json.dumps(x), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialized':
        continue
    reqid = request.get('id')
    if method == 'initialize':
        emit({'id': reqid, 'result': {}})
    elif method == 'externalAgentConfig/detect':
        emit({'id': reqid, 'result': {'items': items}})
    elif method == 'externalAgentConfig/import':
        completion = {'importId': 'synthetic-import', 'itemTypeResults': [
            {'itemType': 'SESSIONS', 'successes': [], 'failures': []}]}
        if mode == 'partial':
            completion['itemTypeResults'][0]['failures'] = [{'stage': 'copy', 'message': 'synthetic failure'}]
        if mode == 'malformed':
            completion.pop('itemTypeResults')
        note = {'method': 'externalAgentConfig/import/completed', 'params': completion}
        if mode == 'early':
            emit(note)
        if mode == 'rejected':
            emit({'id': reqid, 'error': {'code': -1, 'message': 'synthetic rejected'}})
            continue
        emit({'id': reqid, 'result': {'importId': 'synthetic-import'}})
        if mode == 'wrong-id':
            note['params']['importId'] = 'unrelated-import'
        if mode not in ('early', 'timeout'):
            emit(note)
    elif method == 'externalAgentConfig/import/readHistories':
        emit({'id': reqid, 'result': {'data': [{'importId': 'synthetic-import',
                                             'itemTypeResults': []}], 'connectors': []}})
'''.replace('ITEMS', repr(self.items)).replace('MODE', repr(mode)))
        script.chmod(0o700)
        return script

    def run_native(self, mode, action='apply'):
        command = [sys.executable, str(CONVERTER_DIR / 'native_import.py'), '--codex', str(self.fake_server(mode)),
                   '--timeout', '3', action, '--receipt', str(self.receipt)]
        if action == 'apply':
            command.extend(['--plan', str(self.plan), '--types', 'SESSIONS'])
        return subprocess.run(command, capture_output=True, text=True, timeout=10)

    def test_completion_before_import_response_is_retained(self):
        result = self.run_native('early')
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt['state'], 'completed')
        self.assertEqual(receipt['completion']['importId'], 'synthetic-import')

    def test_native_timeout_must_be_positive(self):
        result = subprocess.run([sys.executable, str(CONVERTER_DIR / 'native_import.py'),
                                 '--timeout', '0', 'status', '--receipt', str(self.receipt)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.receipt.exists())

    def test_partial_failure_is_nonzero_and_keeps_evidence(self):
        result = self.run_native('partial')
        self.assertEqual(result.returncode, 2, result.stderr)
        receipt = json.loads(self.receipt.read_text())
        self.assertTrue(receipt['completion']['itemTypeResults'][0]['failures'])
        self.assertEqual(json.loads(result.stdout)['failures'], 1)

    def test_timeout_is_not_reported_completed(self):
        result = self.run_native('timeout')
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads(self.receipt.read_text())
        self.assertNotEqual(receipt['state'], 'completed')
        self.assertEqual(receipt['importId'], 'synthetic-import')

    def test_unrelated_completion_is_not_accepted(self):
        result = self.run_native('wrong-id')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(json.loads(self.receipt.read_text())['state'], 'completed')

    def test_import_rejection_never_claims_completion(self):
        result = self.run_native('rejected')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(json.loads(self.receipt.read_text())['state'], 'completed')

    def test_malformed_completion_cannot_claim_success(self):
        result = self.run_native('malformed')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotEqual(json.loads(self.receipt.read_text())['state'], 'completed')

    def test_status_does_not_destroy_existing_apply_receipt(self):
        original = {'state': 'running', 'importId': 'synthetic-import', 'plan': str(self.plan),
                    'categories': {'SESSIONS': 1}, 'started_at': 123}
        self.receipt.write_text(json.dumps(original))
        self.run_native('early', action='status')
        after = json.loads(self.receipt.read_text())
        self.assertEqual(after.get('importId'), original['importId'])
        self.assertEqual(after.get('plan'), original['plan'])
        self.assertEqual(after.get('started_at'), original['started_at'])


if __name__ == '__main__':
    unittest.main()
