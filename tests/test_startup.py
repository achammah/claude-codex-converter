"""Startup regressions against generated projects; no source programs execute."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

import yaml

PACKAGE = Path(__file__).resolve().parents[1]
CONVERTER = PACKAGE / 'converter/claude_to_codex.py'


class StartupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cue-startup-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.output = self.root / 'target'
        self.global_settings = self.root / 'global-settings.json'
        self.global_settings.write_text('{}')
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')

    def write(self, path, content):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def test_release_version_has_one_runtime_source(self):
        from converter import __version__
        from converter.claude_to_codex import VERSION as manifest_version
        from converter.version import VERSION

        metadata = tomllib.loads((PACKAGE / 'pyproject.toml').read_text())
        self.assertEqual(__version__, VERSION)
        self.assertEqual(manifest_version, VERSION)
        self.assertIn('version', metadata['project']['dynamic'])
        self.assertEqual(metadata['tool']['setuptools']['dynamic']['version']['attr'],
                         'cue_converter.version.VERSION')
        result = subprocess.run(
            [sys.executable, '-c', 'from converter.cli import main; raise SystemExit(main())', '--help'],
            cwd=PACKAGE, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(VERSION, result.stdout)

    def convert(self, settings=None, strict=False):
        self.write('.claude/settings.json', json.dumps(settings or {}))
        command = [sys.executable, str(CONVERTER), str(self.source), '--output', str(self.output),
                   '--global-settings', str(self.global_settings)]
        if strict:
            command.append('--strict')
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2 if strict else 0, result.stderr)
        return tomllib.loads((self.output / '.codex/config.toml').read_text())

    def test_end_and_interrupt_fit_native_limit_without_rewriting_source_timeouts(self):
        original = {'hooks': {event: [{'hooks': [{'type': 'command', 'command': 'touch source-must-not-run', 'timeout': seconds}]}]
                              for event, seconds in [('SessionEnd', 120), ('Interrupt', 10), ('Stop', 90)]}}
        self.convert(original)
        hooks = json.loads((self.output / '.codex/hooks.json').read_text())['hooks']
        for event in ('SessionEnd', 'Interrupt'):
            self.assertEqual(hooks[event][0]['hooks'][0]['timeout'], 3)
        self.assertEqual(hooks['Stop'][0]['hooks'][0]['timeout'], 120)
        routes = json.loads((self.output / '.cue/hook-routes.json').read_text())
        self.assertEqual(routes['SessionEnd'][0]['handler']['timeout'], 120)
        self.assertEqual(routes['Interrupt'][0]['handler']['timeout'], 10)
        self.assertEqual(json.loads((self.output / '.cue-source-archive/project/settings.json').read_text()), original)
        self.assertFalse((self.source / 'source-must-not-run').exists())
        self.assertFalse((self.output / 'source-must-not-run').exists())

    def test_strict_report_does_not_claim_long_source_lifecycle_work_is_preserved(self):
        self.convert({'hooks': {'SessionEnd': [{'hooks': [
            {'type': 'command', 'command': 'echo first', 'timeout': 2},
            {'type': 'command', 'command': 'echo second', 'timeout': 2}]}]}}, strict=True)
        findings = json.loads((self.output / '.cue/conversion-findings.json').read_text())
        budget = next(row for row in findings if row['category'] == 'hook-lifecycle-budget')
        self.assertEqual(budget['status'], 'manual')
        self.assertEqual(budget['native_timeout_seconds'], 3)
        self.assertEqual(budget['source_timeouts'], [2, 2])
        self.assertIn('all matching source handlers', budget['rule'])

    def test_skill_budget_preserves_complete_catalog_descriptions_and_bodies(self):
        descriptions = {}
        for index in range(12):
            name = 'skill-' + str(index)
            description = ('Choose this skill for the complete input and output workflow. ' * 12).strip()
            descriptions[name] = description
            self.write('.claude/skills/' + name + '/SKILL.md',
                       '---\n' + yaml.safe_dump({'name': name, 'description': description}) +
                       '---\n\nRead references/complete.md before acting.\n')
            self.write('.claude/skills/' + name + '/references/complete.md', 'COMPLETE_RESOURCE_' + name)
        config = self.convert()
        self.assertEqual(config['skills']['max_context_tokens'], 10000)
        for name, description in descriptions.items():
            skill = self.output / '.agents/skills' / name
            content = (skill / 'SKILL.md').read_text()
            self.assertEqual(yaml.safe_load(content.split('---', 2)[1])['description'], description)
            self.assertIn('Read references/complete.md before acting.', content)
            self.assertEqual((skill / 'references/complete.md').read_text(), 'COMPLETE_RESOURCE_' + name)

    def test_project_persona_stays_in_agents_without_config_instruction_injection(self):
        instruction = '# Working role\nYou are Beacon, the project assistant.\n'
        self.write('CLAUDE.md', instruction)
        config = self.convert()
        agents = (self.output / 'AGENTS.md').read_text()
        self.assertIn(instruction, agents)
        self.assertIn('Use the working name and role defined in the source project instructions', agents)
        self.assertIn('underlying host, provider, or model, identify them accurately', agents)
        self.assertNotIn('developer_instructions', config)
        self.assertNotIn('model_instructions_file', config)

    def test_doctor_public_entrypoint_runs_offline_and_reports_target_configuration(self):
        self.convert()
        env = dict(self.env, PYTHONPATH=os.pathsep.join([str(PACKAGE), self.env.get('PYTHONPATH', '')]))
        result = subprocess.run([sys.executable, '-c', 'from converter.cli import main; raise SystemExit(main())',
                                 'doctor', str(self.output)], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['offline_checks_passed'])
        self.assertFalse(report['native_inventory_checked'])
        self.assertFalse(report['hooks_executed'])
        self.assertFalse(report['mcp_servers_started'])
        self.assertEqual(report['runtime_readiness'], 'not_verified')

    def test_permission_anchors_keep_global_project_and_absolute_roots_distinct(self):
        global_rule = 'Read(/user-private/**)'
        self.global_settings.write_text(json.dumps({'permissions': {'deny': [global_rule]}}))
        absolute = self.root / 'absolute-private'
        config = self.convert({'permissions': {'deny': ['Read(/project-private/**)', 'Read(/' + str(absolute) + '/**)']}})
        filesystem = config['permissions']['converted']['filesystem']
        self.assertEqual(filesystem[str(self.root / 'user-private/**')], 'deny')
        self.assertEqual(filesystem[str(self.output / 'project-private/**')], 'deny')
        self.assertEqual(filesystem[str(absolute / '**')], 'deny')
        self.assertNotIn('/project-private/**', filesystem)
        rows = json.loads((self.output / '.cue/permission-rules.json').read_text())
        inherited = next(row for row in rows if row['original_rule'] == global_rule)
        self.assertEqual(inherited['source_root'], str(self.global_settings.parent.resolve()))
        self.assertEqual(inherited['action'], 'deny')

    def test_same_rule_in_different_settings_sources_keeps_both_roots(self):
        rule = 'Read(/private/**)'
        self.global_settings.write_text(json.dumps({'permissions': {'deny': [rule]}}))
        config = self.convert({'permissions': {'deny': [rule]}})
        filesystem = config['permissions']['converted']['filesystem']
        self.assertEqual(filesystem[str(self.root / 'private/**')], 'deny')
        self.assertEqual(filesystem[str(self.output / 'private/**')], 'deny')
        rows = json.loads((self.output / '.cue/permission-rules.json').read_text())
        self.assertEqual(len([row for row in rows if row['original_rule'] == rule]), 2)

    def test_additional_directory_is_target_relative_and_unknown_controls_stay_visible(self):
        config = self.convert({'permissions': {'additionalDirectories': ['../shared'], 'disableBypassPermissionsMode': 'disable'},
                               'sandbox': {'network': {'allowedDomains': ['example.test']}, 'enabled': True}})
        self.assertEqual(config['permissions']['converted']['filesystem'][str(self.root / 'shared')], 'write')
        findings = json.loads((self.output / '.cue/conversion-findings.json').read_text())
        paths = {row['source'] for row in findings if row['status'] == 'manual'}
        self.assertIn('effective-settings/permissions/disableBypassPermissionsMode', paths)
        self.assertIn('effective-settings/sandbox/network/allowedDomains', paths)
        self.assertIn('effective-settings/sandbox/enabled', paths)

    def test_source_permission_mode_is_not_silently_declared_native_equivalent(self):
        self.convert({'permissions': {'defaultMode': 'plan'}}, strict=True)
        findings = json.loads((self.output / '.cue/conversion-findings.json').read_text())
        mode = next(row for row in findings if row['category'] == 'permission-mode')
        self.assertEqual(mode['status'], 'manual')
        self.assertEqual(mode['mode'], 'plan')
        self.assertIn('plan and dontAsk restrictions are not reproduced', mode['rule'])

    def test_malformed_permission_list_fails_before_runtime_generation(self):
        self.write('.claude/settings.json', json.dumps({'permissions': {'deny': 'Read'}}))
        result = subprocess.run([sys.executable, str(CONVERTER), str(self.source), '--output', str(self.output),
                                 '--global-settings', str(self.global_settings)],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('must be a list of strings', result.stderr)
        self.assertFalse((self.output / '.codex/config.toml').exists())


if __name__ == '__main__':
    unittest.main()
