#!/usr/bin/env python3
"""Opt-in Codex MCP elicitation probe with synthetic answers.

Uses an ephemeral thread and per-invocation configuration. Model-free by default;
--model-turn opts into paid turns. Never persists hook trust, imports chats, or
answers a real user's question.
"""
import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def toml(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(toml(v) for v in value) + ']'
    return json.dumps(value)


class Probe:
    def __init__(self, command, cwd, timeout, codex_state=None):
        child_env = dict(os.environ)
        if codex_state is not None:
            # Disposable child-only state. Never initialize the owner's history
            # database, load their global hooks, or persist their hook trust.
            child_env['CODEX_HOME'] = str(codex_state)
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, bufsize=1, env=child_env)
        self.inbox = queue.Queue()
        self.stderr = deque(maxlen=30)
        self.timeout = timeout
        self.counter = 0
        self.action = None
        self.accept_content = {'q0': 'A'}
        self.response_meta = None
        self.requests = []
        self.notifications = []
        threading.Thread(target=self.read_stdout, daemon=True).start()
        threading.Thread(target=self.read_stderr, daemon=True).start()

    def read_stdout(self):
        for line in self.process.stdout:
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                self.stderr.append('Non-JSON stdout omitted')
        self.inbox.put(None)

    def read_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line.rstrip())

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def call(self, method, params):
        self.counter += 1
        ident = self.counter
        self.send({'id': ident, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.inbox.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError('Timed out waiting for ' + method)
            if message is None:
                raise RuntimeError('App-server exited: ' + '\n'.join(self.stderr))
            if 'method' in message and 'id' in message:
                if message['method'] == 'mcpServer/elicitation/request' and self.action:
                    params = message['params']
                    if params.get('serverName') != 'cue_questions' or params.get('mode') != 'form':
                        raise RuntimeError('Unexpected elicitation source or mode')
                    self.requests.append(params)
                    content = self.accept_content if self.action == 'accept' else None
                    self.send({'id': message['id'], 'result': {'action': self.action, 'content': content,
                                                               '_meta': self.response_meta}})
                else:
                    self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Probe refuses unrelated requests'}})
                continue
            if 'method' in message:
                self.notifications.append(message['method'])
                continue
            if message.get('id') == ident:
                if 'error' in message:
                    raise RuntimeError(method + ': ' + json.dumps(message['error']))
                return message['result']

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def wait_turn(self, turn_id):
        deadline = time.monotonic() + self.timeout
        output = []
        while True:
            try:
                message = self.inbox.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError('Timed out waiting for model turn')
            if message is None:
                raise RuntimeError('App-server exited during turn')
            method = message.get('method')
            params = message.get('params', {})
            if 'id' in message and method:
                if method == 'mcpServer/elicitation/request' and params.get('serverName') == 'cue_questions' and params.get('mode') == 'form':
                    self.requests.append(params)
                    self.send({'id': message['id'], 'result': {'action': 'accept', 'content': {'q0': 'A'}}})
                else:
                    self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unrelated request refused'}})
            elif method == 'item/agentMessage/delta':
                output.append(params.get('delta', ''))
            elif method == 'turn/completed' and params.get('turn', {}).get('id') == turn_id:
                return params['turn'], ''.join(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--server', type=Path, default=ROOT / 'converter/ask_user_question.py')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=45)
    parser.add_argument('--model-turn', action='store_true', help='Additionally run one paid synthetic model turn and inspect MCP hook payloads')
    parser.add_argument('--require-native-layout-ack', action='store_true',
                        help='Require native metadata return support; this tests transport, not rendering')
    parser.add_argument('--forced-trip', action='store_true', help='With --model-turn, also verify a synthetic question is denied by PreToolUse')
    args = parser.parse_args()
    if not args.run:
        parser.error('Pass --run to execute this synthetic local probe')
    if args.forced_trip and not args.model_turn:
        parser.error('--forced-trip requires --model-turn')
    if not args.server.is_file():
        parser.error('Question server file does not exist')
    report = {'model_turns': 0, 'permanent_trust_changed': False, 'cases': [],
              'source': 'https://learn.chatgpt.com/docs/app-server',
              'evidence_class': 'synthetic app-server transport; does not render or drive terminal UI',
              'isolated_codex_state': True}
    report['question_server_sha256'] = hashlib.sha256(args.server.read_bytes()).hexdigest()
    report['codex_version'] = subprocess.run([args.codex, '--version'], capture_output=True, text=True, check=True).stdout.strip()
    client = None
    try:
        with tempfile.TemporaryDirectory(prefix='cue-live-question-') as directory:
            base = Path(directory).resolve()
            state = base / 'codex-state'
            state.mkdir()
            log = base / 'hooks.jsonl'
            hook = base / 'record.py'
            hook.write_text('import json,sys\nfrom pathlib import Path\nd=json.load(sys.stdin)\n'
                            + 'with Path(' + repr(str(log)) + ').open("a") as f: f.write(json.dumps(d)+"\\n")\n'
                            + 'if d.get("hook_event_name")=="PreToolUse" and any(q.get("question")=="Denied synthetic question" for q in d.get("tool_input",{}).get("questions",[])):\n'
                            + ' print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Synthetic question denied by test hook. Do not retry."}}))\n')
            routes = {event: [{'matcher': '.*', 'hooks': [{'type': 'command',
                        'command': shlex.quote(sys.executable) + ' ' + shlex.quote(str(hook))}]}]
                      for event in ('PreToolUse', 'PostToolUse')}
            servers = {'cue_questions': {'command': sys.executable,
                       'args': [str(args.server.resolve())], 'tool_timeout_sec': 3600}}
            command = [args.codex, '--dangerously-bypass-hook-trust', 'app-server', '--listen', 'stdio://',
                       '-c', 'mcp_servers=' + toml(servers), '-c', 'hooks=' + toml(routes),
                       '-c', 'features.hooks=true']
            client = Probe(command, base, args.timeout, codex_state=state)
            client.call('initialize', {'clientInfo': {'name': 'cue_question_probe', 'version': '1.0.0'},
                                      'capabilities': {'experimentalApi': True}})
            client.send({'method': 'initialized'})
            thread = client.call('thread/start', {'cwd': str(base), 'ephemeral': True,
                                                'approvalPolicy': 'on-request', 'sandbox': 'workspace-write',
                                                'config': {'bypass_hook_trust': True}})
            thread_id = thread['thread']['id']
            inventory = client.call('mcpServerStatus/list', {'threadId': thread_id, 'detail': 'toolsAndAuthOnly'})
            servers_found = inventory['data']
            while inventory.get('nextCursor'):
                inventory = client.call('mcpServerStatus/list', {'threadId': thread_id,
                    'detail': 'toolsAndAuthOnly', 'cursor': inventory['nextCursor']})
                servers_found.extend(inventory['data'])
            found = any(s['name'] == 'cue_questions' and 'AskUserQuestion' in s['tools'] for s in servers_found)
            report['discovered'] = found
            if not found:
                raise RuntimeError('Configured AskUserQuestion tool was not discovered')
            questions = [{'question': 'Pick one', 'header': 'Choice', 'options': [
                {'label': 'A', 'description': 'first'}, {'label': 'B', 'description': 'second'}], 'multiSelect': False}]
            cases = [(action, questions, {'q0': 'A'}, {'Pick one': 'A'} if action == 'accept' else {})
                     for action in ('accept', 'decline', 'cancel')]
            multiple = [dict(questions[0], multiSelect=True)]
            cases.extend([
                ('accept', multiple, {'q0': ['A', 'B']}, {'Pick one': 'A, B'}),
                ('accept', questions, {'q0_other': 'Synthetic other'}, {'Pick one': 'Synthetic other'}),
                ('accept', [{'question': 'Write a word'}], {'q0': 'Synthetic text'}, {'Write a word': 'Synthetic text'})])
            for index, (action, current_questions, content, expected) in enumerate(cases):
                client.action = action
                client.accept_content = content
                client.response_meta = {'io.cue/questions': {'version': 1}} if action == 'accept' else None
                before = len(client.requests)
                result = client.call('mcpServer/tool/call', {'threadId': thread_id, 'server': 'cue_questions',
                    'tool': 'AskUserQuestion', 'arguments': {'questions': current_questions}})
                structured = result.get('structuredContent', {})
                request = client.requests[-1] if len(client.requests) > before else {}
                ui_hint = request.get('_meta', {}).get('io.cue/questions')
                expected_hint = {'version': 1, 'questions': [
                    {'id': 'q' + str(i), 'multiSelect': bool(q.get('multiSelect', False)),
                     **({'customField': 'q' + str(i) + '_other'} if q.get('options') else {})}
                    for i, q in enumerate(current_questions)]}
                passed = (not result.get('isError') and len(client.requests) == before + 1
                          and structured.get('action') == action and structured.get('answers') == expected
                          and structured.get('questions') == current_questions
                          and ui_hint == expected_hint)
                report['cases'].append({'case': ['single', 'decline', 'cancel', 'multiple', 'other', 'text'][index],
                    'action': action, 'passed': passed,
                    'elicitation_requests': len(client.requests) - before,
                    'returned_action': structured.get('action'), 'answers_match': structured.get('answers') == expected})
                report['cases'][-1]['layout_hint_preserved'] = ui_hint == expected_hint
                report['cases'][-1]['answer_details'] = structured.get('answerDetails')
                report['cases'][-1]['native_layout_ack_preserved'] = structured.get('questionLayout') == 'same-question-v1'
            if args.model_turn:
                before = len(client.requests)
                prompt = ('Call the MCP tool AskUserQuestion on server cue_questions exactly once, with arguments '
                          + json.dumps({'questions': questions})
                          + '. Wait for the actual answer, then reply exactly QUESTION_RESULT=A if the answer is A. '
                          'Use this MCP tool, and do not use native request_user_input or shell tools.')
                started = client.call('turn/start', {'threadId': thread_id,
                    'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]})
                report['model_turns'] = 1
                finished, output = client.wait_turn(started['turn']['id'])
                report['model_case'] = {'status': finished.get('status'),
                    'elicitation_requests': len(client.requests) - before,
                    'answer_observed': 'QUESTION_RESULT=A' in output}
                if args.forced_trip:
                    before = len(client.requests)
                    prompt = ('Attempt AskUserQuestion on server cue_questions exactly once with '
                              '{"questions":[{"question":"Denied synthetic question"}]}. '
                              'If denied, do not retry or bypass. Report the denial and finish.')
                    started = client.call('turn/start', {'threadId': thread_id,
                        'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]})
                    report['model_turns'] += 1
                    finished, output = client.wait_turn(started['turn']['id'])
                    report['forced_trip'] = {'status': finished.get('status'),
                        'elicitation_requests': len(client.requests) - before}
            events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            report['hook_events'] = [{'event': e.get('hook_event_name'), 'tool': e.get('tool_name')} for e in events]
            report['hook_pipeline_observed'] = all(any(e.get('hook_event_name') == kind
                and e.get('tool_name') == 'mcp__cue_questions__AskUserQuestion' for e in events)
                                                  for kind in ('PreToolUse', 'PostToolUse'))
            report['hook_limit'] = 'Direct MCP RPC hook observation is separate from model-dispatched hook coverage.'
            report['passed'] = all(c['passed'] for c in report['cases'])
            report['native_layout_ack_transport'] = all(c['native_layout_ack_preserved'] for c in report['cases'] if c['action'] == 'accept')
            if args.require_native_layout_ack:
                report['passed'] = report['passed'] and report['native_layout_ack_transport']
            if args.model_turn:
                report['raw_synthetic_hook_payloads'] = events
                case = report['model_case']
                case['passed'] = (case['status'] == 'completed' and case['elicitation_requests'] == 1
                                  and case['answer_observed'] and report['hook_pipeline_observed'])
                report['passed'] = report['passed'] and case['passed']
                if args.forced_trip:
                    denied_events = [e for e in events if any(q.get('question') == 'Denied synthetic question'
                                      for q in e.get('tool_input', {}).get('questions', []))]
                    denial = report['forced_trip']
                    denial['pre_events'] = sum(e['hook_event_name'] == 'PreToolUse' for e in denied_events)
                    denial['post_events'] = sum(e['hook_event_name'] == 'PostToolUse' for e in denied_events)
                    denial['passed'] = (denial['status'] == 'completed' and denial['elicitation_requests'] == 0
                                        and denial['pre_events'] == 1 and denial['post_events'] == 0)
                    report['passed'] = report['passed'] and denial['passed']
    except Exception as exc:
        report.update(passed=False, error=str(exc))
    finally:
        if client:
            client.close()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
