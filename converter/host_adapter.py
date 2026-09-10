"""Stage a Claude host view over an existing converted shared .cue estate.

This is a one-way, reviewable export, not a synchronizer. It does not install
files, execute hooks, modify trust, restore running agents, or import history.
Relative links are designed to resolve after installation beside the shared .cue
root; they may be dangling inside the deliberately minimal staging directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import yaml


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _checked(path, root):
    if not path.is_relative_to(root) or '..' in path.relative_to(root).parts:
        raise ValueError('Shared source escaped selected root: ' + str(path))
    current = path
    while True:
        if current.is_symlink():
            raise ValueError('Shared source crosses a symlink: ' + str(current))
        if current == root:
            break
        current = current.parent
    return path


def _read(path, root):
    return _checked(path, root).read_text()


def _frontmatter(content, source):
    if not content.startswith('---\n'):
        return {}, content
    match = re.match(r'^---\n(.*?)\n---(?:\n|$)(.*)$', content, re.S)
    if not match:
        raise ValueError('Unclosed skill or agent frontmatter: ' + str(source))
    try:
        metadata = yaml.safe_load(match[1]) or {}
    except yaml.YAMLError:
        repaired = re.sub(r'^(description):[ \t]+([^\n]+)$',
                          lambda m: m[1] + ': ' + json.dumps(m[2])
                          if not m[2].startswith(('"', "'", '|', '>', '[', '{')) else m[0],
                          match[1], flags=re.M)
        if repaired == match[1]:
            raise
        metadata = yaml.safe_load(repaired) or {}
    if not isinstance(metadata, dict):
        raise ValueError('Skill or agent metadata must be an object: ' + str(source))
    return metadata, match[2]


def _replace_root(value, original, replacement):
    return re.sub(re.escape(str(original)) + r'''(?=$|[/\s'"),])''', lambda _: str(replacement), value)


def stage_claude(neutral_root, output, install_root=None):
    """Stage host files; return a report. ``install_root`` defaults to neutral_root.

    Required neutral files: .cue/effective-settings.json, conversion-report.json,
    and shared-instructions.md (the latter excludes any Codex host prelude).
    Skill metadata is restored from .cue/metadata/skills; agent source metadata
    comes from the preserved working resource trees. Hook types and source disable
    flags remain intact for Claude to interpret natively.
    """
    root = Path(neutral_root).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    target = Path(install_root).expanduser().resolve() if install_root else root
    if not root.is_dir():
        raise ValueError('Shared source is not a directory: ' + str(root))
    if output == root or output.is_relative_to(root) or output == target or output.is_relative_to(target):
        raise ValueError('Use a staging directory outside the shared source and installation root.')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Host staging output must be a new empty directory.')
    cue = root / '.cue'
    shared = cue / 'shared-instructions.md'
    if not shared.is_file():
        raise ValueError('Missing .cue/shared-instructions.md; regenerate the shared estate. Codex AGENTS.md is not a Claude doctrine source.')
    _checked(shared, root)
    effective = json.loads(_read(cue / 'effective-settings.json', root))
    conversion = json.loads(_read(cue / 'conversion-report.json', root))
    if not isinstance(effective, dict):
        raise ValueError('Effective settings must be an object.')
    original_source = Path(conversion['source'])
    original_project = original_source.parent
    records, findings = [], []

    def translate(value):
        if isinstance(value, str):
            value = _replace_root(value, original_source, target / '.cue')
            value = _replace_root(value, original_project, target)
            value = _replace_root(value, root, target)
            # Preserve provider-owned home stores and external user configuration.
            value = value.replace('~/.claude', '~/<SOURCE_HOME>')
            home_claude = str(Path.home() / '.claude')
            value = value.replace(home_claude, '<ABS_SOURCE_HOME>')
            value = re.sub(r'''(?<![\w~])\.claude(?=/)''', '.cue', value)
            value = value.replace('.cue/CLAUDE.md', '.cue/INSTRUCTIONS.md')
            return value.replace('~/<SOURCE_HOME>', '~/.claude').replace('<ABS_SOURCE_HOME>', home_claude)
        if isinstance(value, list):
            return [translate(item) for item in value]
        if isinstance(value, dict):
            return {key: translate(item) for key, item in value.items()}
        return value

    def write(relative, content, mode=0o600):
        path = output / relative
        if path.exists() or path.is_symlink():
            raise ValueError('Host output collision: ' + relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode()
        path.write_bytes(data)
        path.chmod(mode)
        records.append({'path': relative, 'kind': 'file', 'sha256': _sha(data)})

    def link(relative, source):
        _checked(source, root)
        if not source.exists():
            raise ValueError('Shared resource is missing: ' + str(source))
        source_relative = source.relative_to(root)
        destination = output / relative
        if destination.exists() or destination.is_symlink():
            raise ValueError('Host output collision: ' + relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        link_text = os.path.relpath(target / source_relative, (target / relative).parent)
        destination.symlink_to(link_text, target_is_directory=source.is_dir())
        row = {'path': relative, 'kind': 'symlink', 'link': link_text, 'shared_source': str(source_relative)}
        if source.is_file():
            row['source_sha256'] = _sha(source.read_bytes())
        records.append(row)

    # Validate sources before creating any host entrypoint.
    metadata_root = cue / 'metadata/skills'
    skill_metadata = {}
    if metadata_root.exists():
        _checked(metadata_root, root)
        for path in sorted(metadata_root.glob('*.json')):
            name = path.stem
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name):
                raise ValueError('Unsafe skill name: ' + name)
            metadata = json.loads(_read(path, root))
            if not isinstance(metadata, dict):
                raise ValueError('Skill metadata must be an object: ' + name)
            skill_metadata[name] = metadata
    command_names = set()
    command_paths = {}
    commands = cue / 'commands'
    if commands.exists():
        _checked(commands, root)
        for path in sorted(commands.rglob('*.md')):
            metadata, _ = _frontmatter(_read(path, root), path)
            command_name = str(metadata.get('name', path.stem))
            command_names.add(command_name)
            command_paths[command_name] = path
    command_map_path = cue / 'command-skill-map.json'
    command_map = {}
    if command_map_path.exists():
        command_map = json.loads(_read(command_map_path, root))
        if not isinstance(command_map, dict):
            raise ValueError('Command skill map must be an object.')
        for name, relative in command_map.items():
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name):
                raise ValueError('Unsafe mapped command name: ' + name)
            if not isinstance(relative, str):
                raise ValueError('Mapped command path must be a string: ' + name)
            path = Path(relative)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Mapped command must reference a neutral project-relative path: ' + name)
            source_command = _checked(root / path, root)
            if not source_command.is_file():
                raise ValueError('Mapped command source is missing: ' + name)
            command_names.add(name)
            command_paths[name] = source_command
    agent_paths = []
    agent_roots = [cue / 'agents', cue / 'vendor/user/agents']
    plugins = cue / 'vendor/plugins'
    if plugins.exists():
        _checked(plugins, root)
        agent_roots.extend(path / 'agents' for path in sorted(plugins.iterdir()) if path.is_dir())
    agent_names = set()
    for agent_root in agent_roots:
        if not agent_root.exists():
            continue
        _checked(agent_root, root)
        for path in sorted(agent_root.rglob('*.md')):
            metadata, _ = _frontmatter(_read(path, root), path)
            name = str(metadata.get('name', path.stem))
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
                raise ValueError('Unsafe agent name: ' + name)
            if name in agent_names:
                raise ValueError('Ambiguous source agent name: ' + name)
            agent_names.add(name)
            agent_paths.append((name, path))

    output.mkdir(parents=True, exist_ok=True)
    settings = translate(effective)
    env = settings.setdefault('env', {})
    if not isinstance(env, dict):
        raise ValueError('Source env setting must be an object.')
    env['CUE_PROJECT_DIR'] = str(target)
    env['CUE_STATE_ROOT'] = str(target / '.cue/state/claude')
    write('.claude/settings.json', json.dumps(settings, indent=2, ensure_ascii=False) + '\n')
    # The effective settings and shared doctrine already include these layers.
    # Stage explicit empty replacements so an old local file cannot silently
    # override the selected shared snapshot after a reviewed installation.
    write('.claude/settings.local.json', '{}\n')
    write('.claude/CLAUDE.md', '')
    write('CLAUDE.local.md', '')
    link('CLAUDE.md', shared)
    for name, path in agent_paths:
        link('.claude/agents/' + name + '.md', path)
    if command_map:
        for name, path in sorted(command_paths.items()):
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name):
                raise ValueError('Unsafe native command name: ' + name)
            link('.claude/commands/' + name + '.md', path)
        findings.append('Inherited commands are registered by their reviewed converted names; directory-derived native command namespaces may differ.')
    elif commands.exists():
        link('.claude/commands', commands)
    hooks = cue / 'hooks'
    if hooks.exists():
        link('.claude/hooks', hooks)
    for name, metadata in sorted(skill_metadata.items()):
        if name in command_names:
            continue
        skill = cue / 'skills' / name
        _, body = _frontmatter(_read(skill / 'SKILL.md', root), skill / 'SKILL.md')
        activation = f'Before using this skill, run `python3 .cue/scripts/activate_skill.py {name}` so its scoped hooks apply.\n\n'
        if metadata.get('hooks') and body.lstrip('\n').startswith(activation):
            body = body.lstrip('\n')[len(activation):]
        restored = translate({key: value for key, value in metadata.items() if not key.startswith('__converter_')})
        restored.setdefault('name', name)
        header = yaml.safe_dump(restored, sort_keys=False, allow_unicode=True).strip()
        write('.claude/skills/' + name + '/SKILL.md', '---\n' + header + '\n---\n\n' + translate(body), 0o644)
        for resource in sorted(skill.iterdir()):
            if resource.name == 'SKILL.md':
                continue
            link('.claude/skills/' + name + '/' + resource.name, resource)
    mcp = cue / 'mcp-source.json'
    if mcp.exists():
        write('.mcp.json', json.dumps({'mcpServers': translate(json.loads(_read(mcp, root)))}, indent=2) + '\n')
    manifest_path = cue / 'file-manifest.json'
    if manifest_path.exists():
        manifest = json.loads(_read(manifest_path, root))
        nested_targets = sorted({row['target'] for row in manifest
                                 if row.get('transformation') == 'instruction-merge' and
                                 row.get('target', '').endswith('/AGENTS.md')})
        for relative in nested_targets:
            path = Path(relative)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe nested instruction path in manifest: ' + relative)
            content = _read(root / path, root)
            prefix = '# Converted directory instructions\n\n'
            if content.startswith(prefix):
                content = content[len(prefix):]
            write(str(path.with_name('CLAUDE.md')), translate(content), 0o644)
    findings.extend([
        'Stage only: install the reviewed plan into the declared installation root; shared .cue resources must already exist there.',
        'Settings and reconstructed skill headers are snapshots. Restage after editing shared metadata; this is not bidirectional synchronization.',
        'Original Claude hook types and disable settings are retained. No hook execution or runtime verification was performed.',
        'Existing root/local Claude instructions and inherited user/plugins can alter precedence after installation; review collisions and actual host loading.',
        'Merged user settings can be inherited again from the live Claude user configuration, duplicating hooks; manual cutover review is required and no global files are changed.',
        'Existing native .claude/rules and stale agent, skill, or command entries are not deleted by this staging plan; review duplicate loading and precedence before switching.',
        'Running agents, approvals, conversation history, plugin installation, and external credentials are not transferred by this host view.',
    ])
    report = {'version': 1, 'host': 'claude', 'state': 'staged', 'neutral_root': str(root),
              'installation_root': str(target), 'files': records, 'findings': findings}
    report_path = output / '.claude/conversion-stage.json'
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    report_path.chmod(0o600)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    stage = sub.add_parser('stage')
    stage.add_argument('neutral_root', type=Path)
    stage.add_argument('--host', choices=['claude'], required=True)
    stage.add_argument('--output', type=Path, required=True)
    stage.add_argument('--install-root', type=Path)
    args = parser.parse_args(argv)
    try:
        report = stage_claude(args.neutral_root, args.output, args.install_root)
        print(json.dumps({'state': report['state'], 'host': report['host'], 'files': len(report['files']),
                          'report': str(args.output / '.claude/conversion-stage.json')}))
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print('Host staging failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
