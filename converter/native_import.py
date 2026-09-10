#!/usr/bin/env python3
"""Inspect and extend Codex's native Claude importer without fabricating history.

detect PROJECT --plan FILE [--include-home] [--max-sessions N] [--max-age-days N]
apply --plan FILE --types SESSIONS,PLUGINS --receipt FILE
status --receipt FILE

Apply writes to Codex's actual destinations, not the converter staging directory.
Plans and receipts can contain private paths; they are written with mode 0600.
"""
import argparse
from collections import Counter, deque
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

try:
    from .version import VERSION
except ImportError:
    from version import VERSION


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def fingerprint(items):
    return hashlib.sha256(json.dumps(items, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Client:
    def __init__(self, executable='codex', timeout=120):
        self.timeout = timeout
        self.process = subprocess.Popen([executable, 'app-server', '--listen', 'stdio://'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
        self.inbox, self.notifications, self.errors = queue.Queue(), [], deque(maxlen=40)
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        self.request('initialize', {'clientInfo': {'name': 'cue_converter', 'version': VERSION},
                                    'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized'})

    def _read(self):
        for line in self.process.stdout:
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                self.errors.append('Non-JSON app-server output omitted.')
        self.inbox.put(None)

    def _stderr(self):
        for line in self.process.stderr:
            self.errors.append(line.rstrip())

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def receive(self, deadline):
        try:
            message = self.inbox.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError('Native importer timed out; reconcile status before retrying any apply.')
        if message is None:
            raise RuntimeError('Codex app-server closed the connection. Check CLI availability and filesystem access.')
        # This client starts no model turns and authorizes no server-initiated tool requests.
        if 'method' in message and 'id' in message:
            self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported client request'}})
        return message

    def request(self, method, params=None):
        self.next_id += 1
        request_id = self.next_id
        message = {'id': request_id, 'method': method}
        if params is not None:
            message['params'] = params
        self.send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            response = self.receive(deadline)
            if response.get('id') == request_id and 'method' not in response:
                if 'error' in response:
                    error = response['error']
                    raise RuntimeError(f'Native {method} rejected (code {error.get("code")}): {error.get("message")}')
                return response.get('result')
            if 'method' in response and 'id' not in response:
                self.notifications.append(response)

    def completed(self, import_id):
        deadline = time.monotonic() + self.timeout
        while True:
            for i, note in enumerate(self.notifications):
                if note.get('method') == 'externalAgentConfig/import/completed' and note.get('params', {}).get('importId') == import_id:
                    return self.notifications.pop(i)['params']
            note = self.receive(deadline)
            if 'method' in note and 'id' not in note:
                self.notifications.append(note)

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


def summary(items):
    return dict(sorted(Counter(item['itemType'] for item in items).items()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--timeout', type=int, default=300)
    sub = parser.add_subparsers(dest='action', required=True)
    detect = sub.add_parser('detect')
    detect.add_argument('project', type=Path)
    detect.add_argument('--plan', type=Path, required=True)
    detect.add_argument('--include-home', action='store_true')
    detect.add_argument('--max-sessions', type=int, default=50)
    detect.add_argument('--max-age-days', type=int, default=30)
    apply = sub.add_parser('apply')
    apply.add_argument('--plan', type=Path, required=True)
    apply.add_argument('--types', required=True, help='Explicit comma-separated categories from detection')
    apply.add_argument('--receipt', type=Path, required=True)
    status = sub.add_parser('status')
    status.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be a positive number of seconds')
    client = None
    try:
        if args.action == 'detect':
            if args.plan.exists():
                raise ValueError('Plan already exists; choose a new path to retain audit history.')
            if args.max_sessions < 0 or args.max_age_days < 0:
                raise ValueError('History limits must be nonnegative.')
        if args.action == 'apply' and args.receipt.exists():
            raise ValueError('Receipt already exists. Reconcile status instead of reapplying.')
        client = Client(args.codex, args.timeout)
        if args.action == 'detect':
            params = {'migrationSource': 'claude-code', 'includeHome': args.include_home,
                      'cwds': [str(args.project.expanduser().resolve())],
                      'maxSessions': args.max_sessions, 'maxSessionAgeDays': args.max_age_days}
            result = client.request('externalAgentConfig/detect', params)
            plan = {'version': 1, 'detect_params': params, 'detection': result,
                    'items_sha256': fingerprint(result['items']), 'created_at': time.time()}
            private_json(args.plan, plan)
            print(json.dumps({'plan': str(args.plan), 'categories': summary(result['items']),
                              'include_home': args.include_home}, indent=2))
        elif args.action == 'apply':
            plan = json.loads(args.plan.read_text())
            if fingerprint(plan['detection']['items']) != plan['items_sha256']:
                raise ValueError('Saved detection was modified; create a fresh plan.')
            requested = set(args.types.split(','))
            selected = [x for x in plan['detection']['items'] if x['itemType'] in requested]
            if requested - {x['itemType'] for x in selected}:
                raise ValueError('Requested categories were not all detected.')
            current = client.request('externalAgentConfig/detect', plan['detect_params'])
            current_selected = [x for x in current['items'] if x['itemType'] in requested]
            if fingerprint(current_selected) != fingerprint(selected):
                raise ValueError('Native detection changed; inspect a new plan before applying.')
            receipt = {'state': 'starting', 'categories': summary(selected), 'plan': str(args.plan),
                       'started_at': time.time(), 'warning': 'Native writes use real target locations; no staging or rollback API.'}
            private_json(args.receipt, receipt)
            accepted = client.request('externalAgentConfig/import',
                                      {'migrationSource': 'claude-code', 'migrationItems': selected})
            receipt.update(state='running', importId=accepted['importId'])
            private_json(args.receipt, receipt)
            completed = client.completed(accepted['importId'])
            results = completed.get('itemTypeResults')
            if not isinstance(results, list) or not results or any(
                    not isinstance(r, dict) or not isinstance(r.get('itemType'), str)
                    or not isinstance(r.get('successes'), list) or not isinstance(r.get('failures'), list)
                    for r in results):
                raise ValueError('Malformed native completion; receipt remains running until status is reconciled.')
            if requested - {r['itemType'] for r in results}:
                raise ValueError('Native completion omitted selected categories; success is not established.')
            receipt.update(state='completed', completion=completed)
            private_json(args.receipt, receipt)
            failures = sum(len(x.get('failures', [])) for x in completed.get('itemTypeResults', []))
            print(json.dumps({'receipt': str(args.receipt), 'state': 'completed', 'failures': failures}))
            return 2 if failures else 0
        else:
            result = client.request('externalAgentConfig/import/readHistories')
            if args.receipt.exists():
                previous = json.loads(args.receipt.read_text())
                previous['history_snapshot'] = result
                previous['history_checked_at'] = time.time()
                private_json(args.receipt, previous)
            else:
                private_json(args.receipt, result)
            print(json.dumps({'history_receipt': str(args.receipt), 'history_entries': len(result.get('data', []))}))
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
