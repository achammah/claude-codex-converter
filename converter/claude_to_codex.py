#!/usr/bin/env python3
"""Convert a Claude project into an auditable, independently usable Codex setup.

python3 claude_to_codex.py PROJECT --output NEW_DIRECTORY
Requires Python 3.11+ and PyYAML. Never executes source hooks or edits the source.
No universal program can infer the meaning of arbitrary executable hooks; strict
mode returns nonzero for unsupported behavior rather than claiming silent parity.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tomllib

try:
    from .instruction_sources import collect_instruction_supplements
    from .hook_timeouts import source_timeout, wrapper_timeout
    from .protocol import permission_path_pattern
    from .status_line import NATIVE_STATUS_LINE_ITEMS, build_manifest, parse_state_roots
    from .version import VERSION
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from instruction_sources import collect_instruction_supplements
    from hook_timeouts import source_timeout, wrapper_timeout
    from protocol import permission_path_pattern
    from status_line import NATIVE_STATUS_LINE_ITEMS, build_manifest, parse_state_roots
    from version import VERSION

try:
    import yaml
except ImportError:
    raise SystemExit('Install the converter dependency: python3 -m pip install -r requirements.txt')


SHORT_LIFECYCLE_TIMEOUT = 3
SHORT_LIFECYCLE_EVENTS = {'SessionEnd', 'Interrupt'}
EVENTS = {'SessionStart', 'SessionEnd', 'SubagentStart', 'SubagentStop', 'PreToolUse',
          'PermissionRequest', 'PostToolUse', 'PreCompact', 'PostCompact', 'UserPromptSubmit', 'Stop', 'Interrupt'}
KNOWN_SETTINGS = {'$schema', 'permissions', 'hooks', 'env', 'model', 'effortLevel', 'outputStyle',
                  'statusLine', 'enabledPlugins', 'alwaysThinkingEnabled', 'autoCompactEnabled',
                  'modelSettings', 'promptSuggestionEnabled', 'tui', 'skipDangerousModePermissionPrompt',
                  'editorMode', 'switchModelsOnFlag', 'remoteControlAtStartup', 'agentPushNotifEnabled'}
LIST_MERGE_KEYS = {'allow', 'ask', 'deny', 'additionalDirectories'}


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f'Duplicate YAML key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(x) for x in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k) + ' = ' + toml_value(v) for k, v in value.items()) + '}'
    return json.dumps(str(value), ensure_ascii=False)


def frontmatter(content, source):
    if not content.startswith('---\n'):
        return {}, content
    match = re.match(r'^---\n(.*?)\n---(?:\n|$)(.*)$', content, re.S)
    if not match:
        raise ValueError(f'Unclosed YAML frontmatter: {source}')
    try:
        metadata = yaml.load(match[1], Loader=UniqueLoader) or {}
    except yaml.YAMLError:
        # Some working Claude estates contain plain descriptions with ': ' in
        # them. Recover only this unambiguous single-line scalar, never guess
        # arbitrary nested structures or collapse scoped-hook metadata.
        repaired = re.sub(r'^(description):[ \t]+([^\n]+)$',
                          lambda m: m[1] + ': ' + json.dumps(m[2])
                          if not m[2].startswith(('"', "'", '|', '>', '[', '{')) else m[0],
                          match[1], flags=re.M)
        if repaired == match[1]:
            raise
        metadata = yaml.load(repaired, Loader=UniqueLoader) or {}
        metadata['__converter_repairs__'] = 'Quoted a plain single-line description containing YAML punctuation; original bytes preserved.'
    if not isinstance(metadata, dict):
        raise ValueError(f'Frontmatter must be a mapping: {source}')
    return metadata, match[2]


def merge(base, value, path=()):
    """Merge settings layers while preserving additive permission and hook arrays."""
    out = dict(base)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], item, (*path, key))
        elif isinstance(item, list) and isinstance(out.get(key), list) and (key in LIST_MERGE_KEYS or 'hooks' in path):
            out[key] = list(out[key])
            for entry in item:
                if entry not in out[key]:
                    out[key].append(entry)
        else:
            out[key] = item
    return out


class Converter:
    def __init__(self, args):
        self.args = args
        p = args.source.expanduser().resolve()
        self.source = p if p.name == '.claude' else p / '.claude'
        self.project = self.source.parent
        self.output = args.output.expanduser().resolve()
        self.findings, self.files, self.agents, self.skills = [], [], [], []
        self.scoped_hooks, self.routes = [], {}
        self.permission_rules = []
        self.extra_skills, self.extra_agents, self.extra_commands, self.resource_maps = [], [], [], []
        self.model_map = dict(pair.split('=', 1) for pair in args.model_map)
        self.legacy_state_roots = parse_state_roots(getattr(args, 'legacy_state_root', []))
        if not self.source.is_dir() and not any((self.project / name).is_file() for name in ('CLAUDE.md', 'CLAUDE.local.md', '.mcp.json')):
            raise ValueError(f'No .claude directory: {self.source}')
        if self.output == self.project or self.output == self.source or self.output.is_relative_to(self.source):
            raise ValueError('Use a separate output directory; source is never overwritten.')
        if self.output.exists() and any(self.output.iterdir()):
            raise ValueError('Output directory must be empty. Existing work is never overwritten.')

    def finding(self, kind, source, rule, status='converted', **extra):
        self.findings.append({'id': f'CONV-{len(self.findings)+1:04}', 'category': kind,
                              'source': str(source), 'rule': rule, 'status': status, **extra})

    def archive_root_file(self, path):
        if path.parent != self.project or not self.safe_source(path):
            raise ValueError('Root source must be a regular project file: ' + str(path))
        data = path.read_bytes()
        archive = self.output / '.cue-source-archive/root' / path.name
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(data)
        archive.chmod(0o600)
        self.files.append({'source': str(path), 'root_relative': path.name,
                           'archive': archive.relative_to(self.output).as_posix(),
                           'sha256': digest(data), 'bytes': len(data),
                           'source_mode': path.stat().st_mode & 0o7777, 'status': 'archived'})
        return data

    def translate(self, content):
        # Specific project roots precede relative path substitutions. Historical
        # home-directory paths are deliberately not vendor-renamed.
        for source, target in self.resource_maps:
            content = content.replace(str(source), str(target))
        content = content.replace(str(self.source), str(self.output / '.cue'))
        content = content.replace(str(self.project), str(self.output))
        content = content.replace('~/.claude', '~/<SOURCE_CLAUDE_HOME>')
        content = content.replace(str(Path.home() / '.claude'), '<ABS_SOURCE_CLAUDE_HOME>')
        # Provider-owned task/transcript stores also appear as path components,
        # especially in test fixtures. Keep these consistent with ~/.claude.
        content = re.sub(r'''(['"])\.claude\1(?=\s*(?:,|/)\s*(['"])(?:tasks|projects)\2)''',
                         lambda m: m[1] + '<SOURCE_CLAUDE_STORE_COMPONENT>' + m[1], content)
        content = content.replace('.claude/', '.cue/').replace('".claude"', '".cue"').replace("'.claude'", "'.cue'")
        content = re.sub(r'''(?<![\w~])\.claude(?![\w.-])''', '.cue', content)
        content = content.replace('CLAUDE_PROJECT_DIR', 'CUE_PROJECT_DIR').replace('CLAUDE.md', 'INSTRUCTIONS.md')
        return content.replace('~/<SOURCE_CLAUDE_HOME>', '~/.claude').replace('<ABS_SOURCE_CLAUDE_HOME>', str(Path.home() / '.claude')).replace('<SOURCE_CLAUDE_STORE_COMPONENT>', '.claude')

    def safe_source(self, path):
        """A symlinked ancestor is as meaningful as a symlinked leaf."""
        here = path
        while here != self.project and here != here.parent:
            if here.is_symlink():
                return False
            here = here.parent
        return True

    def imported_instructions(self, content, source, stack=()):
        """Expand real file imports recursively, preserving their source evidence."""
        def replace(match):
            token = match[1].rstrip('.,;:)')
            suffix = match[1][len(token):]
            path = Path(token).expanduser()
            path = path if path.is_absolute() else source.parent / path
            resolved = path.resolve()
            boundaries = [self.project]
            if self.args.include_user_resources:
                boundaries.append(self.args.global_settings.expanduser().resolve().parent if self.args.global_settings else Path.home() / '.claude')
            if not any(resolved.is_relative_to(boundary) for boundary in boundaries) or not self.safe_source(path):
                self.finding('instruction-import', source, 'External or symlinked import needs an explicit packaging boundary.', 'manual', imported_path=token)
                return match[0]
            if resolved in stack:
                self.finding('instruction-import', source, 'Import cycle detected; repeated expansion omitted.', 'manual', imported_path=token)
                return match[0]
            if not resolved.is_file():
                self.finding('instruction-import', source, 'Referenced import does not resolve to a file.', 'manual', imported_path=token)
                return match[0]
            data = resolved.read_bytes()
            archive = self.output / '.cue-source-archive/imported' / digest(str(resolved).encode())[:16] / resolved.name
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(data); archive.chmod(0o600)
            self.finding('instruction-import', source, 'Referenced instruction file expanded recursively and archived.', imported_path=token, sha256=digest(data), archive=str(archive.relative_to(self.output)))
            return '\n' + self.imported_instructions(data.decode(), resolved, (*stack, resolved)) + '\n' + suffix
        # Ignore fenced code; source import syntax outside it can be inline.
        parts, fence, inline_ticks = [], None, 0
        for line in content.splitlines(keepends=True):
            marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\n'))
            if fence:
                parts.append(line)
                if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                    fence = None
            elif marker and not inline_ticks:
                fence = (marker[1][0], len(marker[1]))
                parts.append(line)
            else:
                # Inline code examples are literal source text, not file imports.
                for chunk in re.split(r'(`+)', line):
                    if chunk.startswith('`'):
                        inline_ticks = 0 if inline_ticks == len(chunk) else (inline_ticks or len(chunk))
                        parts.append(chunk)
                    else:
                        parts.append(chunk if inline_ticks else re.sub(r'(?<![\w@])@([^\s`<>]+)', replace, chunk))
        return ''.join(parts)

    def inventory(self):
        reserved = {'effective-settings.json', 'settings-provenance.json', 'settings-layers',
                    'metadata', 'root-instruction-sources', 'mcp-source.json', 'hook-routes.json',
                    'conversion-findings.json', 'conversion-report.json', 'compatibility-report.md',
                    'file-manifest.json', 'generated-manifest.json', '.converter-runtime', 'vendor', 'shared-instructions.md', 'command-skill-map.json'}
        occupied = reserved & {p.name for p in self.source.iterdir()} if self.source.is_dir() else set()
        if occupied:
            raise ValueError('Source occupies converter metadata names; no files were overwritten: ' + ', '.join(sorted(occupied)))
        planned_targets = {}
        for path in sorted(self.source.rglob('*')):
            rel = path.relative_to(self.source)
            if path.is_symlink():
                self.finding('symlink', path, 'External and directory symlinks need an explicit packaging decision.', 'manual', target=os.readlink(path))
                self.files.append({'source': str(path), 'kind': 'symlink', 'link': os.readlink(path), 'status': 'not-followed'})
                continue
            if not path.is_file():
                continue
            data = path.read_bytes()
            original = self.output / '.cue-source-archive/project' / rel
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, original)
            os.chmod(original, 0o600)
            target = self.output / '.cue' / Path(str(rel).replace('CLAUDE.md', 'INSTRUCTIONS.md'))
            if target in planned_targets:
                raise ValueError(f'Translated path collision: {planned_targets[target]} and {path}; original archives retained.')
            planned_targets[target] = path
            archived = ('__pycache__' in rel.parts or rel.name == '.DS_Store' or 'worktrees' in rel.parts
                        or rel.suffix in ('.pyc', '.bak', '.w20bak', '.prewire-bak'))
            row = {'source': str(path), 'source_relative': str(rel), 'sha256': digest(data), 'bytes': len(data),
                   'source_mode': path.stat().st_mode & 0o7777,
                   'archive': str(original.relative_to(self.output)), 'status': 'archived' if archived else 'converted'}
            if not archived:
                try:
                    converted = self.translate(data.decode()).encode()
                except UnicodeDecodeError:
                    converted = data
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(converted)
                shutil.copymode(path, target)
                row.update(target=str(target.relative_to(self.output)), target_sha256=digest(converted))
            self.files.append(row)
        self.finding('inventory', self.source, 'Every source file is hashed and preserved before transformation.', files=len(self.files))

    def user_resources(self):
        if not self.args.include_user_resources:
            return
        home = self.args.global_settings.expanduser().resolve().parent if self.args.global_settings else Path.home() / '.claude'
        roots = [(home / name, self.output / '.cue/vendor/user' / name) for name in ('skills', 'agents', 'commands')]
        installed = home / 'plugins/installed_plugins.json'
        if installed.exists():
            registry = json.loads(installed.read_text()).get('plugins', {})
            for plugin, enabled in self.settings.get('enabledPlugins', {}).items():
                if not enabled:
                    continue
                entries = registry.get(plugin, [])
                applicable = [x for x in entries if x.get('scope') == 'user' or x.get('projectPath') == str(self.project)]
                if len(applicable) != 1:
                    self.finding('plugin-discovery', plugin, 'No unique applicable installed package; preserved enabled declaration.', 'manual')
                    continue
                root = Path(applicable[0]['installPath'])
                slug = re.sub(r'[^a-zA-Z0-9_-]', '-', plugin) + '-' + digest(plugin.encode())[:16]
                roots.append((root, self.output / '.cue/vendor/plugins' / slug))
        for source, target in roots:
            if source.is_symlink() or not self.safe_source(source):
                self.finding('external-symlink', source, 'Selected resource root crosses a symlink; resource tree was not followed.', 'manual')
                continue
            if not source.is_dir():
                continue
            self.resource_maps.append((source, target))
            for path in sorted(source.rglob('*')):
                rel = path.relative_to(source)
                if path.is_symlink():
                    self.finding('external-symlink', path, 'Symlink preserved as evidence; not followed into unselected resources.', 'manual', target=os.readlink(path))
                    continue
                if not path.is_file():
                    continue
                data = path.read_bytes()
                archive = self.output / '.cue-source-archive/external' / digest(str(source).encode())[:16] / rel
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, archive); archive.chmod(0o600)
                dest = target / rel
                if dest.exists() or dest.is_symlink():
                    raise ValueError('Resource target collision: ' + str(dest))
                try:
                    changed = self.translate(data.decode()).replace('${CLAUDE_PLUGIN_ROOT}', str(target)).encode()
                except UnicodeDecodeError:
                    changed = data
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(changed); shutil.copymode(path, dest)
                self.files.append({'source': str(path), 'sha256': digest(data), 'bytes': len(data),
                                   'source_mode': path.stat().st_mode & 0o7777,
                                   'archive': str(archive.relative_to(self.output)), 'target': str(dest.relative_to(self.output)),
                                   'status': 'converted', 'scope': 'user-resource'})
            skill_root = target if source.name == 'skills' else target / 'skills'
            self.extra_skills.extend(skill_root.glob('*/SKILL.md'))
            agent_root = target if source.name == 'agents' else target / 'agents'
            self.extra_agents.extend(agent_root.rglob('*.md'))
            hook_file = target / 'hooks/hooks.json'
            if hook_file.exists():
                self.hook_groups(json.loads(hook_file.read_text()).get('hooks', {}), source / 'hooks/hooks.json')
            if source.name == 'commands' or (target / 'commands').exists():
                command_root = target if source.name == 'commands' else target / 'commands'
                self.extra_commands.extend(command_root.rglob('*.md'))
                if self.extra_commands:
                    self.finding('external-commands', source, 'Inherited command definitions become Codex skills with complete sibling resource trees; collisions are reported.')
            for config in (target / '.lsp.json', target / '.mcp.json', target / '.claude-plugin/plugin.json'):
                if config.exists():
                    self.finding('plugin-contract', source / config.relative_to(target), 'Package manifest retained; host-specific plugin, LSP and MCP declarations need a native provider adapter.', 'manual')
            self.finding('user-resources', source, 'All regular package files retained with hashes; skill and agent entrypoints registered.')

    def load_settings(self):
        layers = []
        if self.args.global_settings:
            layers.append(self.args.global_settings.expanduser().resolve())
        elif (Path.home() / '.claude/settings.json').exists():
            self.finding('inheritance', '~/.claude/settings.json', 'User-global settings exist. Include them explicitly with --global-settings to avoid silently omitting effective behavior.', 'manual')
        for name in ('settings.json', 'settings.local.json'):
            if (self.source / name).exists():
                layers.append(self.source / name)
        effective = {}
        provenance = []
        for i, path in enumerate(layers):
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError(f'Settings must be a JSON object: {path}')
            layer_permissions = value.get('permissions', {})
            if not isinstance(layer_permissions, dict):
                raise ValueError(f'Permissions must be a JSON object: {path}')
            for key in ('allow', 'ask', 'deny', 'additionalDirectories'):
                entries = layer_permissions.get(key, [])
                if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
                    raise ValueError(f'Permission {key} must be a list of strings: {path}')
            effective = merge(effective, value)
            source_root = self.output if path.parent == self.source else path.parent
            for action in ('allow', 'ask', 'deny'):
                for rule in layer_permissions.get(action, []):
                    if not isinstance(rule, str):
                        raise ValueError(f'Permission rule must be a string: {path}#permissions/{action}')
                    row = {'action': action, 'rule': self.translate(rule), 'original_rule': rule,
                           'source_root': str(source_root)}
                    if row not in self.permission_rules:
                        self.permission_rules.append(row)
            dump(self.output / '.cue/settings-layers' / f'{i}-{path.name}', value)
            provenance.append({'path': str(path), 'sha256': digest(path.read_bytes()), 'keys': list(value)})
            for key in value:
                if key not in KNOWN_SETTINGS:
                    self.finding('setting', f'{path}#/{key}', 'Unknown source setting preserved; no executable target mapping is claimed.', 'manual')
        dump(self.output / '.cue/settings-provenance.json', provenance)
        self.settings = effective
        dump(self.output / '.cue/effective-settings.json', effective)
        dump(self.output / '.cue/permission-rules.json', self.permission_rules)
        self.finding('settings-merge', layers, 'Layers are global, project, local; permission and event hook lists merge additively.')

    def hook_groups(self, hooks, source, scope=None):
        if self.settings.get('disableAllHooks') is True:
            if hooks:
                self.finding('hooks-disabled', source, 'Explicit disableAllHooks suppresses source hook routes, including skill, agent, and plugin hooks.')
            return
        if not isinstance(hooks, dict):
            raise ValueError(f'Hook table must be an object: {source}')
        for event, groups in hooks.items():
            if event not in EVENTS and event != 'PostToolUseFailure':
                self.finding('hook-event', f'{source}/{event}', 'No documented Codex event equivalent. Definition preserved but not registered.', 'manual')
                continue
            if not isinstance(groups, list):
                raise ValueError(f'Hook event must contain a list: {source}/{event}')
            for group in groups:
                for handler in group.get('hooks', []):
                    if handler.get('type') != 'command':
                        self.finding('hook-handler', f'{source}/{event}', 'This converter adapts command hooks; other handler types require a dedicated contract adapter.', 'manual', handler_type=handler.get('type'))
                        continue
                    command = handler.get('command')
                    if not isinstance(command, str) or not command:
                        raise ValueError(f'Empty hook command: {source}/{event}')
                    unsupported = set(handler) - {'type', 'command', 'timeout', 'statusMessage', 'additionalContextLimit'}
                    if unsupported:
                        self.finding('hook-options', source, 'Handler options have no verified translation; handler is not activated.', 'manual', keys=sorted(unsupported))
                        continue
                    converted = self.translate(command)
                    source_timeout(handler, event)  # Reject invalid deadlines before activation.
                    external = re.findall(r'(?:/Users/|/home/|/opt/|~/)[^\s\"\';]+', converted)
                    if external and not all(x.startswith(str(self.output)) for x in external):
                        self.finding('external-hook', source, 'Command references host-external code; inspect its provider assumptions before activation.', 'manual', command_sha256=digest(command.encode()))
                        if not self.args.include_external_hooks:
                            continue
                    row = {'event': event, 'matcher': group.get('matcher', ''),
                           'handler': {**handler, 'command': converted}, 'source': str(source), 'scope': scope}
                    self.routes.setdefault(event, []).append(row)
                    self.finding('hook-route', source, 'Command routed through event/tool/output normalization; source code behavior requires fixture and live-host verification.', 'needs-runtime-test', event=event, scope=scope)

    def instructions(self):
        user_root = (self.args.global_settings.expanduser().resolve().parent if self.args.global_settings else Path.home() / '.claude') if self.args.include_user_resources else None
        supplements = collect_instruction_supplements(
            self.project, self.output, user_root=user_root, finding=self.finding,
            frontmatter=frontmatter,
            render=lambda p, b: self.translate(self.imported_instructions(b, p, (p.resolve(),))))
        self.files.extend(supplements['files'])
        parts = supplements['root_sections']
        sources = []
        # Root CLAUDE.md is distinct from the .claude/CLAUDE.md in this user's estate.
        for path in (self.project / 'CLAUDE.md', self.source / 'CLAUDE.md', self.project / 'CLAUDE.local.md'):
            if path.exists() and self.safe_source(path):
                original = path.read_text()
                sources.append(str(path))
                parts.append(self.translate(self.imported_instructions(original, path, (path.resolve(),))))
                if path.parent != self.source:
                    self.archive_root_file(path)
                    dump(self.output / '.cue/root-instruction-sources' / (path.name + '.json'), {'source': str(path), 'text': original, 'sha256': digest(path.read_bytes())})
        for path in sorted((self.source / 'rules').rglob('*.md')) if (self.source / 'rules').exists() else []:
            if not self.safe_source(path):
                continue
            metadata, body = frontmatter(path.read_text(), path)
            if metadata.get('paths'):
                self.finding('scoped-rule', path, 'Path-scoped rules retained with explicit applicability; Codex has no identical YAML paths rule loader.', 'manual', paths=metadata['paths'])
                parts.append('Apply the following only to these paths: ' + json.dumps(metadata['paths']) + '\n' + self.translate(self.imported_instructions(body, path, (path.resolve(),))))
            else:
                parts.append(self.translate(self.imported_instructions(body, path, (path.resolve(),))))
        prelude = (Path(__file__).parent / 'host-contract.md').read_text()
        content = prelude + '\n\n' + '\n\n---\n\n'.join(parts)
        text(self.output / '.cue/shared-instructions.md', '\n\n---\n\n'.join(parts) + '\n')
        text(self.output / 'AGENTS.md', content)
        self.instruction_bytes = len(content.encode()) + supplements['nested_max_bytes']
        self.finding('instruction-size', sources, 'Full content retained; project_doc_max_bytes raised above generated UTF-8 size.', bytes=self.instruction_bytes)

    def convert_agents(self):
        candidates = list((self.source / 'agents').rglob('*.md')) + self.extra_agents
        for path in sorted(candidates):
            if not self.safe_source(path):
                continue
            metadata, body = frontmatter(path.read_text(), path)
            name = metadata.get('name', path.stem)
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', str(name)):
                raise ValueError(f'Unsafe agent name: {name}')
            if name in self.agents:
                raise ValueError(f'Duplicate agent name: {name}')
            self.agents.append(name)
            desc = metadata.get('description')
            if not isinstance(desc, str) or not desc.strip():
                raise ValueError(f'Agent description missing: {path}')
            instructions = self.translate(body)
            tools = metadata.get('tools')
            if tools:
                instructions = 'Source capability restriction: ' + str(tools) + '. Use only the corresponding available host capabilities.\n\n' + instructions
                self.finding('agent-tools', path, 'Capability list retained as instructions; exact per-tool enforcement is not equivalent when Bash can write or spawn.', 'manual', tools=tools)
            for skill in metadata.get('skills') or []:
                instructions = f'Read .cue/skills/{skill}/SKILL.md in full before work.\n' + instructions
            agent = {'name': name, 'description': desc, 'developer_instructions': instructions}
            if metadata.get('effort'):
                agent['model_reasoning_effort'] = metadata['effort']
            model = metadata.get('model')
            if model and model != 'inherit':
                if model in self.model_map:
                    agent['model'] = self.model_map[model]
                else:
                    self.finding('model', path, 'No cross-provider equivalence inferred. Agent inherits parent; supply --model-map SOURCE=TARGET.', 'manual', model=model)
            for key in sorted(set(metadata) - {'name', 'description', 'tools', 'model', 'effort', 'skills', 'background', 'hooks'}):
                self.finding('agent-metadata', path, 'Source metadata retained but requires a specific target mapping.', 'manual', key=key)
            if metadata.get('background'):
                agent['developer_instructions'] = 'Run asynchronously when supported; preserve the return contract.\n' + agent['developer_instructions']
            if metadata.get('hooks'):
                self.hook_groups(metadata['hooks'], path, scope={'agent': name})
            rendered = '\n'.join(k + ' = ' + toml_value(v) for k, v in agent.items()) + '\n'
            tomllib.loads(rendered)
            text(self.output / '.codex/agents' / (name + '.toml'), rendered)

    def convert_skills(self):
        candidates = list((self.source / 'skills').glob('*/SKILL.md')) if (self.source / 'skills').exists() else []
        candidates += self.extra_skills
        commands = list((self.source / 'commands').rglob('*.md')) if (self.source / 'commands').exists() else []
        commands += self.extra_commands
        command_map = {}
        for path in sorted([x for x in candidates if x not in self.extra_skills] + [x for x in commands if x not in self.extra_commands]) + sorted(self.extra_skills + self.extra_commands):
            if not self.safe_source(path):
                continue
            metadata, body = frontmatter(path.read_text(), path)
            is_command = path in commands
            name = metadata.get('name', path.stem if is_command else path.parent.name)
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', str(name)):
                raise ValueError(f'Invalid skill name: {name}')
            if name in self.skills:
                if path in self.extra_skills or path in self.extra_commands:
                    self.finding('skill-shadowing', path, 'Project or earlier explicit skill takes precedence; inherited resource tree preserved.', name=name)
                    continue
                raise ValueError(f'Skill/command collision: {name}')
            self.skills.append(name)
            desc = metadata.get('description', f'Run the migrated /{name} command.' if is_command else '')
            if not isinstance(desc, str) or not desc.strip():
                raise ValueError(f'Skill description missing: {path}')
            target = self.output / '.cue/skills' / name
            if is_command:
                resource_parent = path.parent if path in self.extra_commands else self.output / '.cue/commands' / path.parent.relative_to(self.source / 'commands')
                if target.exists():
                    raise ValueError('Command skill resource collision: ' + str(target))
                shutil.copytree(resource_parent, target, symlinks=True)
                command_path = path if path in self.extra_commands else self.output / '.cue/commands' / path.relative_to(self.source / 'commands')
                command_map[name] = str(command_path.relative_to(self.output))
            if path in self.extra_skills:
                if target.exists():
                    self.finding('skill-shadowing', path, 'Project skill takes precedence over inherited skill with the same name; inherited resource tree preserved.', 'converted', name=name)
                    self.skills.pop()
                    continue
                shutil.copytree(path.parent, target)
            if not is_command and path not in self.extra_skills and name != path.parent.name:
                old_target = self.output / '.cue/skills' / path.parent.name
                if target.exists():
                    raise ValueError(f'Renamed skill would overwrite another resource tree: {target}')
                shutil.copytree(old_target, target)
                old_prefix = str(old_target.relative_to(self.output)) + '/'
                new_prefix = str(target.relative_to(self.output)) + '/'
                for row in self.files:
                    if row.get('target', '').startswith(old_prefix):
                        row['target'] = new_prefix + row['target'][len(old_prefix):]
                self.finding('skill-resources', path, 'Declared skill name differs from directory; entire resource tree copied to preserve relative references.')
            target.mkdir(parents=True, exist_ok=True)
            header = yaml.safe_dump({'name': name, 'description': desc}, sort_keys=False, allow_unicode=True).strip()
            translated_body = self.translate(body)
            if metadata.get('hooks'):
                self.hook_groups(metadata['hooks'], path, scope={'skill': name})
                translated_body = f'Before using this skill, run `python3 .cue/scripts/activate_skill.py {name}` so its scoped hooks apply.\n\n' + translated_body
            if re.search(r'!`|\$ARGUMENTS|\$\d', body):
                self.finding('command-expansion', path, 'Host-specific shell interpolation or positional arguments need explicit instruction/tool adaptation; never execute them during conversion.', 'manual')
            for key in sorted(set(metadata) - {'name', 'description', 'hooks', 'disable-model-invocation', 'user-invocable', 'metadata'}):
                self.finding('skill-metadata', path, 'Preserved in source archive; target-specific invocation semantics require review.', 'manual', key=key)
            text(target / 'SKILL.md', '---\n' + header + '\n---\n\n' + translated_body)
            if metadata.get('disable-model-invocation') is True:
                text(target / 'agents/openai.yaml', 'policy:\n  allow_implicit_invocation: false\n')
            link = self.output / '.agents/skills' / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=True)
            dump(self.output / '.cue/metadata/skills' / (name + '.json'), metadata)
        dump(self.output / '.cue/command-skill-map.json', command_map)

    def config(self):
        settings = self.settings
        permissions = settings.get('permissions', {})
        config = ['# Generated by claude-to-codex ' + VERSION,
                  'project_doc_max_bytes = ' + str(max(32768, self.instruction_bytes + 32768)),
                  'approval_policy = "on-request"', 'default_permissions = "converted"']
        model = settings.get('model')
        if model:
            if model in self.model_map:
                config.append('model = ' + toml_value(self.model_map[model]))
            else:
                self.finding('main-model', 'effective-settings/model', 'Keep the Codex model selected by the user unless an explicit mapping is supplied.', 'manual', model=model)
        if settings.get('effortLevel'):
            config.append('model_reasoning_effort = ' + toml_value(settings['effortLevel']))
        if 'statusLine' in settings:
            config += ['', '[tui]', 'status_line = ' + toml_value(NATIVE_STATUS_LINE_ITEMS)]
        config += ['', '[permissions.converted]', 'extends = ":workspace"', '[permissions.converted.filesystem]']
        filesystem = {}
        for path in permissions.get('additionalDirectories', []):
            directory = Path(self.translate(path)).expanduser()
            if not directory.is_absolute():
                directory = self.output / directory
            filesystem[os.path.abspath(directory)] = 'write'
        read_paths, edit_paths = set(), set()
        for row in self.permission_rules:
            if row['action'] not in ('ask', 'deny'):
                continue
            rule = row['rule']
            m = re.fullmatch(r'(Read|Edit)\((.+)\)', rule)
            if m:
                p = permission_path_pattern(m[2], self.output, source_root=row['source_root'], action=row['action'])
                (read_paths if m[1] == 'Read' else edit_paths).add(p)
                if not m[2].startswith(('/', '~/')):
                    self.finding('permission-cwd', rule,
                                 'The native filesystem fallback anchors this relative restriction at the target project. '
                                 'Runtime tool checks also evaluate the actual call working directory; shell filesystem access after a directory change needs host verification.',
                                 'needs-runtime-test', action=row['action'])
            elif not rule.startswith('Bash('):
                self.finding('permission', rule, 'Tool-specific permission has no verified native mapping; retain in runtime metadata and require review.', 'manual')
        for path in sorted(read_paths):
            filesystem[path] = 'deny'
        for path in sorted(edit_paths - read_paths):
            filesystem[path] = 'read'
        config += [toml_value(path) + ' = ' + toml_value(access) for path, access in sorted(filesystem.items())]
        self.finding('permissions', 'effective-settings/permissions', 'Source Read ask paths become native deny entries; managed hosts can make these non-escalatable. Edit ask paths become read-only. These restrictions do not preserve source approval semantics.', 'manual')
        if edit_paths:
            self.finding('permission-edit-deny', 'effective-settings/permissions',
                         'Native read-only paths require escalation for shell writes. Source Edit denies are enforced on adapted file-tool events; '
                         'read-only filesystem access does not by itself make every escalated shell write impossible.', 'manual')
        for key in sorted(set(permissions) - {'allow', 'ask', 'deny', 'additionalDirectories', 'defaultMode'}):
            self.finding('permission-setting', 'effective-settings/permissions/' + key,
                         'Permission control retained in source settings; no automatic target mapping is claimed.', 'manual', key=key)
        def sandbox_controls(value, prefix='sandbox'):
            if isinstance(value, dict) and value:
                for key in sorted(value):
                    sandbox_controls(value[key], prefix + '/' + key)
            else:
                self.finding('sandbox-setting', 'effective-settings/' + prefix,
                             'Source sandbox control retained; the generated native permission profile does not claim this field has equivalent enforcement.', 'manual')
        if 'sandbox' in settings:
            sandbox_controls(settings['sandbox'])
        mode = permissions.get('defaultMode', 'default')
        self.finding('permission-mode', 'effective-settings/permissions/defaultMode',
                     'The generated workspace permission profile with on-request approval is not an exact Claude permission mode. '
                     'Default/manual edit prompts and acceptEdits shell exemptions differ; plan and dontAsk restrictions are not reproduced by this profile. '
                     'Bypass modes are never enabled automatically. Review this mode before activation; --strict rejects unresolved mode parity.',
                     'manual', mode=mode)
        env = settings.get('env', {})
        ordinary_env = {k: v for k, v in env.items() if not k.startswith('CLAUDE_')}
        if ordinary_env:
            config += ['', '[shell_environment_policy.set]']
            config += [toml_value(k) + ' = ' + toml_value(v) for k, v in ordinary_env.items()]
            self.finding('environment', 'effective-settings/env', 'Non-Claude environment settings are copied into shell environment policy; keep generated config private.')
        agent_env = {'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS', 'CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION', 'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'}
        for key in env:
            if key.startswith('CLAUDE_') and key not in agent_env:
                self.finding('environment', key, 'Claude-specific environment variable preserved; no verified target setting.', 'manual')
        config += ['', '[skills]', 'max_context_tokens = 10000', '', '[agents]', 'enabled = true']
        self.finding('skill-catalog-budget', 'skills/max_context_tokens',
                     'Allocate the documented maximum catalog budget without shortening source descriptions or skill bodies. Larger inherited catalogs can still require host review.',
                     'converted', max_context_tokens=10000)
        if env.get('CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'):
            config.append('max_concurrent_threads_per_session = ' + str(int(env['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'])))
        if env.get('CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION'):
            self.finding('agent-limit', 'effective-settings/env', 'Total per-session limit is recorded but Codex native config only exposes a concurrency ceiling.', 'manual')
        mcp = self.project / '.mcp.json'
        if mcp.exists():
            servers = json.loads(self.archive_root_file(mcp)).get('mcpServers', {})
            dump(self.output / '.cue/mcp-source.json', servers)
            for name, server in servers.items():
                if name == 'cue_questions':
                    raise ValueError('Source MCP server uses reserved cue_questions name; rename it before conversion.')
                typ = server.get('type', 'stdio')
                if typ not in ('stdio', 'http'):
                    self.finding('mcp', name, 'Transport requires a verified adapter; server is not silently converted.', 'manual', transport=typ)
                    continue
                config += ['', '[mcp_servers.' + toml_value(name) + ']']
                if name in settings.get('disabledMcpjsonServers', []):
                    config.append('enabled = false')
                    self.finding('mcp-disabled', name, 'Explicit source disabledMcpjsonServers preserved as native enabled=false.')
                for key, target_key in [('command', 'command'), ('args', 'args'), ('env', 'env'), ('url', 'url'), ('headers', 'http_headers')]:
                    if key in server:
                        value = server[key]
                        if key == 'command' and isinstance(value, str):
                            value = self.translate(value)
                        if key == 'args' and isinstance(value, list):
                            value = [self.translate(x) if isinstance(x, str) else x for x in value]
                        config.append(target_key + ' = ' + toml_value(value))
                self.finding('mcp', name, 'Server definition transferred; authentication must be performed in the target host.', 'needs-runtime-test')
        if 'statusLine' in settings:
            manifest = build_manifest(settings['statusLine'], translate=self.translate, project=self.project, output=self.output,
                                      legacy_state_roots=self.legacy_state_roots)
            manifest['capture_enabled'] = settings.get('disableAllHooks') is not True
            if getattr(self.args, 'native_status', False):
                manifest['activation'] = 'patched-native-codex-status-provider'
                manifest['native_footer_mode'] = 'patched-command-provider'
                manifest['source_command_in_native_footer'] = True
                manifest['requires_patched_codex'] = True
            dump(self.output / '.cue/status-line.json', manifest)
            if getattr(self.args, 'native_status', False):
                config += ['', '[tui.status_provider]',
                           'command = ' + toml_value(['python3', str(self.output / '.cue/scripts/status_line.py'),
                                                       str(self.output), '--native-stdin', '--run-source', '--timeout', '44']),
                           'refresh_interval_ms = 5000', 'timeout_ms = 45000', 'max_lines = 4', 'ansi = "sgr"']
                self.finding('native-status-runtime', 'effective-settings/statusLine',
                             'Native command status provider requires the converter-patched Codex runtime; stock Codex does not implement this configuration.',
                             'needs-runtime-test')
            self.finding('status-line', 'effective-settings/statusLine',
                         'Source statusLine is retained with an explicit command bridge and a session-bound status reader. '
                         + ('Patched Codex renders the command through native provider configuration.' if getattr(self.args, 'native_status', False)
                            else 'Codex native built-in footer items are configured; arbitrary source-command output remains on the explicit bridge.'), 'converted',
                         bridge_supported=manifest['bridge_supported'], native_footer_supported=True,
                         native_footer_items=NATIVE_STATUS_LINE_ITEMS, source_command_in_native_footer=bool(getattr(self.args, 'native_status', False)))
            for key in manifest['unmapped_fields']:
                self.finding('status-line-setting', 'effective-settings/statusLine/' + key,
                             'Source statusLine field is retained in the manifest; the command bridge does not claim its UI semantics.', 'manual')
            for dependency in manifest['dependencies']:
                self.finding('status-line-dependency', dependency['reference'],
                             'Static dependency candidate recorded in .cue/status-line.json; dynamic dependencies and host state require separate verification.',
                             'converted' if dependency['status'] == 'staged' else 'manual', dependency_status=dependency['status'])
            for binding in manifest['state_rebindings']:
                self.finding('status-state-binding', binding.get('path', binding['original_literal']),
                             'Generated-copy state binding recorded with syntax verification and before/after hashes when applied. '
                             'Original archive bytes remain unchanged; missing or syntactically unsafe bindings require review.',
                             'converted' if binding['status'] == 'rebound' else 'manual', evidence=binding)
        elif self.legacy_state_roots:
            self.finding('status-state-binding', '--legacy-state-root',
                         'State-root mappings were supplied without a source statusLine; no source command was rewritten.', 'manual')
        for plugin, enabled in settings.get('enabledPlugins', {}).items():
            if enabled:
                self.finding('plugin', plugin, 'Enabled Claude plugin requires discovery and translation of its installed package, including LSP/MCP/hook dependencies.', 'manual')
        for key in sorted(set(settings) - {'$schema', 'permissions', 'hooks', 'env', 'model', 'effortLevel', 'statusLine', 'enabledPlugins'}):
            self.finding('setting', key, 'Setting is retained in effective-settings; host UI/model-specific behavior has no automatic mapping.', 'manual')
        config += ['', '[mcp_servers.cue_questions]', 'command = "python3"',
                   'args = ' + toml_value([str(self.output / '.cue/scripts/ask_user_question.py')]),
                   'tool_timeout_sec = 3600', '', '[features]', 'hooks = true']
        self.finding('question-tool', 'mcp_servers/cue_questions',
                     'Standalone AskUserQuestion uses native MCP form elicitation; accepted answers, decline, and cancel stay distinct. Requires a host with elicitation support; timeout is not an answer.', 'needs-runtime-test')
        rendered = '\n'.join(config) + '\n'
        tomllib.loads(rendered)
        text(self.output / '.codex/config.toml', rendered)

    def runtime(self):
        source_dir = Path(__file__).parent
        runtime_dir = self.output / '.cue/.converter-runtime'
        if runtime_dir.exists():
            raise ValueError('Source occupies reserved runtime directory .converter-runtime')
        runtime_dir.mkdir()
        shutil.copy2(source_dir / 'protocol.py', runtime_dir / 'protocol.py')
        shutil.copy2(source_dir / 'hook_timeouts.py', runtime_dir / 'hook_timeouts.py')
        shutil.copy2(source_dir / 'status_line.py', runtime_dir / 'status_line.py')
        for source, target in [('runtime.py', 'converted_hook.py'), ('activate_skill.py', 'activate_skill.py'), ('ask_user_question.py', 'ask_user_question.py'), ('status_line.py', 'status_line.py'), ('codex_tui.py', 'codex_tui.py')]:
            if (self.output / '.cue/scripts' / target).exists():
                raise ValueError(f'Source occupies generated entrypoint scripts/{target}; choose a namespace before installation.')
            shutil.copy2(source_dir / source, self.output / '.cue/scripts' / target)
        if 'statusLine' in self.settings:
            status_entry = ('#!/bin/sh\n'
                            'HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                            'exec python3 "$HERE/../.cue/scripts/status_line.py" "$HERE/.." "$@"\n')
            text(self.output / '.codex/cue-status', status_entry)
            (self.output / '.codex/cue-status').chmod(0o755)
            manifest = json.loads((self.output / '.cue/status-line.json').read_text())
            if getattr(self.args, 'native_status', False):
                self.finding('status-line-surface', 'effective-settings/statusLine',
                             'Plain codex renders the converted command through the patched native status provider. Install the bundled compatible runtime before activating this configuration.',
                             'needs-runtime-test', configuration='tui.status_provider')
            else:
                source_flag = ' --run-source-status' if manifest.get('bridge_supported') else ''
                launcher = ('#!/bin/sh\n'
                            'HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                            'exec python3 "$HERE/../.cue/scripts/codex_tui.py" --project "$HERE/.."'
                            + source_flag + ' -- "$@"\n')
                text(self.output / '.codex/cue-codex', launcher)
                (self.output / '.codex/cue-codex').chmod(0o755)
                self.finding('status-line-surface', 'effective-settings/statusLine',
                             'Stock Codex has no custom native footer provider. The generated cue-codex launcher reserves two terminal rows and renders the converted status command there.',
                             'needs-runtime-test', launcher='.codex/cue-codex')
        dump(self.output / '.cue/hook-routes.json', self.routes)
        target_events = set(self.routes)
        if 'PostToolUseFailure' in target_events:
            target_events.remove('PostToolUseFailure'); target_events.add('PostToolUse')
        if 'PreCompact' in target_events:
            target_events.add('SessionStart')
        target_events |= {'PreToolUse', 'PermissionRequest'}
        if 'statusLine' in self.settings and self.settings.get('disableAllHooks') is not True:
            target_events |= {'SessionStart', 'SessionEnd', 'PostToolUse', 'UserPromptSubmit', 'Stop'}
        if any((r.get('scope') or {}).get('skill') for rows in self.routes.values() for r in rows):
            target_events.add('PostToolUse')
        command = 'python3 ' + shlex.quote(str(self.output / '.cue/scripts/converted_hook.py'))
        hooks = {event: [{'hooks': [{'type': 'command', 'command': command,
                                   'timeout': SHORT_LIFECYCLE_TIMEOUT if event in SHORT_LIFECYCLE_EVENTS else wrapper_timeout(self.routes, event)}]}]
                 for event in sorted(target_events)}
        for event in sorted(target_events - SHORT_LIFECYCLE_EVENTS):
            self.finding('hook-wrapper-budget', 'hooks/' + event,
                         'Source command deadlines are retained, including event-specific defaults. The sequential wrapper receives the sum of candidate deadlines plus 30 seconds for adapter work.',
                         'converted', event=event, native_timeout_seconds=wrapper_timeout(self.routes, event))
        for event in sorted(target_events & {'PreToolUse', 'PostToolUse', 'PermissionRequest'}):
            source_events = [event, 'PostToolUseFailure'] if event == 'PostToolUse' else [event]
            if any(self.routes.get(source_event) for source_event in source_events):
                self.finding('hook-patch-budget', 'hooks/' + event,
                             'Multi-file apply_patch calls repeat matching source handlers for each file view. Native configuration has a fixed enclosing deadline; it cannot scale with the runtime file count. Each handler retains its deadline, but aggregate multi-file timeout parity requires a host adaptation.',
                             'manual', event=event)
        for event in sorted(target_events & SHORT_LIFECYCLE_EVENTS):
            if not self.routes.get(event):
                continue
            self.finding('hook-lifecycle-budget', 'hooks/' + event,
                         'Codex permits at most 3 seconds for this native wrapper, including normalization and all matching source handlers. '
                         'Source timeout values remain preserved; longer or sequential source work may be interrupted and needs an explicit lifecycle adaptation.',
                         'manual', event=event, native_timeout_seconds=SHORT_LIFECYCLE_TIMEOUT,
                         source_handler_count=len(self.routes.get(event, [])),
                         source_timeouts=[route['handler'].get('timeout') for route in self.routes.get(event, [])])
        dump(self.output / '.codex/hooks.json', {'description': 'Converted hooks: review compatibility-report.md before trusting.', 'hooks': hooks})
        self.finding('activation', self.output / '.codex/hooks.json', 'Restart in a trusted project and review definitions in /hooks; generated files do not imply runtime activation.', 'user-action')

    def run(self):
        try:
            from .codex_to_claude import restore_codex_original
        except ImportError:
            from codex_to_claude import restore_codex_original
        restored = restore_codex_original(self.project, self.output)
        if restored is not None:
            print(json.dumps(restored, indent=2))
            return 0
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / '.cue/scripts').mkdir(parents=True, exist_ok=True)
        self.inventory()
        self.load_settings()
        self.user_resources()
        self.hook_groups(self.settings.get('hooks', {}), 'effective-settings/hooks')
        self.instructions()
        self.convert_agents()
        self.convert_skills()
        self.config()
        self.runtime()
        dump(self.output / '.cue/conversion-findings.json', self.findings)
        report = {'converter_version': VERSION, 'source': str(self.source), 'output': str(self.output),
                  'file_count': len(self.files), 'agent_count': len(self.agents), 'skill_count': len(self.skills),
                  'manual_findings': sum(f['status'] == 'manual' for f in self.findings),
                  'runtime_unverified': sum(f['status'] == 'needs-runtime-test' for f in self.findings),
                  'source_changed': False}
        dump(self.output / '.cue/conversion-report.json', report)
        body = '# Conversion report\n\n' + json.dumps(report, indent=2) + '\n\n'
        body += 'Generated configuration is reviewable, not evidence of behavioral equivalence.\n\n'
        for f in self.findings:
            body += f'- **{f["id"]} / {f["status"]}** `{f["source"]}`: {f["rule"]}\n'
        text(self.output / '.cue/compatibility-report.md', body)
        ignore = self.output / '.cue/.gitignore'
        prior_ignore = ignore.read_text() if ignore.exists() else ''
        text(ignore, prior_ignore + '\n# Converter runtime state\nsettings-layers/\nstate/\n**/__pycache__/\n')
        for row in self.files:
            if row.get('target'):
                row['target_sha256'] = digest((self.output / row['target']).read_bytes())
        dump(self.output / '.cue/file-manifest.json', self.files)
        generated = []
        for path in sorted(self.output.rglob('*')):
            relative = path.relative_to(self.output)
            if relative.parts[0] == '.cue-source-archive':
                continue
            if path.is_symlink():
                generated.append({'path': relative.as_posix(), 'kind': 'symlink',
                                  'target': os.readlink(path)})
            elif path.is_file():
                generated.append({'path': relative.as_posix(), 'sha256': digest(path.read_bytes()),
                                  'mode': path.stat().st_mode & 0o7777})
        dump(self.output / '.cue/generated-manifest.json',
             {'schema_version': 1, 'files': generated})
        print(json.dumps(report, indent=2))
        return 2 if self.args.strict and (report['manual_findings'] or report['runtime_unverified']) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Project directory or its .claude directory')
    parser.add_argument('--output', type=Path, required=True, help='New empty output project directory')
    parser.add_argument('--global-settings', type=Path, help='Explicit user-global Claude settings layer')
    parser.add_argument('--include-user-resources', action='store_true', help='Inventory and bind global skills/agents and enabled installed plugin packages')
    parser.add_argument('--model-map', action='append', default=[], metavar='SOURCE=TARGET')
    parser.add_argument('--include-external-hooks', action='store_true', help='Keep external command routes despite manual-review findings; never executes them')
    parser.add_argument('--legacy-state-root', action='append', default=[], metavar='OLD=RELATIVE_TARGET',
                        help='Explicit status-command state mapping into .cue/state/codex; preserve original source and verify generated-copy rewrites')
    parser.add_argument('--strict', action='store_true', help='Exit 2 if any manual or unverified behavior remains; still write all review artifacts')
    parser.add_argument('--native-status', action='store_true', help='Generate native status provider configuration for the converter-patched Codex runtime')
    args = parser.parse_args()
    try:
        return Converter(args).run()
    except (ValueError, OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print('Conversion failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
