"""Async card receipts identify submitted questions, never completed answers."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(os.environ.get('CUE_PROTOCOL_TEST_SOURCE', Path(__file__).resolve().parents[1] / 'converter/protocol.py'))
spec = importlib.util.spec_from_file_location('async_protocol_test', SOURCE)
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


class AsyncQuestionProtocol(unittest.TestCase):
    def event(self, result=None):
        return {'hook_event_name': 'PostToolUse', 'tool_name': 'functions.request_user_input_async',
                'session_id': 'async-fixture', 'tool_input': {'questions': [
                    {'title': 'Choose a fixture', 'options': ['First', 'Second']}]},
                'tool_response': result if result is not None else {'accepted': True}}

    def test_submission_maps_question_without_answer(self):
        original = self.event()
        prior = copy.deepcopy(original)
        result = protocol.normalize(original)
        self.assertEqual(original, prior)
        self.assertEqual(result['tool_name'], 'AskUserQuestion')
        question = result['tool_input']['questions'][0]
        self.assertEqual(question['question'], 'Choose a fixture')
        self.assertEqual(question['options'], [{'label': 'First'}, {'label': 'Second'}])
        self.assertTrue(result['cue_question_submitted'])
        self.assertTrue(result['cue_question_unanswered'])

    def test_receipt_cannot_manufacture_answer_evidence(self):
        for result in ({'accepted': True, 'answers': {'Choose a fixture': 'First'}, 'action': 'accept'},
                       {'accepted': False}, {'accepted': True, 'isError': True}, {},
                       {'accepted': 'true'}, {'action': 'cancel'}, 'not json'):
            with self.subTest(result=result):
                normalized = protocol.normalize(self.event(result))
                self.assertTrue(normalized['cue_question_unanswered'])
                if result != {'accepted': True, 'answers': {'Choose a fixture': 'First'}, 'action': 'accept'}:
                    self.assertFalse(normalized['cue_question_submitted'])

    def test_free_text_and_malformed_questions_do_not_crash(self):
        for questions in ([{'title': 'Write a fixture'}], [None, {'title': None, 'options': None}], None):
            event = self.event()
            event['tool_input']['questions'] = questions
            result = protocol.normalize(event)
            self.assertEqual(result['tool_name'], 'AskUserQuestion')
            self.assertTrue(result['cue_question_unanswered'])

    def test_real_synchronous_answer_remains_answered(self):
        event = self.event({'answers': {'q1': {'answers': ['First']}}})
        event['tool_name'] = 'functions.request_user_input'
        event['tool_input'] = {'questions': [{'id': 'q1', 'question': 'Choose a fixture'}]}
        self.assertFalse(protocol.normalize(event)['cue_question_unanswered'])

    def test_native_transcript_exposes_async_question_call_to_stop_readers(self):
        with tempfile.TemporaryDirectory(prefix='cue-async-transcript-') as tmp:
            path = Path(tmp) / 'native.jsonl'
            event = self.event()
            path.write_text(json.dumps({'type': 'response_item', 'timestamp': '2026-09-10T08:53:37Z',
                'payload': {'type': 'function_call', 'name': 'request_user_input_async', 'call_id': 'fixture-call',
                            'arguments': json.dumps(event['tool_input'])}}) + '\n')
            previous = protocol.STATE
            try:
                protocol.STATE = Path(tmp) / 'state'
                view = Path(protocol.transcript_view(str(path), {'session_id': 'async-fixture'}))
                record = json.loads(view.read_text().splitlines()[0])
            finally:
                protocol.STATE = previous
            call = record['message']['content'][0]
            self.assertEqual(call['name'], 'AskUserQuestion')
            self.assertEqual(call['id'], 'fixture-call')
            self.assertEqual(call['input']['questions'][0]['question'], 'Choose a fixture')


if __name__ == '__main__':
    unittest.main()
