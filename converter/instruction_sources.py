"""Discover explicitly selected user instructions and nested project instructions.

This helper never executes source code, follows source symlinks, or writes global
configuration. Nested Claude instructions become nested AGENTS.md files, preserving
filesystem scope instead of promoting every nested instruction to the project root.
"""
import hashlib
import os
from pathlib import Path


EXCLUDED_DIRECTORIES = frozenset({
    '.git', '.cue', '.cue-source-archive', '.codex', '.agents',
    'node_modules', '.venv', 'venv', '__pycache__',
})


def _safe_path(path, boundary, finding):
    current = path
    while True:
        if current.is_symlink():
            finding('instruction-symlink', path,
                    'Instruction source crosses a symlink; content was not followed.', 'manual')
            return False
        if current == boundary:
            break
        if current == current.parent:
            raise ValueError('Instruction source escaped its selected boundary: ' + str(path))
        current = current.parent
    return True


def _regular(path, boundary, finding):
    if not path.exists() and not path.is_symlink():
        return False
    if not _safe_path(path, boundary, finding):
        return False
    if not path.is_file():
        finding('instruction-source', path, 'Instruction entrypoint is not a regular file.', 'manual')
        return False
    return True


def _rule_files(root, boundary, finding):
    if not root.exists() and not root.is_symlink():
        return []
    if not _safe_path(root, boundary, finding):
        return []
    result = []
    for current, directories, files in os.walk(root, followlinks=False):
        current = Path(current)
        for name in sorted(directories):
            if (current / name).is_symlink():
                finding('instruction-symlink', current / name,
                        'Rule directory is a symlink; content was not followed.', 'manual')
        directories[:] = sorted(name for name in directories if not (current / name).is_symlink())
        result.extend(current / name for name in sorted(files)
                      if name.endswith('.md') and _regular(current / name, boundary, finding))
    return result


def _safe_destination(path, output):
    if not path.is_relative_to(output):
        raise ValueError('Instruction target escaped output: ' + str(path))
    current = path
    while True:
        if current.is_symlink():
            raise ValueError('Instruction target crosses a symlink: ' + str(current))
        if current == output:
            break
        current = current.parent


def collect_instruction_supplements(project, output, *, user_root=None, finding, frontmatter, render):
    """Return inherited root sections and write safely scoped nested AGENTS.md files.

    ``finding(category, source, rule, status='converted', **detail)`` follows the
    converter's finding API. ``frontmatter(content, source)`` returns metadata/body.
    ``render(source_path, body)`` handles import expansion and path translation.
    ``user_root`` is an explicitly selected Claude user configuration directory;
    pass None when user resources were not requested.
    """
    project, output = Path(project), Path(output)
    root_sections = []
    nested_max_bytes = 0
    nested_chain_bytes = {}
    archived = {}
    files = []

    def section(path, boundary, kind, scope):
        if not _regular(path, boundary, finding):
            return None
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        source_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
        archive = output / '.cue-source-archive/instruction-sources' / source_id / path.name
        _safe_destination(archive, output)
        if archive.exists() and archive.read_bytes() != data:
            raise ValueError('Instruction archive collision: ' + str(archive))
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(data)
        archive.chmod(0o600)
        archived[path] = archive
        files.append({'source': str(path), 'sha256': digest, 'bytes': len(data),
                      'source_mode': path.stat().st_mode & 0o7777,
                      'archive': str(archive.relative_to(output)),
                      'target': str(Path(scope) / 'AGENTS.md'), 'status': 'converted',
                      'scope': str(scope), 'transformation': 'instruction-merge'})
        metadata, body = frontmatter(data.decode(), path) if kind == 'rule' else ({}, data.decode())
        body = render(path, body)
        if metadata.get('paths'):
            import json
            body = ('Apply the following only to these paths, relative to this instruction directory: '
                    + json.dumps(metadata['paths'], ensure_ascii=False) + '\n\n' + body)
            finding('scoped-rule', path,
                    'Rule paths retained as explicit applicability instructions; no identical native conditional loader is claimed.',
                    'manual', paths=metadata['paths'], scope=str(scope))
        for key in sorted(set(metadata) - {'paths'}):
            finding('instruction-metadata', path,
                    'Rule metadata preserved in its archive; no target interpretation is inferred.', 'manual', key=key)
        finding('instruction-source', path,
                'Instruction content included within its selected scope and original bytes archived.',
                scope=str(scope), source_kind=kind, sha256=digest, archive=str(archive.relative_to(output)))
        return body

    if user_root is not None:
        user_root = Path(user_root)
        if user_root.is_symlink():
            finding('instruction-symlink', user_root,
                    'Selected user instruction root is a symlink; content was not followed.', 'manual')
        elif user_root.is_dir():
            candidates = [(user_root / 'CLAUDE.md', 'instruction')]
            candidates += [(path, 'rule') for path in _rule_files(user_root / 'rules', user_root, finding)]
            for path, kind in candidates:
                body = section(path, user_root, kind, '.')
                if body is not None:
                    root_sections.append(body)
            if root_sections:
                finding('instruction-precedence', user_root,
                        'Explicitly selected user instructions precede project instructions in AGENTS.md; host instruction precedence still applies.',
                        'manual')

    if not project.is_dir():
        raise ValueError('Selected project is not a directory: ' + str(project))
    for current, directories, _ in os.walk(project, followlinks=False):
        current = Path(current)
        retained = []
        for name in sorted(directories):
            child = current / name
            if name == '.claude' or name in EXCLUDED_DIRECTORIES or child.resolve() == output.resolve():
                continue
            if child.is_symlink():
                finding('instruction-symlink', child,
                        'Nested project directory is a symlink; instructions were not searched beneath it.', 'manual')
                continue
            retained.append(name)
        directories[:] = retained
        if current == project:
            continue  # The converter already handles the primary project scope.
        scope = current.relative_to(project)
        candidates = [(current / 'CLAUDE.md', 'instruction'),
                      (current / '.claude/CLAUDE.md', 'instruction'),
                      (current / 'CLAUDE.local.md', 'instruction')]
        candidates += [(path, 'rule') for path in _rule_files(current / '.claude/rules', current, finding)]
        sections = []
        for path, kind in candidates:
            body = section(path, current, kind, scope)
            if body is not None:
                sections.append(body)
        if not sections:
            continue
        target = output / scope / 'AGENTS.md'
        _safe_destination(target, output)
        if target.exists() or target.is_symlink():
            raise ValueError('Nested instruction target collision: ' + str(target))
        if (current / 'AGENTS.md').exists() or (current / 'AGENTS.md').is_symlink():
            finding('instruction-precedence', current / 'AGENTS.md',
                    'An existing native AGENTS.md shares this scope; installation must review the proposed replacement and preserve its required instructions.',
                    'manual', target=str(target))
        content = '# Converted directory instructions\n\n' + '\n\n---\n\n'.join(sections) + '\n'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        inherited_bytes = max((size for parent, size in nested_chain_bytes.items()
                               if scope.is_relative_to(parent)), default=0)
        nested_chain_bytes[scope] = inherited_bytes + len(content.encode())
        nested_max_bytes = max(nested_max_bytes, nested_chain_bytes[scope])
        finding('nested-instructions', current,
                'Nested Claude instructions written to a directory-scoped AGENTS.md; differing host discovery and precedence require review.',
                'manual', target=str(target.relative_to(output)), scope=str(scope), bytes=len(content.encode()))
    return {'root_sections': root_sections, 'nested_max_bytes': nested_max_bytes,
            'archives': {str(path): str(target.relative_to(output)) for path, target in archived.items()},
            'files': files}
