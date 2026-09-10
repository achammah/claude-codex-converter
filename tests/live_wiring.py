#!/usr/bin/env python3
"""Opt-in real Codex host tests against disposable synthetic projects.

Runs model turns using the local Codex account. It does not persist hook trust;
the documented automation flag runs reviewed hooks for these invocations only.
Existing user hooks may also run. Never run this script against unreviewed hooks.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Actually run local Codex model turns')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--codex', default='codex')
    args = parser.parse_args()
    if not args.run:
        raise SystemExit('Pass --run after reviewing this test and locally installed user hooks.')
    report = {'live_host': True, 'cases': [], 'permanent_trust_changed': False}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='converter-live-') as directory:
        base = Path(directory).resolve()
        source = base / 'source'; target = base / 'target'; (source / '.claude/hooks').mkdir(parents=True)
        script = '''import json, pathlib, sys
d=json.load(sys.stdin)
p=pathlib.Path.cwd()/"observed-events.jsonl"
with p.open("a") as f: f.write(json.dumps({"event":d.get("hook_event_name"),"tool":d.get("tool_name"),"command":d.get("tool_input",{}).get("command","")})+"\\n")
if d.get("hook_event_name")=="PreToolUse" and "CUE_TEST_DENY" in d.get("tool_input",{}).get("command",""):
 print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Synthetic policy refuses this marker. Continue with the allowed task."}}))
'''
        (source / '.claude/hooks/guard.py').write_text(script)
        hooks = {event: [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'python3 .claude/hooks/guard.py'}]}]
                 for event in ('PreToolUse', 'PostToolUse')}
        (source / '.claude/settings.json').write_text(json.dumps({'hooks': hooks}))
        global_settings = base / 'global.json'; global_settings.write_text('{}')
        conversion = subprocess.run([sys.executable, str(ROOT / 'converter/claude_to_codex.py'), str(source),
                                     '--output', str(target), '--global-settings', str(global_settings)], capture_output=True, text=True)
        if conversion.returncode:
            raise SystemExit(conversion.stderr)
        subprocess.run(['git', 'init', '-q', str(target)], check=True, capture_output=True)
        spec = importlib.util.spec_from_file_location('live_converter', ROOT / 'converter/claude_to_codex.py')
        converter = importlib.util.module_from_spec(spec); spec.loader.exec_module(converter)
        routes = json.loads((target / '.codex/hooks.json').read_text())['hooks']
        inline_hooks = converter.toml_value(routes)
        report['hook_loading'] = 'Explicit per-invocation inline configuration of generated definitions; permanent project discovery/trust is a separate check.'
        prompts = [
            ('forced-trip', 'First, attempt this exact shell command once: python3 -c "from pathlib import Path; Path(\'denied-marker\').write_text(\'CUE_TEST_DENY\')". If it is blocked, do not retry or bypass it. Then create allowed-marker containing OK using a shell command and report what happened.'),
            ('genuine-work', 'Create hello.py that prints 42, run it, and report the actual output. Keep the implementation simple.')]
        for name, prompt in prompts:
            command = [args.codex, 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--sandbox', 'workspace-write',
                       '--dangerously-bypass-hook-trust', '--strict-config', '-C', str(target),
                       '--enable', 'hooks', '-c', 'hooks=' + inline_hooks,
                       '-c', 'projects.' + json.dumps(str(target)) + '.trust_level="trusted"', '--json', prompt]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
            events_file = target / 'observed-events.jsonl'
            events = [json.loads(line) for line in events_file.read_text().splitlines()] if events_file.exists() else []
            if name == 'forced-trip':
                attempted = any(e['event'] == 'PreToolUse' and 'CUE_TEST_DENY' in e['command'] for e in events)
                blocked = not (target / 'denied-marker').exists()
                recovered = (target / 'allowed-marker').exists()
                passed = completed.returncode == 0 and attempted and blocked and recovered
                row = {'case': name, 'passed': passed, 'attempt_observed': attempted, 'denied_marker_absent': blocked,
                       'allowed_work_completed': recovered, 'exit_code': completed.returncode}
            else:
                hello = target / 'hello.py'
                output = subprocess.run([sys.executable, str(hello)], capture_output=True, text=True) if hello.exists() else None
                executed = any(e['event'] == 'PostToolUse' and 'hello.py' in e['command'] for e in events)
                passed = completed.returncode == 0 and output is not None and output.returncode == 0 and output.stdout.strip() == '42' and executed
                row = {'case': name, 'passed': passed, 'script_outputs_42': bool(output and output.stdout.strip() == '42'),
                       'execution_observed': executed, 'exit_code': completed.returncode}
            row['pre_events'] = sum(e['event'] == 'PreToolUse' for e in events)
            row['post_events'] = sum(e['event'] == 'PostToolUse' for e in events)
            row['native_output_sha256'] = hashlib.sha256(completed.stdout.encode()).hexdigest()
            if not passed:
                # Synthetic task output only; retain details for diagnosing a failing host test.
                args.report.parent.mkdir(parents=True, exist_ok=True)
                (args.report.parent / (name + '-failure.log')).write_text(completed.stderr + '\n' + completed.stdout)
            report['cases'].append(row)
            print(json.dumps(row), flush=True)
    report.update(passed=all(x['passed'] for x in report['cases']), elapsed_seconds=round(time.monotonic()-started, 3))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
