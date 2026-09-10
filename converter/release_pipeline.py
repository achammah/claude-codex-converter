"""Offline compatible-release gates; publishing and native builds are separate."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.parse
import zipfile

from . import managed_update
from . import normalize_workspace_lock

TARGETS = {
    'aarch64-apple-darwin', 'x86_64-apple-darwin',
    'aarch64-unknown-linux-gnu', 'x86_64-unknown-linux-gnu',
}
CHECKS = {'native_tests', 'cli_helper_execution', 'question_ui', 'update_route', 'update_rollback'}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def write(path, value):
    """Replace a complete document, never expose a partially written feed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def discover(metadata, commit):
    """Normalize explicit upstream release metadata; never infer compatibility."""
    if metadata.get('draft') or metadata.get('prerelease'):
        raise ValueError('Only published stable release candidates are accepted')
    tag = metadata.get('tag_name', '')
    match = re.fullmatch(r'(?:rust-v|v)?(\d+\.\d+\.\d+)', tag)
    if not match or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Candidate requires an exact version tag and resolved commit')
    url = metadata.get('html_url', '')
    if not url.startswith('https://github.com/openai/codex/releases/tag/'):
        raise ValueError('Candidate provenance must name the upstream release')
    return {'schemaVersion': 1, 'state': 'candidate', 'version': match[1],
            'tag': tag, 'commit': commit, 'source': url, 'metadataSha256': sha(canonical(metadata))}


def apply_candidate_source(source, manifest_path):
    """Prepare a disposable candidate checkout; caller owns its mutations."""
    source, manifest_path = Path(source).resolve(), Path(manifest_path).resolve()
    manifest = read(manifest_path)
    patches = [manifest] + manifest.get('additional_patches', [])
    actual = subprocess.run(['git','rev-parse','HEAD'],cwd=source,check=True,capture_output=True,text=True).stdout.strip()
    if actual != manifest.get('upstream_commit'):
        raise ValueError('Source does not match pinned commit')
    if tuple(p.get('feature_marker') for p in patches) != managed_update.REQUIRED_MARKERS:
        raise ValueError('Required ordered patch contract is missing')
    prepared = None
    preparation = manifest.get('sourcePreparation')
    if preparation is not None:
        if preparation.get('program') != 'normalize_workspace_lock.py':
            raise ValueError('Unknown candidate source preparation')
        bundled = Path(normalize_workspace_lock.__file__).read_bytes()
        supplied = managed_update.confined(manifest_path.parent, preparation['program']).read_bytes()
        if supplied != bundled or sha(bundled) != preparation.get('sha256'):
            raise ValueError('Source preparation digest differs from bundled normalizer')
        prepared = normalize_workspace_lock.normalize(source)
    records = []
    for entry in patches:
        path = managed_update.confined(manifest_path.parent, entry['patch_file'])
        data = path.read_bytes()
        if sha(data) != entry['patch_sha256']: raise ValueError('Patch digest mismatch')
        for arguments in (['git','apply','--check',str(path)], ['git','apply',str(path)]):
            subprocess.run(arguments, cwd=source, check=True, capture_output=True, text=True)
        records.append({'file':entry['patch_file'],'sha256':sha(data),'marker':entry['feature_marker']})
    return {'patches':records,'sourcePreparation':prepared}


def validate_patches(source, manifest_path):
    """Apply patches to an isolated checkout of the exact pinned commit."""
    source, manifest_path = Path(source).resolve(), Path(manifest_path).resolve()
    manifest = read(manifest_path)
    commit = manifest['upstream_commit']
    if not re.fullmatch(r'[a-f0-9]{40}', commit): raise ValueError('Invalid pinned commit')
    patches = [manifest] + manifest.get('additional_patches', [])
    if tuple(p.get('feature_marker') for p in patches) != managed_update.REQUIRED_MARKERS:
        raise ValueError('Required ordered patch contract is missing')
    def run(args, cwd):
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    if run(['git', 'rev-parse', 'HEAD'], source).stdout.strip() != commit:
        raise ValueError('Source does not match pinned commit')
    with tempfile.TemporaryDirectory(prefix='cue-patch-validation-') as tmp:
        run(['git', 'clone', '--quiet', '--no-hardlinks', '--no-checkout', str(source), tmp], source)
        run(['git', 'checkout', '--quiet', '--detach', commit], tmp)
        application = apply_candidate_source(tmp, manifest_path)
        diff = run(['git', 'diff', '--binary', 'HEAD'], tmp).stdout.encode()
    return {'schemaVersion': 1, 'passed': True, 'commit': commit,
            'manifestSha256': sha(manifest_path.read_bytes()), **application,
            'patchedDiffSha256': sha(diff), 'scope': 'patch application only; no build or runtime claim'}


def inventory(root):
    root = Path(root)
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink() or not (p.is_dir() or p.is_file()):
            raise ValueError('Package contains a symlink or special file')
        if p.is_file():
            result[p.relative_to(root).as_posix()] = {'sha256': sha(p.read_bytes()), 'mode': p.stat().st_mode & 0o777}
    return result


