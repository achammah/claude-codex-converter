"""Transcript streaming contracts; run against either protocol implementation."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class TranscriptStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        module = Path(os.environ.get('CUE_PROTOCOL_TEST_PATH',
                      str(Path(__file__).resolve().parents[1] / 'converter/protocol.py')))
        spec = importlib.util.spec_from_file_location('stream_protocol', module)
        self.protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.protocol)
        self.protocol.STATE = self.root / 'state'
        self.source = self.root / 'input.jsonl'
        self.data = {'session_id': 'stream-test', 'hook_event_name': 'Stop'}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows):
        with self.source.open('w') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')

    def view(self):
        return Path(self.protocol.transcript_view(str(self.source), self.data))

    def test_large_unicode_output_is_exact_without_whole_file_read(self):
        row = {'type': 'assistant', 'message': {'content': '😀é' * 4096}}
        self.write(row for _ in range(512))
        with patch.object(Path, 'read_text', side_effect=AssertionError('whole-file read')):
            out = self.view()
        expected = (json.dumps(row) + '\n').encode()
        with out.open('rb') as f:
            self.assertEqual(sum(1 for line in f if line == expected), 512)
        self.assertEqual(out.stat().st_size, len(expected) * 512)

    def test_event_fallback_sort_and_response_user_suppression(self):
        events = [{'type': 'event_msg', 'timestamp': t,
                   'payload': {'type': 'user_message', 'message': t}} for t in ['z', 'a']]
        self.write(events)
        self.assertEqual([json.loads(x)['timestamp'] for x in self.view().read_text().splitlines()], ['a', 'z'])
        self.write(events + [{'type': 'response_item', 'timestamp': 'm', 'payload': {
            'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'real'}]}}])
        rows = [json.loads(x) for x in self.view().read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['message']['content'][0]['text'], 'real')

    def test_malformed_and_unknown_provenance_counts(self):
        self.source.write_text('bad\n[]\n' + json.dumps({'type': 'future', 'payload': {}}) + '\n' +
                               json.dumps({'type': 'assistant', 'message': {'content': 'ok'}}))
        self.view()
        record = json.loads((self.protocol.STATE / 'adapter-health.jsonl').read_text())
        self.assertEqual(record['detail'], {'types': ['future', 'invalid-json', 'non-object'], 'count': 4})

    def test_midread_failure_does_not_replace_previous_view(self):
        self.write([{'type': 'assistant', 'message': {'content': 'prior'}}])
        out = self.view()
        before = out.read_bytes()
        class BrokenReader:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def __iter__(self):
                yield '{"type":"assistant","message":{"content":"partial"}}\n'
                raise OSError('isolated read failure')
        original = Path.open
        source = self.source
        def opening(path, *args, **kwargs):
            return BrokenReader() if path == source else original(path, *args, **kwargs)
        with patch.object(Path, 'open', opening):
            self.assertIsNone(self.protocol.transcript_view(str(source), self.data))
        self.assertEqual(out.read_bytes(), before)
        record = json.loads((self.protocol.STATE / 'adapter-health.jsonl').read_text())
        self.assertEqual(record['code'], 'transcript-unreadable')


if __name__ == '__main__':
    unittest.main()
