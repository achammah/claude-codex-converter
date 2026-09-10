"""Stage Codex setup for Claude without installing or executing source content.

API: stage_codex_to_claude(source, output, strict=False, model_map=None,
restore_original=True) returns a deterministic preservation report. Strict gaps
set exit_code=2; malformed input/drift raises ValueError after archival staging.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tomllib
import yaml

try:
    from .version import VERSION
except ImportError:
    from version import VERSION


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or str(path) in ('', '.'):
        raise ValueError('Unsafe relative setup path')
    return path


def checked(path, root):
    path = Path(path)
    if not path.is_relative_to(root):
        raise ValueError('Setup path escapes source root')
    current = path
    while current != root.parent:
        if current.is_symlink():
            raise ValueError('Setup path crosses a symlink')
        if current == root:
            break
        current = current.parent
    return path


def parse_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise ValueError('Malformed JSON setup document: '+path.name) from None


def parse_toml(path):
    try:
        return tomllib.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise ValueError('Malformed TOML setup document: '+path.name) from None


def stage_codex_to_claude(source, output, *, strict=False, model_map=None, restore_original=True):
    source = Path(source).expanduser().absolute()
    if source.is_symlink():
        raise ValueError('Source root must not be a symlink')
    root = source.parent if source.name == '.codex' else source
    root = root.resolve()
    out = Path(output).expanduser().absolute()
    if out.is_symlink() or any(p.is_symlink() for p in out.parents):
        raise ValueError('Output must not cross a symlink')
    out = out.resolve()
    if not root.is_dir() or out == root or out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError('Use a separate output directory outside the source')
    if out.exists():
        raise ValueError('Output directory must be new')
    if not any((root/p).exists() for p in ('.codex', '.agents', 'AGENTS.md', 'AGENTS.override.md')):
        raise ValueError('No Codex setup found')
    model_map = dict(model_map or {})
    out.mkdir(parents=True, mode=0o700)
    findings, inventory, emitted = [], [], []

    def gap(category, location, message):
        findings.append(dict(category=category, source=str(location), status='manual', message=message))

    def write(rel, data, mode=0o600):
        rel = safe_relative(rel)
        dest = out/rel
        if dest.exists() or dest.is_symlink():
            raise ValueError('Output collision: '+str(rel))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            data = data.encode()
        dest.write_bytes(data)
        dest.chmod(mode)
        emitted.append(dict(path=str(rel), sha256=sha(data), mode=mode))

    def dump(rel, value):
        write(rel, json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)+'\n')

    # Inventory selected configuration trees plus every nested instruction entrypoint.
    selected = set()
    for name in ('.codex', '.agents', '.cue', '.cue-source-archive'):
        base = root/name
        if base.is_symlink():
            selected.add(base)
        elif base.is_dir():
            for current, dirs, files in os.walk(base, followlinks=False):
                dirs.sort()
                for child in dirs+sorted(files):
                    path = Path(current)/child
                    if path.is_symlink() or path.is_file():
                        selected.add(path)
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {'.git', '.codex', '.agents', '.cue', '.cue-source-archive', 'node_modules', '.venv'} and not (Path(current)/d).is_symlink())
        selected.update(Path(current)/name for name in ('AGENTS.md', 'AGENTS.override.md') if name in files)
    for path in sorted(selected):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            inventory.append(dict(path=rel, kind='symlink', link=os.readlink(path)))
            gap('symlink', rel, 'Link metadata retained without following or activating its target.')
        else:
            data = checked(path, root).read_bytes()
            archive = '.reverse-source/files/'+rel
            write(archive, data)
            inventory.append(dict(path=rel, kind='file', sha256=sha(data), bytes=len(data), archive=archive, mode=path.stat().st_mode & 0o777))

    restored = []
    manifest_path = root/'.cue/file-manifest.json'
    provenance = restore_original and manifest_path.is_file()
    if provenance:
        conversion = parse_json(checked(root/'.cue/conversion-report.json', root))
        original_source = Path(conversion['source'])
        original_project = original_source.parent
        manifest = parse_json(checked(manifest_path, root))
        if not isinstance(manifest, list):
            raise ValueError('Invalid source manifest')
        candidates, original_modes = {}, {}
        for row in manifest:
            if row.get('kind') == 'symlink':
                gap('original-symlink', 'source-manifest', 'Original link retained as metadata; no unverified target activated.')
                continue
            if not row.get('archive'):
                continue
            data = checked(root/safe_relative(row['archive']), root).read_bytes()
            if sha(data) != row.get('sha256'):
                raise ValueError('Original archive hash mismatch')
            if row.get('target'):
                target = checked(root/safe_relative(row['target']), root)
                if not target.is_file() or sha(target.read_bytes()) != row.get('target_sha256'):
                    raise ValueError('Generated source drift: '+str(row['target']))
            origin = Path(row['source'])
            if row.get('root_relative'):
                rel = safe_relative(row['root_relative'])
            elif row.get('source_relative'):
                rel = Path('.claude')/safe_relative(row['source_relative'])
            elif origin.is_relative_to(original_project):
                rel = safe_relative(str(origin.relative_to(original_project)))
            else:
                gap('external-original', 'source-manifest', 'External original retained; restoring global files requires separate installation scope.')
                continue
            if rel in candidates and candidates[rel] != data:
                raise ValueError('Conflicting original source records')
            candidates[rel] = data
            if 'source_mode' in row:
                original_modes[rel] = 0o600 | (int(row['source_mode']) & 0o111)
            elif rel.suffix in ('.sh', '.py', '.js'):
                gap('original-file-mode', str(rel), 'Legacy provenance omits source executable bits; restored private file requires mode review.')
        # Root instruction snapshots use text + original byte hash; CRLF loss is rejected.
        snapshot_dir = root/'.cue/root-instruction-sources'
        if snapshot_dir.is_dir():
            for path in sorted(snapshot_dir.glob('*.json')):
                row = parse_json(checked(path, root))
                origin = Path(row['source'])
                if origin.is_relative_to(original_project):
                    relative = safe_relative(str(origin.relative_to(original_project)))
                    if relative in candidates:
                        if sha(candidates[relative]) != row['sha256']:
                            raise ValueError('Conflicting root instruction snapshots')
                        continue
                data = row['text'].encode()
                if sha(data) != row['sha256']:
                    raise ValueError('Root instruction archive is not byte-exact')
                origin = Path(row['source'])
                if origin.is_relative_to(original_project):
                    rel = safe_relative(str(origin.relative_to(original_project)))
                    if rel in candidates and candidates[rel] != data:
                        raise ValueError('Conflicting root instruction snapshots')
                    candidates[rel] = data
        baseline = root/'.cue/generated-manifest.json'
        if baseline.is_file():
            rows = parse_json(checked(baseline, root))
            if isinstance(rows, dict):
                rows = rows.get('files', [])
            if not isinstance(rows, list) or not rows:
                raise ValueError('Empty generated manifest cannot establish provenance')
            covered = set()
            for row in rows:
                rel = safe_relative(row['path'])
                path = root/rel
                if row.get('kind') == 'symlink':
                    if not path.is_symlink() or os.readlink(path) != row.get('target', row.get('link')):
                        raise ValueError('Generated link drift: '+str(rel))
                elif not checked(path, root).is_file() or sha(path.read_bytes()) != row['sha256']:
                    raise ValueError('Generated control drift: '+str(rel))
                elif 'mode' not in row:
                    gap('unverified-generated-mode', str(rel), 'Legacy generated manifest omits file mode; executable permission drift cannot be excluded.')
                elif path.stat().st_mode & 0o777 != row['mode']:
                    raise ValueError('Generated control mode drift: '+str(rel))
                covered.add(str(rel))
            controls = {r['path'] for r in inventory if not r['path'].startswith('.cue-source-archive/') and r['path'] != '.cue/generated-manifest.json'}
            if controls != covered:
                raise ValueError('Generated manifest does not cover current controls')
            findings[:] = [f for f in findings if f['category'] != 'symlink' or f['source'] not in covered]
        else:
            gap('unverified-generated-controls', '.cue/file-manifest.json', 'Legacy provenance does not hash generated Codex controls. Original snapshot can be reviewed, but unchanged active configuration is not established.')
        for rel, data in sorted(candidates.items()):
            write(str(rel), data, original_modes.get(rel, 0o600))
            restored.append(str(rel))
        if not restored:
            raise ValueError('Provenance has no restorable project files')
    else:
        settings = {}
        config_path = root/'.codex/config.toml'
        config = parse_toml(checked(config_path, root)) if config_path.is_file() else {}
        for row in inventory:
            name = row['path']
            if name.startswith('.codex/') and name not in ('.codex/config.toml', '.codex/hooks.json') and not name.startswith(('.codex/skills/', '.codex/agents/')):
                gap('native-resource', name, 'Unmapped native resource archived; it is not automatically active in Claude.')
        for path in sorted(selected):
            if path.name not in ('AGENTS.md', 'AGENTS.override.md') or path.is_symlink():
                continue
            if path.name == 'AGENTS.md' and (path.parent/'AGENTS.override.md').exists():
                continue
            if path.relative_to(root).parts[0] in ('.cue', '.cue-source-archive', '.agents', '.codex'):
                continue
            write(str(path.relative_to(root).with_name('CLAUDE.md')), path.read_bytes())
        if config.get('developer_instructions'):
            # Additional scope layer; never overwrite selected project instructions.
            write('.claude/rules/codex-developer-instructions.md', config['developer_instructions'])
            gap('instruction-precedence', 'developer_instructions', 'Instruction bytes retained as project guidance; developer-level priority is not transferable.')
        model = config.get('model')
        if model:
            if model in model_map:
                settings['model'] = model_map[model]
            else:
                gap('model', 'model', 'Target model requires an explicit model mapping.')
        if 'model_reasoning_effort' in config:
            gap('reasoning-effort', 'model_reasoning_effort', 'Provider reasoning levels are not presumed equivalent.')
        permission_keys = {'approval_policy', 'sandbox_mode', 'permissions', 'default_permissions', 'sandbox_workspace_write', 'approvals_reviewer'}
        if permission_keys & config.keys() or any(r['path'].startswith('.codex/rules/') for r in inventory):
            settings['permissions'] = {'defaultMode': 'plan'}
            gap('permissions', 'config.toml', 'Staged Claude plan mode is a restrictive review default, not equivalent enforcement. Native approvals and filesystem rules require review before activation.')
        mcp = {}
        servers = config.get('mcp_servers', {})
        if not isinstance(servers, dict):
            raise ValueError('MCP servers must be a table')
        for name, server in sorted(servers.items()):
            if not isinstance(server, dict):
                raise ValueError('Invalid MCP server definition')
            if 'command' in server and 'url' in server:
                raise ValueError('Ambiguous MCP transport')
            if not any(key in server for key in ('command', 'url')):
                gap('mcp-transport', 'mcp_servers/'+name, 'No executable transport supplied; definition remains archived.')
                continue
            if any(key in server and not isinstance(server[key], str) for key in ('command', 'url')):
                raise ValueError('MCP transport must be a string')
            if 'args' in server and (not isinstance(server['args'], list) or any(not isinstance(x, str) for x in server['args'])):
                raise ValueError('MCP arguments must be strings')
            entry = {k: server[k] for k in ('command', 'args', 'env', 'url') if k in server}
            if 'http_headers' in server:
                entry['headers'] = server['http_headers']
            entry['type'] = 'http' if 'url' in entry else 'stdio'
            mcp[name] = entry
            if server.get('enabled') is False:
                settings.setdefault('disabledMcpjsonServers', []).append(name)
            for key in sorted(set(server)-{'command', 'args', 'env', 'url', 'http_headers', 'enabled'}):
                gap('mcp-setting', 'mcp_servers/'+name+'/'+key, 'Unmapped server field remains in private source archive.')
        if mcp:
            dump('.mcp.json', {'mcpServers': mcp})
        # Skills are portable Markdown/resource trees; native UI policy remains explicit.
        for skillroot in (root/'.agents/skills', root/'.codex/skills'):
            if not skillroot.exists():
                continue
            checked(skillroot, root)
            for path in sorted(skillroot.rglob('*')):
                if path.is_symlink():
                    continue
                if not path.is_file():
                    continue
                checked(path, root)
                rel = path.relative_to(skillroot)
                write('.claude/skills/'+str(rel), path.read_bytes())
                if path.name == 'openai.yaml':
                    gap('skill-policy', str(path.relative_to(root)), 'Codex skill UI/invocation policy retained but not interpreted by Claude.')
        for path in sorted((root/'.codex/agents').glob('*.toml')):
            data = parse_toml(checked(path, root))
            name = data.get('name', path.stem)
            if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
                raise ValueError('Invalid agent name')
            instructions = data.get('developer_instructions', '')
            if not isinstance(instructions, str):
                raise ValueError('Invalid agent instructions')
            metadata = dict(name=name, description=data.get('description', 'Converted agent '+name))
            if not isinstance(metadata['description'], str):
                raise ValueError('Agent description must be a string')
            if data.get('model') in model_map:
                metadata['model'] = model_map[data['model']]
            for key in sorted(set(data)-{'name', 'description', 'developer_instructions'}):
                if key == 'model' and data[key] in model_map:
                    continue
                gap('agent-setting', str(path.relative_to(root))+'/'+key, 'Agent control archived; no equivalent tool, permission or model behavior inferred.')
            write('.claude/agents/'+name+'.md', '---\n'+yaml.safe_dump(metadata, sort_keys=True, allow_unicode=True)+'---\n\n'+instructions)
        hooks_path = root/'.codex/hooks.json'
        if hooks_path.is_file():
            parse_json(checked(hooks_path, root))
            write('.claude/codex-hooks.review.json', hooks_path.read_bytes())
            gap('hooks', '.codex/hooks.json', 'Hook payloads and tool names differ. Original definitions retained for adaptation, not activated or executed.')
        known = {'developer_instructions', 'model', 'model_reasoning_effort', 'mcp_servers'} | permission_keys
        for key in sorted(set(config)-known):
            gap('codex-setting', 'config.toml/'+key, 'Setting retained in source archive; native equivalence is not established.')
        dump('.claude/settings.json', settings)
    report = dict(schema_version=1, converter_version=VERSION, direction='codex-to-claude', state='staged', mode='original-snapshot' if provenance else 'native-codex', runtime_equivalent=False, strict=bool(strict), exit_code=2 if strict and findings else 0, inventory=inventory, restored=restored, generated=emitted.copy(), findings=findings)
    dump('reverse-setup-report.json', report)
    return report


def restore_codex_original(project, output):
    """Restore an unchanged reverse-stage's native snapshot, or None if absent.

    Validates all staged files and archive hashes before creating output. Added,
    removed, edited or linked controls reject restoration. Authentication is not
    interpreted; archived bytes remain private until deliberately restored.
    """
    root = Path(project).expanduser().resolve()
    report_path = root/'reverse-setup-report.json'
    if not report_path.exists() and not report_path.is_symlink():
        return None
    report = parse_json(checked(report_path, root))
    if report.get('schema_version') != 1 or report.get('direction') != 'codex-to-claude':
        raise ValueError('Unsupported reverse provenance')
    target_arg = Path(output).expanduser().absolute()
    if target_arg.is_symlink():
        raise ValueError('Restoration output must not be a symlink')
    target = target_arg.resolve()
    if target.exists() or target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError('Restoration requires a new separate output directory')
    expected = {}
    for row in report['generated']:
        relative = str(safe_relative(row['path']))
        if relative in expected:
            raise ValueError('Duplicate generated provenance path')
        expected[relative] = row
    current = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs+files:
            path = Path(directory)/name
            if path.is_file() or path.is_symlink():
                relative = str(path.relative_to(root))
                if relative != 'reverse-setup-report.json':
                    current[relative] = path
    if set(current) != set(expected):
        raise ValueError('Reverse generated file set drift')
    for relative, row in expected.items():
        path = checked(current[relative], root)
        if not path.is_file() or sha(path.read_bytes()) != row['sha256']:
            raise ValueError('Reverse generated file drift: '+relative)
        if 'mode' in row and path.stat().st_mode & 0o777 != row['mode']:
            raise ValueError('Reverse generated mode drift: '+relative)
    files, links, seen = [], [], set()
    for row in report['inventory']:
        relative = safe_relative(row['path'])
        if str(relative) in seen:
            raise ValueError('Duplicate original provenance path')
        seen.add(str(relative))
        if row['kind'] == 'symlink':
            link = row['link']
            if not isinstance(link, str) or Path(link).is_absolute():
                raise ValueError('External original symlink requires explicit restoration')
            if not (target/relative.parent/link).resolve().is_relative_to(target):
                raise ValueError('Escaping original symlink requires explicit restoration')
            links.append((relative, link))
        elif row['kind'] == 'file':
            archive = checked(root/safe_relative(row['archive']), root)
            data = archive.read_bytes()
            if sha(data) != row['sha256']:
                raise ValueError('Native original archive hash mismatch')
            files.append((relative, data, int(row.get('mode', 0o600)) & 0o777))
        else:
            raise ValueError('Unsupported original file kind')
    # Reject ancestry collisions so no restored symlink redirects later writes.
    for relative, *_ in files+links:
        if any(p != relative and p.is_relative_to(relative) for p, *_ in files+links):
            raise ValueError('Original file ancestry conflict')
    target.mkdir(parents=True, mode=0o700)
    for relative, data, mode in files:
        path = target/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    for relative, link in links:
        path = target/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(link)
    return dict(schema_version=1, state='restored', direction='claude-to-codex', mode='verified-native-snapshot', restored_files=len(files), restored_symlinks=len(links), source_changed=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--model-map', action='append', default=[], metavar='CODEX=CLAUDE')
    parser.add_argument('--native-only', action='store_true', help='Translate current native configuration instead of restoring a provenance snapshot')
    args = parser.parse_args(argv)
    try:
        mapping = {}
        for pair in args.model_map:
            if '=' not in pair:
                raise ValueError('Model mapping requires SOURCE=TARGET')
            source, target = pair.split('=', 1)
            if not source or not target or source in mapping:
                raise ValueError('Invalid or duplicate model mapping')
            mapping[source] = target
        result = stage_codex_to_claude(args.source, args.output, strict=args.strict, model_map=mapping, restore_original=not args.native_only)
        print(json.dumps({'state': result['state'], 'mode': result['mode'], 'findings': len(result['findings']), 'exit_code': result['exit_code']}))
        return result['exit_code']
    except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        # Parser errors can contain configuration values; expose only safe validation text.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, yaml.YAMLError) else type(exc).__name__
        print('Reverse setup failed: '+message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