def package(root, candidate, patch_proof, evidence, output, base_url):
    """Validate runner evidence and bind it to deterministic complete-package bytes.

    Evidence is a trusted runner's report, not a cryptographic attestation authority.
    The publishing job must obtain it from its own successful jobs.
    """
    root, output = Path(root).resolve(), Path(output)
    if output.resolve().is_relative_to(root): raise ValueError('Output must be outside the package')
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Publication base requires an explicit HTTPS directory URL')
    _, meta, cue = managed_update.package_info(root / 'codex-package.json')
    target = meta['target']
    if target not in TARGETS: raise ValueError('Unknown release target')
    if candidate.get('state') != 'candidate' or candidate.get('version') != meta['version']:
        raise ValueError('Candidate version differs from package')
    if patch_proof.get('passed') is not True or patch_proof.get('commit') != candidate.get('commit'):
        raise ValueError('Patch proof does not match candidate')
    if tuple(p.get('marker') for p in patch_proof.get('patches', [])) != managed_update.REQUIRED_MARKERS:
        raise ValueError('Patch proof lacks required patches')
    files = inventory(root)
    required = {'bin/codex', 'bin/codex-code-mode-host', 'codex-path/rg'}
    if 'linux' in target: required.add('bin/codex-linux-sandbox')
    if not required.issubset(files): raise ValueError('Incomplete package companions')
    if any(m.encode() not in (root/'bin/codex').read_bytes() for m in managed_update.REQUIRED_MARKERS):
        raise ValueError('Missing native marker')
    binding = {'candidateSha256': sha(canonical(candidate)), 'patchProofSha256': sha(canonical(patch_proof)),
               'inventorySha256': sha(canonical(files)), 'target': target,
               'releaseId': cue['releaseId'], 'sequence': cue['sequence']}
    if evidence.get('binding') != binding: raise ValueError('Runner evidence does not match package inputs')
    checks = evidence.get('checks', {})
    if set(checks) != CHECKS or any(checks[k].get('passed') is not True or not re.fullmatch(r'[a-f0-9]{64}', checks[k].get('logSha256', '')) for k in CHECKS):
        raise ValueError('Required runtime checks have not passed')
    output.mkdir(parents=True, exist_ok=True)
    archive_name = 'runtime-' + target + '-' + str(cue['sequence']) + '.zip'
    archive = output/archive_name
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        staged = Path(tmp)/archive_name
        with zipfile.ZipFile(staged, 'w') as z:
            for relative, item in files.items():
                info = zipfile.ZipInfo(relative, date_time=(1980,1,1,0,0,0))
                info.create_system = 3; info.external_attr = (0o100000 | item['mode']) << 16
                info.compress_type = zipfile.ZIP_STORED
                z.writestr(info, (root/relative).read_bytes())
        if inventory(root) != files: raise ValueError('Package changed during archival')
        if archive.exists() and archive.read_bytes() != staged.read_bytes(): raise ValueError('Immutable archive already exists with different bytes')
        os.replace(staged, archive)
    return {'releaseId': cue['releaseId'], 'sequence': cue['sequence'], 'version': meta['version'], 'target': target,
            'compatibility': {'validated': True, 'markers': list(managed_update.REQUIRED_MARKERS)},
            'archive': {'location': base_url.rstrip('/')+'/'+archive_name, 'sha256': sha(archive.read_bytes())},
            'evidenceSha256': sha(canonical(evidence)), 'binding': binding}


def advance(feed_path, releases, published):
    """Advance only after trusted publisher verifies hosted archive/evidence hashes."""
    feed_path = Path(feed_path)
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    with managed_update.installation_lock(feed_path):
        return _advance_locked(feed_path, releases, published)


def _advance_locked(feed_path, releases, published):
    feed = read(feed_path) if feed_path.exists() else {'schemaVersion': 1, 'manager': managed_update.MANAGER, 'releases': []}
    if feed.get('schemaVersion') != 1 or feed.get('manager') != managed_update.MANAGER: raise ValueError('Invalid existing feed')
    rows = list(feed['releases'])
    for row in releases:
        proof = published.get(row['archive']['location'], {})
        if proof != {'sha256': row['archive']['sha256'], 'evidenceSha256': row['evidenceSha256'], 'verified': True}:
            raise ValueError('Publication is not verified')
        if row['target'] not in TARGETS or row['compatibility'] != {'validated':True,'markers':list(managed_update.REQUIRED_MARKERS)}:
            raise ValueError('Invalid compatible release')
        prior = [r for r in rows if r['target'] == row['target']]
        if row in prior: continue
        if type(row['sequence']) is not int or row['sequence'] < 1 or any(r['sequence'] >= row['sequence'] for r in prior):
            raise ValueError('Release sequence does not advance')
        rows.append(row)
    feed['releases'] = sorted(rows, key=lambda r:(r['target'],r['sequence']))
    write(feed_path, feed)
    return feed


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('discover');p.add_argument('--metadata',type=Path,required=True);p.add_argument('--commit',required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('patch-check');p.add_argument('--source',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('package')
    for name in ['root','candidate','patch-proof','evidence','output']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--base-url',required=True);p.add_argument('--record',type=Path,required=True)
    p=sub.add_parser('advance');p.add_argument('--feed',type=Path,required=True);p.add_argument('--releases',type=Path,required=True);p.add_argument('--published',type=Path,required=True)
    a=parser.parse_args(argv)
    if a.command=='discover':write(a.output,discover(read(a.metadata),a.commit))
    elif a.command=='patch-check':write(a.output,validate_patches(a.source,a.manifest))
    elif a.command=='package':write(a.record,package(a.root,read(a.candidate),read(a.patch_proof),read(a.evidence),a.output,a.base_url))
    else:advance(a.feed,read(a.releases),read(a.published))
    return 0


if __name__=='__main__':raise SystemExit(main())
