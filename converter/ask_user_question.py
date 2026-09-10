#!/usr/bin/env python3
"""Dependency-free MCP AskUserQuestion server using the host's elicitation UI.

No terminal input, network service, default answers, or persistent answer store.
Run directly, or with the distributable's `questions` command.
"""
import json
import sys

try:
    from .version import VERSION
except ImportError:
    from version import VERSION

NAME = 'AskUserQuestion'
QUESTION_UI_KEY = 'io.cue/questions'
QUESTION_UI_VERSION = 1
SUPPORTED = ('2025-11-25', '2025-06-18')
INPUT_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['questions'],
    'properties': {'questions': {'type': 'array', 'minItems': 1, 'maxItems': 4,
        'items': {'type': 'object', 'additionalProperties': False, 'required': ['question'],
            'properties': {
                'question': {'type': 'string', 'minLength': 1},
                'header': {'type': 'string'}, 'multiSelect': {'type': 'boolean'},
                'options': {'type': 'array', 'maxItems': 20, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['label'],
                    'properties': {'label': {'type': 'string', 'minLength': 1},
                                   'description': {'type': 'string'}, 'preview': {'type': 'string'}}}}
            }}}}}


def form(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {'questions'}:
        raise ValueError('Expected only a questions array.')
    questions = arguments['questions']
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        raise ValueError('Provide one to four questions.')
    properties, required, seen = {}, [], set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict) or set(q) - {'question', 'header', 'options', 'multiSelect'}:
            raise ValueError('Unsupported question fields.')
        title = q.get('question')
        if not isinstance(title, str) or not title.strip() or title in seen:
            raise ValueError('Question text must be nonempty and unique.')
        seen.add(title)
        if not isinstance(q.get('header', ''), str) or not isinstance(q.get('multiSelect', False), bool):
            raise ValueError('Invalid header or multiSelect.')
        options = q.get('options', [])
        if not isinstance(options, list) or len(options) > 20:
            raise ValueError('Invalid options array.')
        labels, titled = set(), []
        for option in options:
            if not isinstance(option, dict) or set(option) - {'label', 'description', 'preview'}:
                raise ValueError('Unsupported option fields.')
            label = option.get('label')
            if not isinstance(label, str) or not label.strip() or label in labels:
                raise ValueError('Option labels must be nonempty and unique.')
            labels.add(label)
            for key in ('description', 'preview'):
                if not isinstance(option.get(key, ''), str):
                    raise ValueError('Option descriptions and previews must be text.')
            detail = '\n'.join(option[k] for k in ('description', 'preview') if option.get(k))
            titled.append({'const': label, 'title': label + (': ' + detail if detail else '')})
        key = 'q' + str(i)
        field = {'title': title, 'description': q.get('header', '')}
        if options:
            if q.get('multiSelect'):
                field.update(type='array', items={'anyOf': titled}, maxItems=len(options))
            else:
                field.update(type='string', oneOf=titled)
            properties[key + '_other'] = {'type': 'string', 'title': title + ': your own answer',
                'description': ('Write an additional answer of your own.' if q.get('multiSelect')
                                else 'Write your own answer instead of selecting an option.')}
        else:
            field.update(type='string', minLength=1)
            required.append(key)
        properties[key] = field
    return {'type': 'object', 'properties': properties, 'required': required}


def question_ui(questions):
    """Versioned renderer hint; the standard schema remains usable without it.

    This requests a layout, never asserts that a client rendered that layout.
    Patched Codex coalesces customField into its question and leaves the input
    visible beside the choices. Other clients retain the standard form fields.
    """
    rows = []
    for i, q in enumerate(questions):
        row = {'id': 'q' + str(i), 'multiSelect': bool(q.get('options') and q.get('multiSelect', False))}
        if q.get('options'):
            row['customField'] = row['id'] + '_other'
        rows.append(row)
    return {QUESTION_UI_KEY: {'version': QUESTION_UI_VERSION, 'questions': rows}}


def answer(questions, result):
    if not isinstance(result, dict) or result.get('action') not in ('accept', 'decline', 'cancel'):
        raise ValueError('Host returned an invalid elicitation action.')
    action = result['action']
    metadata = result.get('_meta')
    acknowledgement = metadata.get(QUESTION_UI_KEY) if isinstance(metadata, dict) else None
    native_ui = (isinstance(acknowledgement, dict)
                 and type(acknowledgement.get('version')) is int
                 and acknowledgement['version'] == QUESTION_UI_VERSION)
    answers, details = {}, {}
    if action == 'accept':
        content = result.get('content')
        allowed = {'q' + str(i) for i in range(len(questions))}
        allowed.update('q' + str(i) + '_other' for i, q in enumerate(questions) if q.get('options'))
        if not isinstance(content, dict) or set(content) - allowed:
            raise ValueError('Host returned invalid answer fields.')
        for i, q in enumerate(questions):
            key = 'q' + str(i)
            choice, other = content.get(key), content.get(key + '_other', '')
            if not isinstance(other, str):
                raise ValueError('Free text answer must be a string.')
            labels = {o['label'] for o in q.get('options', [])}
            if q.get('multiSelect') and labels:
                values = [] if choice is None else choice
                if not isinstance(values, list) or any(not isinstance(v, str) or v not in labels for v in values) or len(values) != len(set(values)):
                    raise ValueError('Invalid multiple selection.')
            else:
                if choice is not None and (not isinstance(choice, str) or (labels and choice not in labels)):
                    raise ValueError('Invalid selection or free text answer.')
                values = [choice] if choice else []
            selections = list(values) if labels else []
            if native_ui and labels and not q.get('multiSelect') and selections and other.strip():
                raise ValueError('Choose one listed option or write your own answer, not both.')
            if other.strip():
                values.append(other)
            if not values or not any(v.strip() for v in values):
                raise ValueError('Every question needs an actual answer; no default was submitted.')
            answers[q['question']] = ', '.join(values)
            details[q['question']] = {
                'selectedOptions': selections,
                'customAnswer': other if other.strip() else None,
                'textAnswer': choice if not labels else None,
            }
    elif result.get('content') not in (None, {}):
        raise ValueError('Declined or cancelled requests cannot contain answers.')
    return {'action': action, 'questions': questions, 'answers': answers,
            'answerDetails': details,
            'questionLayout': 'same-question-v1' if native_ui else 'unconfirmed-standard-form'}


