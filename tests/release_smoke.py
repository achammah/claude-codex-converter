#!/usr/bin/env python3
"""Run the single-file artifact from a fresh interpreter with no installed packages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    report = {'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
              'network_used': False, 'installed_dependencies': False, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='cue-release-smoke-') as temporary:
        root = Path(temporary).resolve()
        venv.EnvBuilder(with_pip=False).create(root / 'interpreter')
        python = root / 'interpreter/bin/python'
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        source, output, target = root / 'source', root / 'converted', root / 'target'
        (source / '.claude/skills/check').mkdir(parents=True)
        (source / '.claude/CLAUDE.md').write_text('Complete synthetic doctrine.\n')
        (source / '.claude/settings.json').write_text(json.dumps({
            'permissions': {'allow': ['Bash(echo:*)']},
            'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': "printf '{}'", 'timeout': 86400}]}]},
            'statusLine': {'type': 'command', 'command': 'python3 .claude/status.py'},
        }))
        (source / '.claude/status.py').write_text(
            'from pathlib import Path\nPath("status-ran").write_text("yes")\nprint("standalone status")\n')
        (source / '.claude/skills/check/SKILL.md').write_text('---\nname: check\ndescription: Check the work\ndisable-model-invocation: true\n---\nRead help.txt.\n')
        (source / '.claude/skills/check/help.txt').write_text('Complete resource.\n')
        (root / 'global.json').write_text('{}')
        target.mkdir()

        def run(*parts):
            result = subprocess.run([str(python), str(artifact), *map(str, parts)],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise AssertionError(result.stderr + result.stdout)
            return result

        run('--help')
        run('convert', source, '--output', output, '--global-settings', root / 'global.json')
        assert (output / '.agents/skills/check/help.txt').read_text() == 'Complete resource.\n'
        assert (output / '.cue/scripts/ask_user_question.py').exists()
        assert not (output / 'status-ran').exists()
        report['checks'].append('offline conversion with bundled dependency and complete resources')
        hooks = json.loads((output / '.codex/hooks.json').read_text())['hooks']
        assert hooks['Stop'][0]['hooks'][0]['timeout'] == 86430
        hook = subprocess.run([str(python), str(output / '.cue/scripts/converted_hook.py')],
                              input=json.dumps({'hook_event_name': 'Stop', 'cwd': str(output),
                                                'session_id': 'release-timeout-fixture'}),
                              env=dict(env, CUE_STATE_ROOT=str(root / 'hook-state')), cwd=root,
                              capture_output=True, text=True, timeout=10)
        assert hook.returncode == 0, hook.stderr
        assert json.loads(hook.stdout) == {}, hook.stdout
        report['checks'].append('long source hook deadline survives packaging and generated runtime executes')
        doctor = run('doctor', output)
        assert json.loads(doctor.stdout)['offline_checks_passed']
        ordinary_status = run('status', output, '--session', 'release-smoke-session')
        assert 'Session status: unobserved' in ordinary_status.stdout
        assert not (output / 'status-ran').exists()
        source_status = run('status', output, '--session', 'release-smoke-session', '--run-source')
        assert source_status.stdout.strip() == 'standalone status'
        assert (output / 'status-ran').read_text() == 'yes'
        report['checks'].append('doctor and status reader stay passive; source status runs only when explicit')
        run('install', 'plan', output, target, '--plan', root / 'plan.json')
        run('install', 'apply', '--plan', root / 'plan.json', '--receipt', root / 'receipt.json')
        assert (target / '.agents/skills/check/help.txt').is_file()
        assert str(target) in (target / '.codex/config.toml').read_text()
        report['checks'].append('installation relocates paths and skill bindings')
        run('host', 'stage', target, '--host', 'claude', '--output', root / 'claude-stage')
        assert 'disable-model-invocation: true' in (root / 'claude-stage/.claude/skills/check/SKILL.md').read_text()
        report['checks'].append('Claude view restores source metadata')
        reverse = root / 'reverse'
        run('reverse', output, '--output', reverse, '--strict')
        assert (reverse / '.claude/CLAUDE.md').read_bytes() == (source / '.claude/CLAUDE.md').read_bytes()
        report['checks'].append('standalone reverse setup restores verified original bytes')
        conversation = root / 'conversation.jsonl'
        conversation_bytes = (json.dumps({
            'type': 'user', 'uuid': 'u1', 'parentUuid': None,
            'sessionId': 'release-smoke', 'timestamp': '2026-01-01T00:00:00Z',
            'cwd': '/synthetic',
            'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'Exact café'}]},
        }, ensure_ascii=False) + '\n').encode()
        conversation.write_bytes(conversation_bytes)
        codex_bundle = root / 'codex-conversation'
        claude_bundle = root / 'claude-conversation'
        run('conversation', '--from', 'claude', '--to', 'codex',
            '--input', conversation, '--output', codex_bundle)
        run('conversation', '--from', 'codex', '--to', 'claude',
            '--input', codex_bundle / 'generated/rollout.jsonl', '--output', claude_bundle)
        assert (claude_bundle / 'generated/claude-session.jsonl').read_bytes() == conversation_bytes
        report['checks'].append('standalone conversation conversion restores exact source bytes')
        initialization = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                     'params': {'capabilities': {'elicitation': {}}}}) + '\n'
        question = subprocess.run([str(python), str(artifact), 'questions'], input=initialization,
                                  env=env, cwd=root, capture_output=True, text=True, timeout=10)
        assert question.returncode == 0 and json.loads(question.stdout)['result']['serverInfo']['name'] == 'cue-questions'
        report['checks'].append('standalone question server speaks MCP')
        run('install', 'rollback', '--receipt', root / 'receipt.json')
        assert not (target / '.codex/config.toml').exists()
        report['checks'].append('rollback restores empty target')
    report['passed'] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
