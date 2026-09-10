"""Independent converter regressions; all source hooks and files are synthetic.

Run with PYTHONPATH=/tmp/cue-converter-deps python3 -m unittest discover \
    -s .cue/tests -p test_converter_regressions.py -v
Baseline reproductions are retained in converter-independent-review.md.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest

CONVERTER = Path(__file__).resolve().parents[1] / 'converter/claude_to_codex.py'


class ConverterRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='converter-regression-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.output = self.base / 'out'
        (self.source / '.claude').mkdir(parents=True)
        self.global_settings = self.base / 'global-settings.json'
        self.global_settings.write_text('{}')
        self.env = dict(os.environ, CUE_STATE_ROOT=str(self.base / 'state'), PYTHONDONTWRITEBYTECODE='1')

    def write(self, name, value):
        target = self.source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)

    def convert(self, settings=None):
        self.write('.claude/settings.json', json.dumps(settings or {}))
        result = subprocess.run([sys.executable, str(CONVERTER), str(self.source), '--output', str(self.output),
                                 '--global-settings', str(self.global_settings)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.output

    def event(self, event, command='echo harmless', **extra):
        data = {'hook_event_name': event, 'tool_name': 'exec_command',
                'tool_input': {'cmd': command}, 'session_id': 'synthetic-session',
                'cwd': str(self.output.resolve()), **extra}
        result = subprocess.run([sys.executable, str(self.output / '.cue/scripts/converted_hook.py')],
                                input=json.dumps(data), capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_denied(self, value):
        hs = value.get('hookSpecificOutput', {})
        self.assertTrue(value.get('decision') == 'block' or hs.get('permissionDecision') == 'deny'
                        or hs.get('decision', {}).get('behavior') == 'deny', value)

    def test_translated_source_path_collision_cannot_silently_overwrite(self):
        self.write('.claude/CLAUDE.md', 'first source')
        self.write('.claude/INSTRUCTIONS.md', 'second source')
        result = subprocess.run([sys.executable, str(CONVERTER), str(self.source), '--output', str(self.output),
                                 '--global-settings', str(self.global_settings)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('collision', result.stderr)

    def test_provider_store_path_components_match_preserved_home_reader(self):
        self.write('.claude/hooks/store.py', '''import os
reader = os.path.expanduser('~/.claude/tasks')
fixture = os.path.join(h, '.claude', 'tasks', name)
transcript = h / '.claude' / 'projects'
project_hook = os.path.join(project, '.claude', 'hooks', 'guard.py')
''')
        self.convert()
        text = (self.output / '.cue/hooks/store.py').read_text()
        self.assertIn("'~/.claude/tasks'", text)
        self.assertIn("h, '.claude', 'tasks'", text)
        self.assertIn("h / '.claude' / 'projects'", text)
        self.assertIn("project, '.cue', 'hooks'", text)

    def test_quoted_separator_and_redirect_filename_are_not_commands(self):
        self.convert({'permissions': {'deny': ['Bash(git push *)']}})
        for command in ("echo ';' git push origin main", "printf x > 'git push origin main'"):
            result = self.event('PreToolUse', command)
            self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny', result)

    def test_active_battery_observe_marker_is_not_archived_only(self):
        self.write('.claude/hooks/.battery-observe', '')
        self.convert()
        self.assertTrue((self.output / '.cue/hooks/.battery-observe').is_file())

    def test_bare_project_directory_operand_is_translated(self):
        self.write('.claude/hooks/commands.py', 'command = "rm -rf .claude"\nformatted = "rm -rf %s/.claude"\nvariable = "${ROOT}/.claude"\nhistory = "~/.claude"\n')
        self.convert()
        text = (self.output / '.cue/hooks/commands.py').read_text()
        self.assertIn('"rm -rf .cue"', text)
        self.assertIn('"~/.claude"', text)
        self.assertIn('"rm -rf %s/.cue"', text)
        self.assertIn('"${ROOT}/.cue"', text)

    def test_wildcard_deny_dominates_broader_allow(self):
        self.convert({'permissions': {'deny': ['Bash(echo *)'], 'allow': ['Bash(echo:*)']}})
        self.assert_denied(self.event('PreToolUse'))
        self.assert_denied(self.event('PermissionRequest'))

    def test_compound_command_cannot_evade_prefix_deny(self):
        self.convert({'permissions': {'deny': ['Bash(echo:*)']}})
        self.assert_denied(self.event('PreToolUse', 'echo harmless; true'))

    def test_exit_two_permission_veto_dominates_allow(self):
        self.convert({'permissions': {'allow': ['Bash(echo:*)']}, 'hooks': {'PermissionRequest': [
            {'hooks': [{'type': 'command', 'command': 'echo synthetic-veto >&2; exit 2'}]}]}})
        self.assert_denied(self.event('PermissionRequest'))

    def test_continue_false_is_not_silently_lost(self):
        payload = json.dumps({'continue': False, 'stopReason': 'synthetic-stop'})
        self.convert({'hooks': {'PreToolUse': [{'hooks': [{'type': 'command',
                      'command': 'printf %s ' + shlex.quote(payload)}]}]}})
        value = self.event('PreToolUse')
        if value.get('continue') is not False:
            self.assert_denied(value)
        self.assertIn('synthetic-stop', json.dumps(value))

    def test_stop_continuation_and_satisfied_state_preserve_source_semantics(self):
        self.write('.claude/hooks/stop_guard.py', '''import json
from pathlib import Path
import sys

event = json.load(sys.stdin)
if not event.get("stop_hook_active") and not Path("board-ok").exists():
    print(json.dumps({"decision": "block", "reason": "Create or bind the missing board."}))
''')
        self.convert({'hooks': {'Stop': [{'hooks': [{'type': 'command',
                      'command': 'python3 .claude/hooks/stop_guard.py'}]}]}})
        first = self.event('Stop', stop_hook_active=False)
        self.assertEqual(first.get('decision'), 'block', first)
        self.assertIn('missing board', first.get('reason', ''))
        # Codex marks an automatically continued Stop. The source guard owns its
        # re-entry policy, and the adapter must pass that field through unchanged.
        self.assertEqual(self.event('Stop', stop_hook_active=True), {})
        (self.output / 'board-ok').write_text('bound\n')
        self.assertEqual(self.event('Stop', stop_hook_active=False), {})

    def test_stop_warning_is_visible_without_becoming_a_continuation(self):
        payload = json.dumps({'systemMessage': 'Synthetic source warning.'})
        self.convert({'hooks': {'Stop': [{'hooks': [{'type': 'command',
                      'command': 'printf %s ' + shlex.quote(payload)}]}]}})
        value = self.event('Stop', stop_hook_active=False)
        self.assertEqual(value.get('systemMessage'), 'Synthetic source warning.')
        self.assertNotIn('decision', value)

    def test_skill_activation_has_observer_and_enforces_scope(self):
        self.write('.claude/skills/guard/SKILL.md', '''---
name: guard
description: Synthetic guard.
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: "echo synthetic-veto >&2; exit 2"
---
Synthetic instructions.
''')
        self.convert()
        hooks = json.loads((self.output / '.codex/hooks.json').read_text())['hooks']
        self.assertIn('PostToolUse', hooks)
        self.assertEqual(self.event('PreToolUse'), {})
        self.event('PostToolUse', 'python3 .cue/scripts/activate_skill.py guard', tool_response={'exit_code': 0})
        self.assert_denied(self.event('PreToolUse'))

    def test_renamed_skill_retains_relative_resources(self):
        self.write('.claude/skills/original/SKILL.md', '---\nname: renamed\ndescription: Synthetic skill.\n---\nRead references/info.txt.\n')
        self.write('.claude/skills/original/references/info.txt', 'synthetic-resource')
        self.convert()
        self.assertEqual((self.output / '.agents/skills/renamed/references/info.txt').read_text(), 'synthetic-resource')

    def test_overlapping_directory_permission_produces_valid_restriction(self):
        restricted = str(self.base / 'external')
        self.convert({'permissions': {'additionalDirectories': [restricted], 'deny': ['Edit(/' + restricted + ')']}})
        config = tomllib.loads((self.output / '.codex/config.toml').read_text())
        filesystem = config['permissions']['converted']['filesystem']
        self.assertIn(filesystem[restricted], ('read', 'deny'))

    def test_mcp_project_path_targets_converted_file(self):
        self.write('.claude/server.py', '# synthetic server; never executed\n')
        self.write('.mcp.json', json.dumps({'mcpServers': {'local': {'command': 'python3', 'args': ['.claude/server.py']}}}))
        self.convert()
        config = tomllib.loads((self.output / '.codex/config.toml').read_text())
        arg = config['mcp_servers']['local']['args'][0]
        target = Path(arg) if Path(arg).is_absolute() else self.output / arg
        self.assertTrue(target.is_file(), arg)
        self.assertIn('.cue', str(target))

    def test_manifest_hashes_describe_final_files(self):
        self.write('.claude/skills/example/SKILL.md', '---\nname: example\ndescription: Synthetic skill.\n---\nHello\n')
        self.write('.claude/scripts/protocol.py', '# synthetic original protocol\n')
        self.convert()
        manifest = json.loads((self.output / '.cue/file-manifest.json').read_text())
        for row in manifest:
            if row.get('target'):
                with self.subTest(target=row['target']):
                    actual = hashlib.sha256((self.output / row['target']).read_bytes()).hexdigest()
                    self.assertEqual(row['target_sha256'], actual)

    def test_symlink_marked_unfollowed_is_not_inlined(self):
        external = self.base / 'external.txt'
        external.write_text('SYNTHETIC_EXTERNAL_MARKER')
        (self.source / '.claude/CLAUDE.md').symlink_to(external)
        self.convert()
        manifest = json.loads((self.output / '.cue/file-manifest.json').read_text())
        unfollowed = any(row.get('kind') == 'symlink' and row.get('status') == 'not-followed' for row in manifest)
        self.assertTrue(unfollowed)
        self.assertNotIn('SYNTHETIC_EXTERNAL_MARKER', (self.output / 'AGENTS.md').read_text())

    def test_rewrite_cannot_upgrade_ask_or_bypass_source_deny(self):
        payload = {'hookSpecificOutput': {'permissionDecision': 'ask',
                                         'updatedInput': {'command': 'echo forbidden'}}}
        self.convert({'permissions': {'deny': ['Bash(echo forbidden)']}, 'hooks': {'PreToolUse': [
            {'hooks': [{'type': 'command', 'command': 'printf %s ' + shlex.quote(json.dumps(payload))}]}]}})
        value = self.event('PreToolUse', 'echo harmless')
        self.assert_denied(value)

    def test_newline_command_cannot_evade_exact_deny(self):
        self.convert({'permissions': {'deny': ['Bash(echo forbidden)']}})
        self.assert_denied(self.event('PreToolUse', 'true\necho forbidden'))

    def test_archive_namespace_cannot_overwrite_source_archive(self):
        self.write('.claude/foo.txt', '.claude/original')
        self.write('.claude/source-archive/foo.txt', 'synthetic-collision')
        result = subprocess.run([sys.executable, str(CONVERTER), str(self.source),
                                 '--output', str(self.output), '--global-settings', str(self.global_settings)],
                                env=self.env, capture_output=True, text=True)
        if result.returncode:
            self.assertIn('source-archive', result.stderr)
            return
        manifest = json.loads((self.output / '.cue/file-manifest.json').read_text())
        for row in manifest:
            if row.get('archive'):
                with self.subTest(archive=row['archive']):
                    self.assertEqual(row['sha256'], hashlib.sha256((self.output / row['archive']).read_bytes()).hexdigest())

    def test_final_gitignore_hash_is_accurate(self):
        self.write('.claude/.gitignore', 'synthetic-custom-ignore\n')
        self.convert()
        manifest = json.loads((self.output / '.cue/file-manifest.json').read_text())
        row = next(row for row in manifest if row.get('source_relative') == '.gitignore')
        self.assertEqual(row['target_sha256'], hashlib.sha256((self.output / row['target']).read_bytes()).hexdigest())

    def test_directory_symlink_rules_are_not_inlined(self):
        external = self.base / 'external-rules'
        external.mkdir()
        (external / 'external.md').write_text('SYNTHETIC_EXTERNAL_RULE')
        (self.source / '.claude/rules').symlink_to(external, target_is_directory=True)
        self.convert()
        self.assertNotIn('SYNTHETIC_EXTERNAL_RULE', (self.output / 'AGENTS.md').read_text())

    def test_inline_and_rule_imports_are_inlined_or_reported(self):
        self.write('CLAUDE.md', 'Follow @docs/synthetic.md before work.\n')
        self.write('docs/synthetic.md', 'SYNTHETIC_ROOT_IMPORT')
        self.write('.claude/rules/import.md', '@../guidance.md\n')
        self.write('.claude/guidance.md', 'SYNTHETIC_RULE_IMPORT')
        self.convert()
        findings = json.loads((self.output / '.cue/conversion-findings.json').read_text())
        imports = [row for row in findings if row['category'] == 'instruction-import']
        instructions = (self.output / 'AGENTS.md').read_text()
        for source, marker in [('CLAUDE.md', 'SYNTHETIC_ROOT_IMPORT'),
                               ('rules/import.md', 'SYNTHETIC_RULE_IMPORT')]:
            with self.subTest(source=source):
                self.assertTrue(marker in instructions or any(row['source'].endswith(source) for row in imports),
                                'Import was neither inlined nor reported: ' + source)


if __name__ == '__main__':
    unittest.main()
