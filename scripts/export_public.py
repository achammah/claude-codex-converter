#!/usr/bin/env python3
"""Create a source-only public export from a reviewed, explicit file allowlist."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile

# Reviewed source set; additions require a new review, never a recursive copy.
ALLOWLIST = ('.github/workflows/compatible-codex.yml', 'BIDIRECTIONAL-CONTRACT.md', 'COMPATIBILITY.md', 'FINDINGS.md', 'LICENSE', 'README.md', 'RELEASE-PIPELINE.md', 'build.py', 'converter/__init__.py', 'converter/activate_skill.py', 'converter/ask_user_question.py', 'converter/bash_file_views.py', 'converter/claude_to_codex.py', 'converter/cli.py', 'converter/codex_to_claude.py', 'converter/codex_tui.py', 'converter/conversations.py', 'converter/doctor.py', 'converter/hook_timeouts.py', 'converter/host-contract.md', 'converter/host_adapter.py', 'converter/install.py', 'converter/instruction_sources.py', 'converter/managed_update.py', 'converter/native_import.py', 'converter/native_runtime.py', 'converter/normalize_workspace_lock.py', 'converter/protocol.py', 'converter/release_pipeline.py', 'converter/requirements.txt', 'converter/runtime.py', 'converter/setup.py', 'converter/status_line.py', 'converter/version.py', 'native/LICENSE', 'native/NOTICE', 'native/__init__.py', 'native/managed-updater.patch', 'native/manifest.json', 'native/manifests/0.154.0/README.md', 'native/manifests/0.154.0/managed-updater.patch', 'native/manifests/0.154.0/manifest.json', 'native/manifests/0.154.0/normalize_workspace_lock.py', 'native/manifests/0.154.0/question-form.patch', 'native/manifests/0.154.0/status-provider.patch', 'native/question-form.patch', 'native/status-provider.patch', 'pyproject.toml', 'scripts/ci_package.py', 'scripts/ci_release.py', 'scripts/export_public.py', 'scripts/release_runner.py', 'tests/bidirectional_repeatability.py', 'tests/live_managed_update.py', 'tests/live_question.py', 'tests/live_wiring.py', 'tests/native_smoke/README.md', 'tests/native_smoke/harness-Cargo.lock', 'tests/native_smoke/helper.py', 'tests/native_smoke/native_tests.py', 'tests/native_smoke/question.py', 'tests/native_smoke/question_server.py', 'tests/native_smoke/requirements.txt', 'tests/native_smoke/resource_capacity.py', 'tests/native_smoke/update_package.py', 'tests/native_smoke/update_prompt.py', 'tests/native_smoke/update_route.py', 'tests/release_smoke.py', 'tests/repeatability.py', 'tests/test_bash_file_views.py', 'tests/test_bash_permission_views.py', 'tests/test_bidirectional_cli.py', 'tests/test_ci_release.py', 'tests/test_conversations.py', 'tests/test_converter_regressions.py', 'tests/test_delivery_regressions.py', 'tests/test_doctor.py', 'tests/test_export_public.py', 'tests/test_hook_timeouts.py', 'tests/test_host_adapter.py', 'tests/test_import_literals.py', 'tests/test_inherited_commands.py', 'tests/test_instruction_sources.py', 'tests/test_managed_update.py', 'tests/test_mcp_permissions.py', 'tests/test_native_hook_ask.py', 'tests/test_native_lockfile_patch.py', 'tests/test_native_runtime.py', 'tests/test_native_smoke_layout.py', 'tests/test_permissions_parity.py', 'tests/test_protocol_async.py', 'tests/test_protocol_streaming.py', 'tests/test_question_regressions.py', 'tests/test_release_pipeline.py', 'tests/test_release_runner.py', 'tests/test_resource_boundaries.py', 'tests/test_reverse_mcp_permissions.py', 'tests/test_reverse_setup.py', 'tests/test_setup.py', 'tests/test_shell_permission_exactness.py', 'tests/test_source_activation.py', 'tests/test_startup.py', 'tests/test_status_line.py', 'tests/test_update_capabilities.py')
MAX_FILE = 16 * 1024 * 1024
PATTERNS = {
    'personal-home-path': re.compile(r'/Users/[A-Za-z][A-Za-z0-9_.-]*/'),
    'private-development-evidence': re.compile(r'\.cue/' + r'runtime-fixes/'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'credential-token': re.compile(r'(?<![A-Za-z0-9])(?:ghp_|github_pat_|sk-proj-|nxs_)[A-Za-z0-9_-]{20,}'),
    'literal-secret': re.compile(r'(?i)(?:api[_-]?key|token|password|secret)\s*[=:]\s*["\'][A-Za-z0-9_+/=-]{32,}["\']'),
}


def digest(data): return hashlib.sha256(data).hexdigest()


def scan(relative, data, forbidden=()):
    text = data.decode('utf-8')
    if '\0' in text: raise ValueError('Binary source file: ' + relative)
    findings = []
    patterns = {**PATTERNS, **{'private-literal-' + str(i): re.compile(re.escape(term), re.I) for i, term in enumerate(forbidden)}}
    for name, pattern in patterns.items():
        for match in pattern.finditer(text):
            findings.append({'path':relative,'rule':name,'line':text.count('\n',0,match.start())+1})
    return findings


def export(source, destination, report_path, *, forbidden=(), allowlist=ALLOWLIST):
    source, destination, report_path = Path(source).resolve(), Path(destination).absolute(), Path(report_path).absolute()
    if destination.exists() or destination.is_symlink(): raise ValueError('Export destination already exists')
    if destination.resolve().is_relative_to(source): raise ValueError('Export must be outside source tree')
    if report_path.resolve().is_relative_to(destination.resolve()): raise ValueError('Scan report must be outside exported source')
    files, findings = {}, []
    if len(set(allowlist)) != len(allowlist): raise ValueError('Duplicate allowlist entry')
    for relative in allowlist:
        parts = PurePosixPath(relative)
        if parts.is_absolute() or any(x in ('','.','..') for x in relative.split('/')): raise ValueError('Invalid allowlist path')
        path=source
        for part in parts.parts:
            path=path/part
            if path.is_symlink(): raise ValueError('Symlink in source allowlist: '+relative)
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_FILE: raise ValueError('Invalid source file: '+relative)
        data=path.read_bytes()
        findings.extend(scan(relative,data,forbidden));files[relative]=(data,info.st_mode & 0o777)
    report={'schemaVersion':1,'passed':not findings,'scope':'explicit source allowlist; bounded text patterns, not exhaustive secret detection','files':len(files),'findings':findings}
    report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    if findings: raise ValueError('Public source scan failed; see external scan report')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.public-export-',dir=destination.parent) as tmp:
        stage=Path(tmp)/'source';stage.mkdir()
        manifest={}
        for relative,(data,mode) in sorted(files.items()):
            target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);target.chmod(mode)
            manifest[relative]={'sha256':digest(data),'mode':mode}
        encoded=(json.dumps({'schemaVersion':1,'files':manifest},sort_keys=True,indent=2)+'\n').encode()
        (stage/'PUBLIC-SOURCE-MANIFEST.json').write_bytes(encoded)
        # Verify reads did not span a moving source snapshot.
        for relative,(data,mode) in files.items():
            if (source/relative).read_bytes()!=data: raise ValueError('Source changed during export: '+relative)
        if destination.exists(): raise ValueError('Export destination appeared during staging')
        os.rename(stage,destination)
    report['manifestSha256']=digest(encoded)
    report_path.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--output',type=Path,required=True);p.add_argument('--report',type=Path,required=True);p.add_argument('--forbid',action='append',default=[])
    a=p.parse_args();print(json.dumps(export(a.source,a.output,a.report,forbidden=a.forbid),sort_keys=True))


if __name__=='__main__':main()
