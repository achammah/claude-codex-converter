import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=False)
    args.report = str(args.work / "report.json")
    binary = Path(args.binary).resolve()
    requests = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                index = len(requests)
                if index == 1:
                    item = {'type': 'custom_tool_call', 'call_id': 'probe-call',
                            'name': 'exec', 'input': 'text(6 * 7);'}
                else:
                    item = {'type': 'message', 'role': 'assistant', 'id': 'probe-message',
                            'content': [{'type': 'output_text', 'text': 'PROBE_FINISHED'}]}
                events = [
                    {'type': 'response.created', 'response': {'id': f'probe-{index}'}},
                    {'type': 'response.output_item.done', 'item': item},
                    {'type': 'response.completed', 'response': {'id': f'probe-{index}',
                        'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
                                  'input_tokens_details': None, 'output_tokens_details': None}}},
                ]
                payload = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n'
                                  for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                errors.append(str(exc))
                self.send_error(500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix='cue-cli-ipc-') as scratch:
        root = Path(scratch)
        home = root / 'codex-home'
        home.mkdir()
        project = root / 'project'
        project.mkdir()
        (home / 'config.toml').write_text(
            'check_for_update_on_startup = false\nmodel = "gpt-6-astra"\nmodel_provider = "fixture"\n'
            'approval_policy = "never"\nsandbox_mode = "workspace-write"\n'
            '[features]\ncode_mode = true\ncode_mode_host = true\n'
            '[model_providers.fixture]\nname = "Local fixture"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            'request_max_retries = 0\nstream_max_retries = 0\n')
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
        env['CODEX_HOME'] = str(home)
        command = [str(binary), 'exec', '--skip-git-repo-check', '--ephemeral', '--json',
                   '-C', str(project), 'Run the local code-mode fixture.']
        try:
            result = subprocess.run(command, env=env, cwd=project, capture_output=True,
                                    text=True, timeout=90)
            exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code, stdout, stderr = -1, str(exc.stdout), str(exc.stderr)
            errors.append('CLI timed out')
    server.shutdown()
    outputs = [item for request in requests[1:] for item in request.get('input', [])
               if item.get('type') == 'custom_tool_call_output' and item.get('call_id') == 'probe-call']
    has_exact_result = any(isinstance(item.get('output'), list) and
                           any(part.get('type') == 'input_text' and part.get('text') == '42'
                               for part in item['output']) for item in outputs)
    passed = exit_code == 0 and len(requests) == 2 and has_exact_result and not errors
    report = {'passed': passed, 'scope': 'real CLI -> core -> code-mode-host -> Responses tool result; local synthetic provider, no paid model',
              'binary': str(binary), 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'request_count': len(requests), 'tool_outputs': outputs, 'exit_code': exit_code,
              'stdout': stdout, 'stderr': stderr, 'errors': errors}
    Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': passed, 'request_count': len(requests), 'exit_code': exit_code,
                      'report': args.report}))
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
