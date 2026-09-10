"""Actual MCP stdio plus question lifecycle regressions; no fabricated user consent."""
import importlib.util
import json
from pathlib import Path
import select
import subprocess
import sys
import unittest

CODE = Path(__file__).resolve().parents[1] / 'converter'
sys.path.insert(0, str(CODE))
import ask_user_question as auq
import protocol

QUESTIONS = [{'question': 'Pick one', 'header': 'Choice', 'options': [
    {'label': 'A', 'description': 'First'}, {'label': 'B', 'description': 'Second'}]}]
NATIVE_UI = {'io.cue/questions': {'version': 1}}


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.server = auq.Server(self.output.append)
        self.call('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {'elicitation': {'form': {}}}}, 1)
        self.output.clear()

    def call(self, method, params, request_id=2):
        self.server.receive({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})

    def ask(self, request_id=2, questions=None):
        self.call('tools/call', {'name': auq.NAME, 'arguments': {'questions': questions or QUESTIONS}}, request_id)
        return self.output[-1]['id']

    def reply(self, eid, result):
        self.server.receive({'jsonrpc': '2.0', 'id': eid, 'result': result})

    def test_no_answer_until_real_host_response(self):
        eid = self.ask()
        self.assertEqual(len(self.output), 1)
        self.assertEqual(self.output[0]['method'], 'elicitation/create')
        self.assertNotIn('default', json.dumps(self.output))
        self.reply(eid, {'action': 'accept', 'content': {'q0': 'B'}})
        result = self.output[-1]['result']
        self.assertEqual(result['structuredContent']['answers'], {'Pick one': 'B'})
        self.assertFalse(result['isError'])

    def test_custom_layout_is_a_versioned_hint_with_standard_fields(self):
        self.ask()
        params = self.output[-1]['params']
        self.assertEqual(params['_meta']['io.cue/questions'], {
            'version': 1, 'questions': [{'id': 'q0', 'customField': 'q0_other', 'multiSelect': False}]})
        schema = params['requestedSchema']
        self.assertEqual(set(schema['properties']), {'q0', 'q0_other'})
        self.assertEqual(schema['properties']['q0']['oneOf'][1]['const'], 'B')
        self.assertEqual(schema['properties']['q0_other']['type'], 'string')
        self.assertNotIn('q0', schema['required'])
        self.assertIn('Hosts without Cue question-layout support', params['message'])

    def test_single_custom_is_an_alternative_not_a_comment(self):
        self.reply(self.ask(), {'action': 'accept', 'content': {'q0_other': 'C, D\nE'}, '_meta': NATIVE_UI})
        result = self.output[-1]['result']['structuredContent']
        self.assertEqual(result['questionLayout'], 'same-question-v1')
        self.assertEqual(result['answers'], {'Pick one': 'C, D\nE'})
        self.assertEqual(result['answerDetails']['Pick one'], {
            'selectedOptions': [], 'customAnswer': 'C, D\nE', 'textAnswer': None})
        self.reply(self.ask(), {'action': 'accept', 'content': {'q0': 'A', 'q0_other': 'C'}, '_meta': NATIVE_UI})
        self.assertTrue(self.output[-1]['result']['isError'])
        self.assertEqual(self.output[-1]['result']['structuredContent']['answers'], {})

    def test_unpatched_host_combination_is_preserved_and_identified(self):
        self.reply(self.ask(), {'action': 'accept', 'content': {'q0': 'A', 'q0_other': 'Additional text'}})
        result = self.output[-1]['result']['structuredContent']
        self.assertEqual(result['answers'], {'Pick one': 'A, Additional text'})
        self.assertEqual(result['questionLayout'], 'unconfirmed-standard-form')
        self.assertEqual(result['answerDetails']['Pick one'], {
            'selectedOptions': ['A'], 'customAnswer': 'Additional text', 'textAnswer': None})

    def test_requested_layout_is_not_renderer_acknowledgement(self):
        for metadata in (None, {}, {'io.cue/questions': {'version': True}},
                         {'io.cue/questions': {'version': 2}}, {'io.cue/questions': '1'}):
            result = auq.answer(QUESTIONS, {'action': 'accept', 'content': {'q0': 'A'}, '_meta': metadata})
            self.assertEqual(result['questionLayout'], 'unconfirmed-standard-form')

    def test_listed_choice_provenance_never_labels_text_as_comment(self):
        self.reply(self.ask(), {'action': 'accept', 'content': {'q0': 'B', 'q0_other': ' '}})
        result = self.output[-1]['result']['structuredContent']
        self.assertEqual(result['answerDetails']['Pick one'], {
            'selectedOptions': ['B'], 'customAnswer': None, 'textAnswer': None})

    def test_custom_text_equal_to_option_keeps_its_provenance(self):
        self.reply(self.ask(), {'action': 'accept', 'content': {'q0_other': 'A'}})
        result = self.output[-1]['result']['structuredContent']
        self.assertEqual(result['answerDetails']['Pick one']['selectedOptions'], [])
        self.assertEqual(result['answerDetails']['Pick one']['customAnswer'], 'A')

    def test_multi_custom_alone_and_duplicate_selection_validation(self):
        questions = [{**QUESTIONS[0], 'multiSelect': True}]
        self.reply(self.ask(questions=questions), {'action': 'accept', 'content': {'q0': [], 'q0_other': 'C'}})
        result = self.output[-1]['result']['structuredContent']
        self.assertEqual(result['answers'], {'Pick one': 'C'})
        self.assertEqual(result['answerDetails']['Pick one']['selectedOptions'], [])
        for content in ({'q0': ['A', 'A']}, {'q0': ['not an option']}, {'q0': [], 'q0_other': '\n'}):
            with self.assertRaises(ValueError):
                auq.answer(questions, {'action': 'accept', 'content': content})

    def test_dismissal_cannot_return_custom_text(self):
        for action in ('decline', 'cancel'):
            with self.assertRaises(ValueError):
                auq.answer(QUESTIONS, {'action': action, 'content': {'q0_other': 'Unaccepted'}})

    def test_empty_custom_text_does_not_erase_valid_choice(self):
        for text in ('', ' ', '\n\t'):
            value = auq.answer(QUESTIONS, {'action': 'accept', 'content': {'q0': 'A', 'q0_other': text}})
            self.assertEqual(value['answers'], {'Pick one': 'A'})
            self.assertIsNone(value['answerDetails']['Pick one']['customAnswer'])

    def test_no_options_has_one_required_text_field(self):
        questions = [{'question': 'Write your answer'}]
        self.ask(questions=questions)
        params = self.output[-1]['params']
        self.assertEqual(set(params['requestedSchema']['properties']), {'q0'})
        self.assertEqual(params['requestedSchema']['required'], ['q0'])
        self.assertEqual(params['_meta']['io.cue/questions']['questions'], [{'id': 'q0', 'multiSelect': False}])
        self.reply(self.output[-1]['id'], {'action': 'accept', 'content': {'q0': 'My answer'}})
        self.assertEqual(self.output[-1]['result']['structuredContent']['answerDetails']['Write your answer'], {
            'selectedOptions': [], 'customAnswer': None, 'textAnswer': 'My answer'})
        with self.assertRaises(ValueError):
            auq.answer(questions, {'action': 'accept', 'content': {'q0_other': 'Wrong field'}})
        self.assertEqual(auq.question_ui([{'question': 'Text', 'multiSelect': True}]), {
            'io.cue/questions': {'version': 1, 'questions': [{'id': 'q0', 'multiSelect': False}]}})

    def test_decline_and_cancel_never_answer(self):
        for action in ('decline', 'cancel'):
            self.reply(self.ask(), {'action': action, 'content': None})
            self.assertEqual(self.output[-1]['result']['structuredContent']['answers'], {})
            self.assertEqual(self.output[-1]['result']['structuredContent']['answerDetails'], {})

    def test_invalid_or_blank_accept_cannot_become_answer(self):
        for content in ({}, {'q0': 'forged'}, {'q0': ['A']}, {'q0_other': '  '}, {'q0': 'A', 'unknown': 'B'}):
            self.reply(self.ask(), {'action': 'accept', 'content': content})
            self.assertTrue(self.output[-1]['result']['isError'])
            self.assertEqual(self.output[-1]['result']['structuredContent']['answers'], {})

    def test_multi_select_and_free_text(self):
        questions = [{**QUESTIONS[0], 'multiSelect': True}, {'question': 'Explain'}]
        self.reply(self.ask(questions=questions), {'action': 'accept', 'content': {
            'q0': ['B', 'A'], 'q0_other': 'Custom', 'q1': 'My reason'}})
        self.assertEqual(self.output[-1]['result']['structuredContent']['answers'], {
            'Pick one': 'B, A, Custom', 'Explain': 'My reason'})
        self.assertEqual(self.output[-1]['result']['structuredContent']['answerDetails']['Pick one'], {
            'selectedOptions': ['B', 'A'], 'customAnswer': 'Custom', 'textAnswer': None})

    def test_interleaved_answers_keep_call_identity(self):
        first, second = self.ask(10), self.ask(11)
        self.reply(second, {'action': 'accept', 'content': {'q0': 'B'}})
        self.assertEqual(self.output[-1]['id'], 11)
        self.reply(first, {'action': 'cancel'})
        self.assertEqual(self.output[-1]['id'], 10)
        self.assertFalse(self.server.pending)

    def test_cancel_notification_and_late_response(self):
        eid = self.ask(17)
        self.call('notifications/cancelled', {'requestId': 17}, None)
        self.assertEqual(self.output[-1]['result']['structuredContent']['action'], 'cancel')
        count = len(self.output)
        self.reply(eid, {'action': 'accept', 'content': {'q0': 'A'}})
        self.assertEqual(len(self.output), count)

    def test_unsupported_host_reports_gap(self):
        for capability in ({}, {'elicitation': {'url': {}}}):
            self.call('initialize', {'capabilities': capability})
            self.ask()
            self.assertTrue(self.output[-1]['result']['isError'])
            self.assertFalse(self.server.pending)

    def test_legacy_empty_elicitation_capability_is_supported(self):
        self.call('initialize', {'capabilities': {'elicitation': {}}})
        self.ask()
        self.assertEqual(self.output[-1]['method'], 'elicitation/create')

    def test_bad_questions_and_duplicate_labels_are_rejected(self):
        for questions in ([], QUESTIONS * 2, [{'question': 'bad', 'options': [{'label': 'A'}, {'label': 'A'}]}],
                          [{'question': 'bad', 'unsupported': True}]):
            with self.assertRaises(ValueError):
                auq.form({'questions': questions})

    def test_only_registered_server_alias_is_normalized(self):
        self.assertEqual(protocol.tool_name('mcp__cue_questions__AskUserQuestion'), 'AskUserQuestion')
        self.assertEqual(protocol.tool_name('mcp__unrelated__AskUserQuestion'), 'mcp__unrelated__AskUserQuestion')

    def test_hook_evidence_separates_answers_and_cancellation(self):
        base = {'tool_name': 'mcp__cue_questions__AskUserQuestion', 'tool_input': {'questions': QUESTIONS}, 'hook_event_name': 'PostToolUse'}
        for action in ('accept', 'decline', 'cancel'):
            value = auq.answer(QUESTIONS, {'action': action, 'content': {'q0': 'A'} if action == 'accept' else None})
            normalized = protocol.normalize(dict(base, tool_response=auq.tool_result(value)))
            self.assertEqual(normalized['cue_question_unanswered'], action != 'accept')
            self.assertEqual(normalized['tool_response']['action'], action)
        self.assertTrue(protocol.normalize(dict(base, tool_response={'submitted': True}))['cue_question_unanswered'])

    def test_partial_accept_never_records_any_answers(self):
        questions = [*QUESTIONS, {'question': 'Explain'}]
        self.reply(self.ask(questions=questions), {'action': 'accept', 'content': {'q0': 'A'}})
        result = self.output[-1]['result']
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['answers'], {})
        normalized = protocol.normalize({'tool_name': 'mcp__cue_questions__AskUserQuestion',
            'hook_event_name': 'PostToolUse', 'tool_input': {'questions': questions},
            'tool_response': auq.tool_result({'action': 'accept', 'answers': {'Pick one': 'A'}})})
        self.assertTrue(normalized['cue_question_unanswered'])

    def test_malformed_mcp_text_is_unanswered_not_normalizer_crash(self):
        for text in (None, [], {}, 42):
            with self.subTest(text=text):
                normalized = protocol.normalize({'tool_name': 'mcp__cue_questions__AskUserQuestion',
                    'hook_event_name': 'PostToolUse', 'tool_input': {'questions': QUESTIONS},
                    'tool_response': {'content': [{'type': 'text', 'text': text}]}})
                self.assertTrue(normalized['cue_question_unanswered'])

    def test_empty_native_answer_containers_are_not_answer_evidence(self):
        for value in ({'answers': []}, {'answers': ['']}, {'answers': ['  ']}, [''], ['  '], {'other': 'metadata'}):
            with self.subTest(value=value):
                normalized = protocol.normalize({'tool_name': 'request_user_input',
                    'hook_event_name': 'PostToolUse', 'tool_input': {'questions': [{'id': 'q0', 'question': 'Pick one'}]},
                    'tool_response': {'answers': {'q0': value}}})
                self.assertTrue(normalized['cue_question_unanswered'])

    def test_native_answers_cover_each_requested_id(self):
        base = {'tool_name': 'request_user_input', 'hook_event_name': 'PostToolUse',
                'tool_input': {'questions': [{'id': 'q0', 'question': 'Pick one'}, {'id': 'q1', 'question': 'Explain'}]}}
        partial = protocol.normalize(dict(base, tool_response={'answers': {'q0': {'answers': ['A']}}}))
        self.assertTrue(partial['cue_question_unanswered'])
        complete = protocol.normalize(dict(base, tool_response={'answers': {
            'q0': {'answers': ['A']}, 'q1': {'answers': ['Because']}}}))
        self.assertFalse(complete['cue_question_unanswered'])

    def test_real_stdio_waits_then_returns_and_exits_on_eof(self):
        proc = subprocess.Popen([sys.executable, str(CODE / 'ask_user_question.py')], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            def send(row):
                proc.stdin.write(json.dumps({'jsonrpc': '2.0', **row}) + '\n'); proc.stdin.flush()
            def read():
                self.assertTrue(select.select([proc.stdout], [], [], 3)[0], 'MCP response timed out')
                return json.loads(proc.stdout.readline())
            proc.stdin.write('not-json\n'); proc.stdin.flush()
            self.assertEqual(read()['error']['code'], -32700)
            send({'id': 1, 'method': 'initialize', 'params': {'capabilities': {'elicitation': {}}}})
            self.assertIn('result', read())
            send({'id': 2, 'method': 'tools/call', 'params': {'name': auq.NAME, 'arguments': {'questions': QUESTIONS}}})
            event = read()
            self.assertFalse(select.select([proc.stdout], [], [], 0.05)[0], 'Answered without user response')
            send({'id': 3, 'method': 'tools/call', 'params': {'name': auq.NAME, 'arguments': {'questions': QUESTIONS}}})
            second = read()
            send({'id': second['id'], 'result': {'action': 'decline', 'content': None}})
            declined = read()
            self.assertEqual(declined['id'], 3)
            self.assertEqual(declined['result']['structuredContent']['answers'], {})
            send({'id': event['id'], 'result': {'action': 'accept', 'content': {'q0_other': 'Written\nanswer'}}})
            accepted = read()
            self.assertEqual(accepted['id'], 2)
            self.assertEqual(accepted['result']['structuredContent']['answers'], {'Pick one': 'Written\nanswer'})
            proc.stdin.close(); proc.wait(timeout=3)
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            proc.stdout.close(); proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
