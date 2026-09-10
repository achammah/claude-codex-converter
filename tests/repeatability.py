#!/usr/bin/env python3
"""Run real repeated conversions of identical inputs and compare every output byte.

python tests/repeatability.py --runs 10000 --fresh-processes 128 --report REPORT.json
Uses one fixed source/output path for the entire run. Source hooks are never run.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

CONVERTER = Path(__file__).resolve().parents[1] / 'converter/claude_to_codex.py'


def tree(root):
    result = []
    for p in sorted(root.rglob('*')):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            result.append([rel, 'symlink', os.readlink(p)])
        elif p.is_file():
            result.append([rel, 'file', p.stat().st_mode & 0o777,
                           hashlib.sha256(p.read_bytes()).hexdigest()])
    return hashlib.sha256(json.dumps(result, separators=(',', ':')).encode()).hexdigest(), result


def fixture(root):
    files = {
        '.claude/CLAUDE.md': '# Policy\nRead @../docs/policy.md and retain all details.\n',
        'src/CLAUDE.md': 'Directory-specific instructions retained.\n',
        'src/deeper/CLAUDE.local.md': 'Deeper instructions retained.\n',
        'docs/policy.md': 'Nested instructions: @nested.md\n',
        'docs/nested.md': 'UTF-8: café — 日本語. Do not omit this line.\n',
        '.claude/agents/reviewer.md': '---\nname: reviewer\ndescription: Review changes\nmodel: inherit\ntools: Read, Bash\ncustom_z: true\ncustom_a: true\n---\nReview all changes.\n',
        '.claude/skills/check/SKILL.md': '---\nname: check\ndescription: Verify the result\ncustom_z: true\ncustom_a: true\nhooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n        - type: command\n          command: python3 .claude/hooks/check.py\n---\nRead references/help.md.\n',
        '.claude/skills/check/references/help.md': 'A resource that must survive unchanged.\n',
        '.claude/commands/inspect.md': '---\ndescription: Inspect a named input\n---\nInspect $ARGUMENTS without implicit execution.\n',
        '.claude/hooks/check.py': 'from pathlib import Path\nPath("HOOK_MUST_NOT_RUN").write_text("bad")\n',
        '.claude/rules/scoped.md': '---\npaths: ["src/**"]\n---\nA scoped rule.\n',
        '.claude/hooks/.observe': 'enabled\n',
        '.claude/.gitignore': 'local-only\n',
        '.claude/old.bak': 'preserve as archive\n',
        '.mcp.json': json.dumps({'mcpServers': {'fixture': {'command': 'python3', 'args': ['.claude/hooks/check.py']}}}),
    }
    settings = {'permissions': {'allow': ['Bash(git:*)'], 'ask': ['Bash(sudo *)', 'Edit(/review/**)'],
                               'deny': ['Bash(git push *)', 'Read(./secret/**)', 'mcp__fixture__delete']},
                'hooks': {event: [{'hooks': [{'type': 'command', 'command': 'python3 .claude/hooks/check.py', 'timeout': 120}]}]
                          for event in ('PostToolUseFailure', 'SessionEnd', 'Interrupt')},
                'statusLine': {'type': 'command', 'command': 'python3 .claude/hooks/check.py'},
                'outputStyle': 'Concise', 'custom_z': True, 'custom_a': True,
                'env': {'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS': '4'}}
    files['.claude/settings.json'] = json.dumps(settings)
    for name, value in files.items():
        p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(value)
    (root / '.claude/skills/check/asset.bin').write_bytes(bytes(range(256)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, default=10000)
    parser.add_argument('--fresh-processes', type=int, default=128)
    parser.add_argument('--native-status', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1 or args.fresh_processes < 0:
        raise SystemExit('Invalid repetition count.')
    spec = importlib.util.spec_from_file_location('converter_under_test', CONVERTER)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    started = time.monotonic()
    report = {'requested_runs': args.runs, 'identical_runs': 0, 'fresh_process_checks': 0,
              'mismatches': [], 'comparison': 'All relative file paths, regular file bytes, modes, symlink targets, and CLI JSON output; one fixed input/output location. Filesystem timestamps excluded.',
              'model_responses_tested': False, 'third_party_hook_execution_tested': False,
              'native_status_config': args.native_status, 'native_binary_build_tested': False}
    try:
        with tempfile.TemporaryDirectory(prefix='converter-repeatability-') as directory:
            root = Path(directory).resolve(); source = root / 'source project'; output = root / 'output project'
            fixture(source)
            global_settings = root / 'global.json'; global_settings.write_text('{}')
            source_before = tree(source)[0]
            options = SimpleNamespace(source=source, output=output, global_settings=global_settings,
                                      model_map=[], include_external_hooks=False, include_user_resources=False,
                                      strict=False, native_status=args.native_status)
            baseline = None
            for n in range(args.runs):
                if output.exists():shutil.rmtree(output)
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    status = module.Converter(options).run()
                actual = (status, tree(output)[0], captured.getvalue())
                if baseline is None:baseline = actual
                if actual != baseline:
                    report['mismatches'].append({'run': n + 1, 'tree_sha256': actual[1]})
                    raise AssertionError('Conversion output changed for identical input.')
                report['identical_runs'] += 1
                if (n + 1) % 250 == 0:
                    print(json.dumps({'identical_runs': n + 1, 'elapsed_seconds': round(time.monotonic()-started, 2)}), flush=True)
            for seed in range(args.fresh_processes):
                shutil.rmtree(output)
                env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONDONTWRITEBYTECODE='1')
                result = subprocess.run([sys.executable, str(CONVERTER), str(source), '--output', str(output), '--global-settings', str(global_settings),
                                         *(['--native-status'] if args.native_status else [])],
                                        env=env, capture_output=True, text=True, timeout=30)
                actual = (result.returncode, tree(output)[0], result.stdout)
                if actual != baseline:
                    report['mismatches'].append({'hash_seed': seed, 'tree_sha256': actual[1]})
                    raise AssertionError('Conversion output changed in a fresh Python process.')
                report['fresh_process_checks'] += 1
            report.update(source_unchanged=tree(source)[0] == source_before,
                          source_hooks_executed=(source / 'HOOK_MUST_NOT_RUN').exists() or (output / 'HOOK_MUST_NOT_RUN').exists(),
                          artifact_tree_sha256=baseline[1], output_file_count=len(tree(output)[1]))
            if not report['source_unchanged'] or report['source_hooks_executed']:
                raise AssertionError('Conversion mutated source or executed a hook.')
            report['passed'] = True
    except Exception as exc:
        report.update(passed=False, error=str(exc))
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['converter_sha256'] = hashlib.sha256(CONVERTER.read_bytes()).hexdigest()
        report['code_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(CONVERTER.parent.glob('*.py'))}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
