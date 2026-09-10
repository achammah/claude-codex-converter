"""Explicit user and nested instruction coverage; synthetic files only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

CONVERTER = Path(__file__).resolve().parents[1] / 'converter'
spec = importlib.util.spec_from_file_location('instruction_sources_under_test', CONVERTER / 'instruction_sources.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def frontmatter(content, source):
    if content.startswith('---\n'):
        header, body = content[4:].split('\n---\n', 1)
        return yaml.safe_load(header), body
    return {}, content


class InstructionSources(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cue-instruction-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'project'
        self.output = self.base / 'output'
        self.user = self.base / 'user-config'
        for path in (self.project, self.output, self.user):
            path.mkdir()
        self.findings = []

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def finding(self, category, source, rule, status='converted', **detail):
        self.findings.append(dict(category=category, source=str(source), rule=rule, status=status, **detail))

    def collect(self, user=False):
        return module.collect_instruction_supplements(
            self.project, self.output, user_root=self.user if user else None,
            finding=self.finding, frontmatter=frontmatter, render=lambda path, body: body)

    def test_nested_content_remains_in_directory_scope_and_is_archived(self):
        source = self.project / 'packages/api/CLAUDE.md'
        self.write(source, 'API_ONLY_INSTRUCTION')
        result = self.collect()
        self.assertEqual(result['root_sections'], [])
        self.assertIn('API_ONLY_INSTRUCTION', (self.output / 'packages/api/AGENTS.md').read_text())
        archive = self.output / result['archives'][str(source)]
        self.assertEqual(archive.read_bytes(), source.read_bytes())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertGreater(result['nested_max_bytes'], 0)

    def test_explicit_user_rules_retain_path_applicability(self):
        self.write(self.user / 'CLAUDE.md', 'USER_INSTRUCTION')
        self.write(self.user / 'rules/api.md', '---\npaths:\n  - src/api/**\n---\nAPI_RULE')
        result = self.collect(user=True)
        self.assertEqual(result['root_sections'][0], 'USER_INSTRUCTION')
        self.assertIn('src/api/**', result['root_sections'][1])
        self.assertIn('API_RULE', result['root_sections'][1])
        self.assertTrue(any(row['category'] == 'scoped-rule' for row in self.findings))

    def test_user_resources_are_not_implicitly_loaded(self):
        self.write(self.user / 'CLAUDE.md', 'UNREQUESTED_USER_INSTRUCTION')
        self.assertEqual(self.collect()['root_sections'], [])

    def test_nested_claude_directory_symlink_is_not_traversed(self):
        self.write(self.user / 'rules/secret.md', 'EXTERNAL_SYNTHETIC_MARKER')
        nested = self.project / 'nested'
        nested.mkdir()
        (nested / '.claude').symlink_to(self.user, target_is_directory=True)
        self.collect()
        self.assertFalse((self.output / 'nested/AGENTS.md').exists())
        self.assertTrue(any(row['category'] == 'instruction-symlink' for row in self.findings))

    def test_existing_destination_is_never_overwritten(self):
        self.write(self.project / 'nested/CLAUDE.md', 'CONVERTED')
        self.write(self.output / 'nested/AGENTS.md', 'EXISTING')
        with self.assertRaises(ValueError):
            self.collect()
        self.assertEqual((self.output / 'nested/AGENTS.md').read_text(), 'EXISTING')

    def test_destination_symlink_cannot_redirect_writes(self):
        self.write(self.project / 'nested/CLAUDE.md', 'CONVERTED')
        (self.output / 'nested').symlink_to(self.user, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.collect()
        self.assertFalse((self.user / 'AGENTS.md').exists())

    def test_existing_native_instructions_require_precedence_review(self):
        self.write(self.project / 'nested/CLAUDE.md', 'CLAUDE_INSTRUCTION')
        self.write(self.project / 'nested/AGENTS.md', 'EXISTING_NATIVE_INSTRUCTION')
        self.collect()
        self.assertTrue(any(row['category'] == 'instruction-precedence' and
                            row['source'].endswith('/nested/AGENTS.md') for row in self.findings))
        self.assertEqual((self.project / 'nested/AGENTS.md').read_text(), 'EXISTING_NATIVE_INSTRUCTION')

    def test_nested_rules_stay_scoped_and_keep_unknown_metadata_visible(self):
        self.write(self.project / 'nested/.claude/rules/test.md',
                   '---\npaths: [test/**]\ncustomFlag: true\n---\nNESTED_RULE')
        self.collect()
        content = (self.output / 'nested/AGENTS.md').read_text()
        self.assertIn('test/**', content)
        self.assertIn('NESTED_RULE', content)
        self.assertTrue(any(row['category'] == 'instruction-metadata' for row in self.findings))

    def test_document_budget_counts_nested_ancestor_chain(self):
        self.write(self.project / 'outer/CLAUDE.md', 'PARENT_INSTRUCTION')
        self.write(self.project / 'outer/inner/CLAUDE.md', 'CHILD_INSTRUCTION')
        result = self.collect()
        expected = sum((self.output / name / 'AGENTS.md').stat().st_size for name in ('outer', 'outer/inner'))
        self.assertEqual(result['nested_max_bytes'], expected)


class InstructionConversionIntegration(unittest.TestCase):
    """The helper must be connected to normal conversion, not only unit callable."""
    setUp = InstructionSources.setUp
    write = InstructionSources.write

    def run_conversion(self, include_user=False):
        settings = self.user / 'settings.json'
        settings.write_text('{}')
        command = [sys.executable, str(CONVERTER / 'claude_to_codex.py'), str(self.project),
                   '--output', str(self.output), '--global-settings', str(settings)]
        if include_user:
            command.append('--include-user-resources')
        result = subprocess.run(command, text=True, capture_output=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_instruction_only_project_is_convertible(self):
        self.write(self.project / 'CLAUDE.md', 'INSTRUCTION_ONLY_PROJECT')
        self.run_conversion()
        self.assertIn('INSTRUCTION_ONLY_PROJECT', (self.output / 'AGENTS.md').read_text())

    def test_full_conversion_includes_explicit_user_rules_and_nested_scope(self):
        (self.project / '.claude').mkdir()
        self.write(self.user / 'CLAUDE.md', 'EXPLICIT_USER_INSTRUCTION')
        self.write(self.user / 'rules/scoped.md', '---\npaths: [api/**]\n---\nEXPLICIT_USER_RULE')
        self.write(self.project / 'nested/CLAUDE.md', 'SCOPED_NESTED_INSTRUCTION')
        self.run_conversion(include_user=True)
        root = (self.output / 'AGENTS.md').read_text()
        self.assertIn('EXPLICIT_USER_INSTRUCTION', root)
        self.assertIn('EXPLICIT_USER_RULE', root)
        self.assertIn('api/**', root)
        self.assertNotIn('SCOPED_NESTED_INSTRUCTION', root)
        self.assertIn('SCOPED_NESTED_INSTRUCTION', (self.output / 'nested/AGENTS.md').read_text())


if __name__ == '__main__':
    unittest.main()
