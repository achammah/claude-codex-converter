"""Synthetic reverse setup tests; no source hook, model, or live install executes."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CONVERTER = Path(__file__).resolve().parents[1]/'converter'
sys.path.insert(0, str(CONVERTER))
import codex_to_claude as reverse


class ReverseSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='reverse-setup-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base/'source'
        self.source.mkdir()
        self.write('.codex/config.toml', '')

    def write(self, name, data):
        path = self.source/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def stage(self, name='out', **kw):
        return reverse.stage_codex_to_claude(self.source, self.base/name, **kw)

    def test_instruction_override_and_nested_scopes(self):
        self.write('AGENTS.md', 'ignored root')
        self.write('AGENTS.override.md', 'root override\r\n')
        self.write('src/AGENTS.md', 'nested')
        self.stage()
        self.assertEqual((self.base/'out/CLAUDE.md').read_bytes(), b'root override\r\n')
        self.assertEqual((self.base/'out/src/CLAUDE.md').read_text(), 'nested')
        self.assertEqual((self.base/'out/.reverse-source/files/AGENTS.md').read_text(), 'ignored root')

    def test_skills_resources_and_agent(self):
        self.write('.agents/skills/demo/SKILL.md', '---\nname: demo\ndescription: Fixture\n---\nRead reference.txt')
        self.write('.agents/skills/demo/reference.txt', 'évidence')
        self.write('.codex/agents/reviewer.toml', 'description="Review"\ndeveloper_instructions="Read only."\n')
        self.stage()
        self.assertEqual((self.base/'out/.claude/skills/demo/reference.txt').read_text(), 'évidence')
        self.assertIn('Read only.', (self.base/'out/.claude/agents/reviewer.md').read_text())

    def test_mcp_disabled_headers_private_report(self):
        self.write('.codex/config.toml', '[mcp_servers.demo]\nurl="https://invalid.example/mcp"\nenabled=false\n[mcp_servers.demo.http_headers]\nAuthorization="SYNTHETIC_SECRET"\n')
        report = self.stage()
        mcp = json.loads((self.base/'out/.mcp.json').read_text())
        self.assertEqual(mcp['mcpServers']['demo']['headers']['Authorization'], 'SYNTHETIC_SECRET')
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(report))
        self.assertEqual(json.loads((self.base/'out/.claude/settings.json').read_text())['disabledMcpjsonServers'], ['demo'])
        self.assertEqual((self.base/'out/.mcp.json').stat().st_mode & 0o777, 0o600)

    def test_permissions_never_enable_bypass(self):
        self.write('.codex/config.toml', 'approval_policy="never"\nsandbox_mode="danger-full-access"\n')
        report = self.stage(strict=True)
        settings = json.loads((self.base/'out/.claude/settings.json').read_text())
        self.assertEqual(settings['permissions']['defaultMode'], 'plan')
        self.assertEqual(report['exit_code'], 2)
        self.assertFalse(report['runtime_equivalent'])

    def test_unknown_fields_retained_and_strict_gap(self):
        self.write('.codex/config.toml', 'future_option="private value"\n')
        self.write('.codex/future.bin', b'\x00\xff')
        report = self.stage(strict=True)
        self.assertEqual(report['exit_code'], 2)
        self.assertEqual((self.base/'out/.reverse-source/files/.codex/future.bin').read_bytes(), b'\x00\xff')
        self.assertNotIn('private value', json.dumps(report))

    def test_malformed_config_no_secret_echo(self):
        self.write('.codex/config.toml', 'token = "SYNTHETIC_SECRET')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = reverse.main([str(self.source), '--output', str(self.base/'out')])
        self.assertEqual(code, 1)
        self.assertNotIn('SYNTHETIC_SECRET', stderr.getvalue())
        self.assertTrue((self.base/'out/.reverse-source/files/.codex/config.toml').is_file())

    def test_symlink_not_followed(self):
        outside = self.base/'external'; outside.write_text('DO NOT COPY')
        (self.source/'.codex/link').symlink_to(outside)
        report = self.stage(strict=True)
        self.assertTrue(any(r['kind']=='symlink' for r in report['inventory']))
        self.assertFalse((self.base/'out/.reverse-source/files/.codex/link').exists())
        self.assertEqual(report['exit_code'], 2)

    def test_destination_collision_rejected(self):
        dest = self.base/'out'; dest.mkdir(); (dest/'mine').write_text('keep')
        with self.assertRaises(ValueError): self.stage()
        self.assertEqual((dest/'mine').read_text(), 'keep')

    def test_no_source_write(self):
        self.write('AGENTS.md', 'original')
        before = {str(p.relative_to(self.source)):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        self.stage()
        self.assertEqual(before, {str(p.relative_to(self.source)):p.read_bytes() for p in self.source.rglob('*') if p.is_file()})

    def test_model_mapping_explicit(self):
        self.write('.codex/config.toml', 'model="codex-example"\n')
        self.stage(model_map={'codex-example':'claude-example'})
        self.assertEqual(json.loads((self.base/'out/.claude/settings.json').read_text())['model'], 'claude-example')

    def test_hooks_quarantined_not_executed(self):
        self.write('.codex/hooks.json', json.dumps({'hooks':{'Stop':[{'hooks':[{'type':'command','command':'exit 99'}]}]}}))
        report = self.stage(strict=True)
        self.assertNotIn('hooks', json.loads((self.base/'out/.claude/settings.json').read_text()))
        self.assertTrue((self.base/'out/.claude/codex-hooks.review.json').exists())
        self.assertEqual(report['exit_code'], 2)

    def forward(self):
        original = self.base/'claude'; original.mkdir()
        (original/'.claude/skills/demo').mkdir(parents=True)
        (original/'.claude/settings.json').write_text('{"permissions":{"deny":["Read(secret)"]}}\n')
        (original/'.claude/CLAUDE.md').write_bytes(b'original doctrine\r\n')
        (original/'CLAUDE.md').write_bytes(b'root doctrine\r\n')
        (original/'.mcp.json').write_bytes(b'{ "mcpServers": {}, "future": "preserve" }\r\n')
        (original/'.claude/skills/demo/SKILL.md').write_text('---\nname: demo\ndescription: Demo fixture\n---\nRead carefully.\n')
        target = self.base/'forward'
        result = subprocess.run([sys.executable, str(CONVERTER/'claude_to_codex.py'), str(original), '--output', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return original, target

    def test_real_forward_reverse_original_bytes(self):
        original, target = self.forward()
        report = reverse.stage_codex_to_claude(target, self.base/'roundtrip', strict=True)
        self.assertEqual(report['exit_code'], 0, report['findings'])
        for path in (original/'.claude').rglob('*'):
            if path.is_file(): self.assertEqual(path.read_bytes(), (self.base/'roundtrip'/path.relative_to(original)).read_bytes())
        for name in ('CLAUDE.md', '.mcp.json'):
            self.assertEqual((original/name).read_bytes(), (self.base/'roundtrip'/name).read_bytes())

    def test_generated_control_drift_rejected(self):
        _, target = self.forward()
        with (target/'.codex/config.toml').open('a') as f:f.write('\nmodel="changed"\n')
        with self.assertRaisesRegex(ValueError, 'Generated control drift'):
            reverse.stage_codex_to_claude(target, self.base/'out')

    def test_added_control_rejected(self):
        _, target = self.forward()
        (target/'.codex/extra.toml').write_text('different=true')
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            reverse.stage_codex_to_claude(target, self.base/'out')

    def test_forward_generated_mode_drift_rejected(self):
        _, target = self.forward()
        path = target/'.cue/scripts/status_line.py'
        path.chmod(0o700 if path.stat().st_mode & 0o777 != 0o700 else 0o600)
        with self.assertRaisesRegex(ValueError, 'Generated control mode drift'):
            reverse.stage_codex_to_claude(target, self.base/'out', strict=True)

    def test_legacy_generated_modes_explicit_gap(self):
        _, target = self.forward()
        path = target/'.cue/generated-manifest.json'
        manifest = json.loads(path.read_text())
        for row in manifest['files']:
            row.pop('mode', None)
        path.write_text(json.dumps(manifest))
        report = reverse.stage_codex_to_claude(target, self.base/'out', strict=True)
        self.assertEqual(report['exit_code'], 2)
        self.assertTrue(any(f['category']=='unverified-generated-mode' for f in report['findings']))

    def test_archive_corruption_rejected(self):
        _, target = self.forward()
        (target/'.cue-source-archive/project/CLAUDE.md').write_text('corrupted')
        with self.assertRaisesRegex(ValueError, 'archive hash mismatch'):
            reverse.stage_codex_to_claude(target, self.base/'out')

    def test_legacy_provenance_explicit_gap(self):
        _, target = self.forward()
        (target/'.cue/generated-manifest.json').unlink()
        report = reverse.stage_codex_to_claude(target, self.base/'out', strict=True)
        self.assertEqual(report['exit_code'], 2)
        self.assertTrue(any(f['category']=='unverified-generated-controls' for f in report['findings']))

    def test_deterministic_staging_bytes(self):
        self.write('AGENTS.md', 'same')
        self.stage('one'); self.stage('two')
        def hashes(name): return {str(p.relative_to(self.base/name)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.base/name).rglob('*') if p.is_file()}
        self.assertEqual(hashes('one'), hashes('two'))

    def test_native_reverse_then_restore_exact(self):
        self.write('AGENTS.md', 'native\r\n')
        self.write('.codex/config.toml', 'approval_policy="never"\n')
        script = self.write('.codex/helper.sh', '#!/bin/sh\nexit 0\n'); script.chmod(0o755)
        self.stage()
        result = reverse.restore_codex_original(self.base/'out', self.base/'restored')
        self.assertEqual(result['state'], 'restored')
        for path in self.source.rglob('*'):
            if path.is_file():
                target = self.base/'restored'/path.relative_to(self.source)
                self.assertEqual(path.read_bytes(), target.read_bytes())
                self.assertEqual(path.stat().st_mode & 0o777, target.stat().st_mode & 0o777)

    def test_native_restore_changed_claude_refused(self):
        self.write('AGENTS.md', 'native')
        self.stage()
        (self.base/'out/CLAUDE.md').write_text('edited')
        with self.assertRaisesRegex(ValueError, 'file drift'):
            reverse.restore_codex_original(self.base/'out', self.base/'restored')
        self.assertFalse((self.base/'restored').exists())

    def test_native_restore_added_file_refused(self):
        self.stage()
        (self.base/'out/.claude/extra.md').write_text('new')
        with self.assertRaisesRegex(ValueError, 'file set drift'):
            reverse.restore_codex_original(self.base/'out', self.base/'restored')

    def test_native_restore_safe_relative_symlink(self):
        self.write('.codex/a.txt', 'content')
        (self.source/'.codex/b.txt').symlink_to('a.txt')
        self.stage()
        reverse.restore_codex_original(self.base/'out', self.base/'restored')
        self.assertTrue((self.base/'restored/.codex/b.txt').is_symlink())
        self.assertEqual((self.base/'restored/.codex/b.txt').read_text(), 'content')

    def test_native_restore_external_link_refused(self):
        (self.source/'.codex/link').symlink_to('../../outside')
        self.stage()
        with self.assertRaisesRegex(ValueError, 'Escaping original symlink'):
            reverse.restore_codex_original(self.base/'out', self.base/'restored')

    def test_native_restore_absent_provenance(self):
        self.assertIsNone(reverse.restore_codex_original(self.source, self.base/'restored'))

    def test_native_restore_mode_drift_refused(self):
        self.write('AGENTS.md', 'native')
        self.stage()
        (self.base/'out/CLAUDE.md').chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'mode drift'):
            reverse.restore_codex_original(self.base/'out', self.base/'restored')
        self.assertFalse((self.base/'restored').exists())

    def test_native_restore_deleted_file_refused(self):
        self.write('AGENTS.md', 'native')
        self.stage()
        (self.base/'out/CLAUDE.md').unlink()
        with self.assertRaisesRegex(ValueError, 'file set drift'):
            reverse.restore_codex_original(self.base/'out', self.base/'restored')
        self.assertFalse((self.base/'restored').exists())


if __name__ == '__main__': unittest.main()