def tool_result(value, error=False):
    return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
            'structuredContent': value, 'isError': error}


class Server:
    """One input loop routes simultaneous calls by ID without blocking on a user."""
    def __init__(self, send):
        self.send = send
        self.pending = {}
        self.sequence = 0
        self.initialized = False
        self.form_supported = False

    def result(self, request_id, value):
        self.send({'jsonrpc': '2.0', 'id': request_id, 'result': value})

    def error(self, request_id, code, message):
        self.send({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}})

    def receive(self, row):
        if not isinstance(row, dict) or row.get('jsonrpc') != '2.0':
            self.error(None, -32600, 'Invalid JSON-RPC message.'); return
        request_id = row.get('id')
        if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            self.error(None, -32600, 'Invalid request ID.'); return
        if 'method' not in row:
            pending = self.pending.pop(request_id, None)
            if pending is None:
                return
            call_id, questions = pending
            try:
                if 'error' in row:
                    raise ValueError('The host could not collect a user response.')
                value = answer(questions, row.get('result'))
                self.result(call_id, tool_result(value))
            except ValueError as exc:
                self.result(call_id, tool_result({'action': 'error', 'answers': {}, 'error': str(exc)}, True))
            return
        method, params = row['method'], row.get('params', {})
        if not isinstance(params, dict):
            if request_id is not None:
                self.error(request_id, -32602, 'Parameters must be an object.')
            return
        if method == 'notifications/cancelled':
            for eid, (call_id, questions) in list(self.pending.items()):
                if call_id == params.get('requestId'):
                    del self.pending[eid]
                    self.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                               'params': {'requestId': eid, 'reason': 'Calling tool was cancelled.'}})
                    self.result(call_id, tool_result(answer(questions, {'action': 'cancel'})))
            return
        if request_id is None:
            return
        if method == 'initialize':
            caps = params.get('capabilities', {})
            elicitation = caps.get('elicitation') if isinstance(caps, dict) else None
            self.form_supported = isinstance(elicitation, dict) and (not elicitation or 'form' in elicitation)
            self.initialized = True
            version = params.get('protocolVersion')
            self.result(request_id, {'protocolVersion': version if version in SUPPORTED else SUPPORTED[0],
                'capabilities': {'tools': {}}, 'serverInfo': {'name': 'cue-questions', 'version': VERSION},
                'instructions': 'AskUserQuestion collects actual user responses. Do not request credentials or secrets. Answers do not change host execution permissions.'})
        elif method == 'ping':
            self.result(request_id, {})
        elif not self.initialized:
            self.error(request_id, -32000, 'Initialize first.')
        elif method == 'tools/list':
            self.result(request_id, {'tools': [{'name': NAME, 'title': 'Ask the user',
                'description': 'Ask one to four questions and wait for the user through the host UI. Supports single choice, multiple choice, and free text. No credentials or secrets. Returns accept, decline, or cancel; an answer is not host execution permission.',
                'inputSchema': INPUT_SCHEMA,
                'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}]})
        elif method == 'tools/call':
            if params.get('name') != NAME:
                self.error(request_id, -32602, 'Unknown tool.'); return
            try:
                schema = form(params.get('arguments'))
                if not self.form_supported:
                    raise ValueError('This host does not advertise MCP form elicitation. Use its native question UI; no answer was collected.')
                if len(self.pending) >= 16 or any(p[0] == request_id for p in self.pending.values()):
                    raise ValueError('Too many pending requests or duplicate call ID.')
            except ValueError as exc:
                self.result(request_id, tool_result({'action': 'error', 'answers': {}, 'error': str(exc)}, True)); return
            self.sequence += 1
            eid = 'cue-question-' + str(self.sequence)
            self.pending[eid] = (request_id, params['arguments']['questions'])
            self.send({'jsonrpc': '2.0', 'id': eid, 'method': 'elicitation/create',
                       'params': {'mode': 'form',
                           'message': 'Choose an option or write your own answer. You can decline or cancel. '
                                      'Hosts without Cue question-layout support show custom input separately.',
                           '_meta': question_ui(params['arguments']['questions']),
                           'requestedSchema': schema}})
        else:
            self.error(request_id, -32601, 'Method not found.')


def main():
    def send(row):
        print(json.dumps(row, ensure_ascii=False), flush=True)
    server = Server(send)
    for line in sys.stdin:
        try:
            if len(line) > 1048576:
                raise ValueError('Message too large')
            server.receive(json.loads(line))
        except (ValueError, TypeError) as exc:
            server.error(None, -32700, 'Invalid JSON-RPC input: ' + type(exc).__name__)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
