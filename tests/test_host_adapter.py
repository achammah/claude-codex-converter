"""Claude host staging tests. Synthetic neutral files; no host or hook is run."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

import yaml

MODULES = Path(__file__).resolve().parents[1] / 'converter'


def load(name):
    spec = importlib.util.spec_from_file_location('test_' + name, MODULES / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cue-host-stage-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'shared project'
        self.output = self.base / 'stage'
        self.root.mkdir()
        self.adapter = load('host_adapter')
        self.write('.cue/shared-instructions.md', 'SHARED_DOCTRINE\n')
        self.write('.cue/conversion-report.json', json.dumps({'source': '/original/project/.claude'}))
        self.write('.cue/effective-settings.json', json.dumps({
            'disableAllHooks': True, 'disabledMcpjsonServers': ['blocked'],
            'model': 'opus', 'env': {'SYNTHETIC_ENV': 'retained'},
            'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
                {'type': 'command', 'command': 'python3 /original/project/.claude/hooks/guard.py', 'timeout': 17}]}]}}))
        self.write('.cue/hooks/guard.py', 'raise AssertionError("Source hooks must never execute during staging")\n')

    def write(self, relative, content):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def stage(self, target=None):
        return self.adapter.stage_claude(self.root, self.output, target)

    def source_hashes(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*') if path.is_file() and not path.is_symlink()}

    def installed_link_destination(self, relative, target=None):
        return ((target or self.root) / relative).parent / os.readlink(self.output / relative)

    def test_settings_and_mcp_keep_source_contract_with_correct_project_paths(self):
        self.write('.cue/mcp-source.json', json.dumps({'blocked': {'command': 'python3',
                    'args': ['/original/project/.claude/server.py']}}))
        before = self.source_hashes()
        report = self.stage()
        settings = json.loads((self.output / '.claude/settings.json').read_text())
        self.assertIs(settings['disableAllHooks'], True)
        self.assertEqual(settings['disabledMcpjsonServers'], ['blocked'])
        self.assertEqual(settings['model'], 'opus')
        self.assertEqual(settings['env']['CUE_PROJECT_DIR'], str(self.root))
        self.assertEqual(settings['env']['CUE_STATE_ROOT'], str(self.root / '.cue/state/claude'))
        handler = settings['hooks']['PreToolUse'][0]['hooks'][0]
        self.assertEqual(handler['timeout'], 17)
        self.assertEqual(handler['command'], 'python3 ' + str(self.root / '.cue/hooks/guard.py'))
        server = json.loads((self.output / '.mcp.json').read_text())['mcpServers']['blocked']
        self.assertEqual(server['args'], [str(self.root / '.cue/server.py')])
        self.assertEqual(report['state'], 'staged')
        self.assertEqual(json.loads((self.output / '.claude/settings.local.json').read_text()), {})
        self.assertEqual((self.output / '.claude/CLAUDE.md').read_text(), '')
        self.assertEqual((self.output / 'CLAUDE.local.md').read_text(), '')
        self.assertTrue(any('duplicating hooks' in finding for finding in report['findings']))
        self.assertEqual(before, self.source_hashes())

    def test_skill_restores_metadata_and_links_shared_resources(self):
        metadata = {'name': 'build', 'description': 'Build fixture.', 'allowed-tools': ['Read'],
                    'context': 'fork', 'model': 'sonnet', 'disable-model-invocation': True,
                    'hooks': {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'echo synthetic'}]}]}}
        self.write('.cue/metadata/skills/build.json', json.dumps(metadata))
        self.write('.cue/skills/build/SKILL.md', '---\nname: build\ndescription: Build fixture.\n---\n\n'
                   'Before using this skill, run `python3 .cue/scripts/activate_skill.py build` so its scoped hooks apply.\n\n'
                   'Use references/spec.md.\n')
        resource = self.write('.cue/skills/build/references/spec.md', 'SHARED_RESOURCE')
        self.stage()
        skill = (self.output / '.claude/skills/build/SKILL.md').read_text()
        restored, body = self.adapter._frontmatter(skill, 'staged skill')
        self.assertEqual(restored, metadata)
        self.assertNotIn('activate_skill.py', body)
        self.assertIn('references/spec.md', body)
        relative = '.claude/skills/build/references'
        self.assertTrue((self.output / relative).is_symlink())
        self.assertEqual((self.installed_link_destination(relative) / 'spec.md').resolve(), resource)

    def test_agent_retains_original_full_metadata_through_stable_link(self):
        original = '---\nname: reviewer\ndescription: Review fixture.\nmodel: opus\npermissionMode: plan\ntools: [Read]\n---\nREVIEW_INSTRUCTION\n'
        source = self.write('.cue/agents/reviewer.md', original)
        self.stage()
        relative = '.claude/agents/reviewer.md'
        self.assertTrue((self.output / relative).is_symlink())
        self.assertEqual(self.installed_link_destination(relative).resolve(), source)
        self.assertEqual(source.read_text(), original)

    def test_codex_prelude_is_never_used_as_fallback_doctrine(self):
        (self.root / '.cue/shared-instructions.md').unlink()
        self.write('AGENTS.md', 'CODEX_ONLY_PRELUDE')
        with self.assertRaisesRegex(ValueError, 'shared-instructions'):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_staging_inside_shared_source_is_refused(self):
        before = self.source_hashes()
        with self.assertRaises(ValueError):
            self.adapter.stage_claude(self.root, self.root / 'stage')
        self.assertEqual(before, self.source_hashes())

    def test_symlinked_doctrine_is_refused_without_following_content(self):
        shared = self.root / '.cue/shared-instructions.md'
        shared.unlink()
        external = self.base / 'external.txt'
        external.write_text('EXTERNAL_SYNTHETIC_DOCTRINE')
        shared.symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_links_are_compatible_with_installer_review_plan(self):
        self.stage()
        installer = load('install')
        plan = self.base / 'install-plan.json'
        installer.plan(self.output, self.root, plan)
        entries = json.loads(plan.read_text())['entries']
        doctrine = next(row for row in entries if row['path'] == 'CLAUDE.md')
        self.assertEqual(doctrine['after']['kind'], 'symlink')
        self.assertEqual(doctrine['after']['link'], '.cue/shared-instructions.md')
        self.assertFalse((self.root / 'CLAUDE.md').exists())  # planning did not apply

    def test_commands_are_not_registered_twice_as_skills(self):
        self.write('.cue/commands/check.md', '---\nname: check\ndescription: Check fixture.\n---\nRun $ARGUMENTS.\n')
        self.write('.cue/metadata/skills/check.json', json.dumps({'name': 'check', 'description': 'Check fixture.'}))
        self.stage()
        self.assertTrue((self.output / '.claude/commands').is_symlink())
        self.assertFalse((self.output / '.claude/skills/check').exists())

    def test_inherited_command_map_binds_native_command_without_duplicate_skill(self):
        source = self.write('.cue/vendor/user/commands/audit.md',
                            '---\nname: audit\ndescription: Synthetic inherited audit.\n---\nAudit $ARGUMENTS.\n')
        self.write('.cue/command-skill-map.json', json.dumps({'audit': '.cue/vendor/user/commands/audit.md'}))
        self.write('.cue/metadata/skills/audit.json', json.dumps({'name': 'audit', 'description': 'Synthetic inherited audit.'}))
        self.stage()
        relative = '.claude/commands/audit.md'
        self.assertTrue((self.output / relative).is_symlink())
        self.assertEqual(self.installed_link_destination(relative).resolve(), source)
        self.assertFalse((self.output / '.claude/skills/audit').exists())

    def test_inherited_command_map_rejects_parent_traversal(self):
        self.write('.cue/command-skill-map.json', json.dumps({'audit': '../outside.md'}))
        with self.assertRaisesRegex(ValueError, 'project-relative'):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_nested_instructions_preserve_directory_scope(self):
        self.write('nested/AGENTS.md', '# Converted directory instructions\n\nNESTED_ONLY\n')
        self.write('.cue/file-manifest.json', json.dumps([
            {'transformation': 'instruction-merge', 'target': 'nested/AGENTS.md'}]))
        self.stage()
        self.assertEqual((self.output / 'nested/CLAUDE.md').read_text(), 'NESTED_ONLY\n')
        self.assertTrue((self.output / 'CLAUDE.md').is_symlink())
        self.assertNotIn('NESTED_ONLY', (self.root / '.cue/shared-instructions.md').read_text())

    def test_explicit_install_root_is_used_instead_of_staging_path(self):
        target = self.base / 'different target'
        self.stage(target)
        settings = json.loads((self.output / '.claude/settings.json').read_text())
        self.assertEqual(settings['env']['CUE_PROJECT_DIR'], str(target))
        self.assertNotIn(str(self.output), json.dumps(settings))
        self.assertEqual(self.installed_link_destination('CLAUDE.md', target).resolve(), target / '.cue/shared-instructions.md')


if __name__ == '__main__':
    unittest.main()
