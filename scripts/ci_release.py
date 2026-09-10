#!/usr/bin/env python3
"""GitHub release discovery and publication; no native compatibility is inferred."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import release_pipeline as pipeline

RUNNERS = {
    'aarch64-apple-darwin': 'macos-15',
    'x86_64-apple-darwin': 'macos-15-intel',
    'aarch64-unknown-linux-gnu': 'ubuntu-24.04-arm',
    'x86_64-unknown-linux-gnu': 'ubuntu-24.04',
}


def gh(*args):
    return subprocess.run(['gh', *args], check=True, capture_output=True, text=True).stdout


def api(path):
    return json.loads(gh('api', path))


def existing_release(repo, tag):
    try:
        return api('repos/' + repo + '/releases/tags/' + tag)
    except subprocess.CalledProcessError as error:
        if 'HTTP 404' in (error.stderr or ''):
            return None
        raise


def ensure_assets(repo, tag, base_url, assets, release):
    """Resume uploads without replacing an already published byte."""
    existing = {item['name'] for item in release.get('assets', [])}
    for path in assets:
        if path.name in existing:
            if hosted_digest(base_url + '/' + path.name) != pipeline.sha(path.read_bytes()):
                raise ValueError('Existing release asset differs: ' + path.name)
        else:
            gh('release', 'upload', tag, str(path), '--repo', repo)


def repository(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise ValueError('An explicit owner/repository is required')
    return value


def fetch(url, limit=4 * 1024 * 1024):
    if not url.startswith('https://'):
        raise ValueError('Publication checks require HTTPS')
    request = urllib.request.Request(url, headers={'User-Agent': 'cue-compatible-release'})
    with urllib.request.urlopen(request, timeout=60) as response:
        if not response.url.startswith('https://'):
            raise ValueError('Insecure download redirect')
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('Download exceeds configured limit')
    return body


def hosted_digest(url):
    if not url.startswith('https://'):
        raise ValueError('Publication checks require HTTPS')
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=60) as response:
        if not response.url.startswith('https://'):
            raise ValueError('Insecure download redirect')
        while data := response.read(1024 * 1024):
            size += len(data)
            if size > 2 * 1024**3:
                raise ValueError('Archive exceeds release limit')
            digest.update(data)
    return digest.hexdigest()


def outputs(values):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in values.items():
                rendered = json.dumps(value, separators=(',', ':')) if not isinstance(value, str) else value
                if '\n' in rendered or '\r' in rendered:
                    raise ValueError('Invalid workflow output')
                stream.write(key + '=' + rendered + '\n')


def discover(root, output, repo):
    repo = repository(repo)
    output.mkdir(parents=True, exist_ok=True)
    metadata = api('repos/openai/codex/releases/latest')
    ref = api('repos/openai/codex/git/ref/tags/' + metadata['tag_name'])['object']
    for _ in range(5):
        if ref['type'] == 'commit':
            break
        if ref['type'] != 'tag':
            raise ValueError('Unsupported upstream reference')
        ref = api('repos/openai/codex/git/tags/' + ref['sha'])['object']
    if ref['type'] != 'commit':
        raise ValueError('Upstream tag did not resolve to a commit')
    candidate = pipeline.discover(metadata, ref['sha'])
    version_manifest = root / 'native/manifests' / candidate['version'] / 'manifest.json'
    source_manifest = version_manifest if version_manifest.is_file() else root / 'native/manifest.json'
    manifest = pipeline.read(source_manifest)
    manifest['upstream_commit'] = candidate['commit']
    native = output / 'native'
    native.mkdir(exist_ok=True)
    for patch in [manifest] + manifest.get('additional_patches', []):
        path = pipeline.managed_update.confined(source_manifest.parent, patch['patch_file'])
        if pipeline.sha(path.read_bytes()) != patch['patch_sha256']:
            raise ValueError('Frozen patch changed')
        dest = native / patch['patch_file']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    preparation = manifest.get('sourcePreparation')
    if preparation:
        path = pipeline.managed_update.confined(source_manifest.parent, preparation['program'])
        if pipeline.sha(path.read_bytes()) != preparation['sha256']:
            raise ValueError('Frozen source preparation changed')
        dest = native / preparation['program']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    source_commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                   check=True, capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{40}', source_commit):
        raise ValueError('Invalid converter source commit')
    # Identity binds source and the updater/verification implementation, not the clock.
    binding = {'candidate': {key: candidate[key] for key in ('version', 'tag', 'commit')},
               'manifest': manifest, 'code': {}}
    for folder in ('converter', 'scripts', 'tests/native_smoke'):
        for path in sorted((root / folder).rglob('*.py')):
            binding['code'][str(path.relative_to(root))] = pipeline.sha(path.read_bytes())
    source_id = 'cue-codex-' + candidate['version'] + '-' + pipeline.sha(pipeline.canonical(binding))[:16]
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if run_id and (not run_id.isdecimal() or not attempt.isdecimal()):
        raise ValueError('Invalid build attempt identity')
    # Rebuilt logs and binaries can differ. Each CI attempt has immutable assets;
    # eligibility still compares the stable source identity to avoid hourly updates.
    release_id = source_id + ('-run-' + run_id + '-' + attempt if run_id else '')
    feed_url = 'https://github.com/' + repo + '/releases/latest/download/compatible-releases.json'
    try:
        prior = json.loads(fetch(feed_url))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        prior = {'schemaVersion': 1, 'manager': pipeline.managed_update.MANAGER, 'releases': []}
    if prior.get('schemaVersion') != 1 or prior.get('manager') != pipeline.managed_update.MANAGER:
        raise ValueError('Invalid published feed')
    sequence = max([row['sequence'] for row in prior['releases']] + [manifest.get('release_sequence', 0)]) + 1
    latest = {target: max((row for row in prior['releases'] if row.get('target') == target),
                          key=lambda row: row['sequence'], default=None) for target in RUNNERS}
    current = all(row is not None and
                  (row['releaseId'] == source_id + '-' + target or
                   row['releaseId'].startswith(source_id + '-run-') and row['releaseId'].endswith('-' + target))
                  for target, row in latest.items())
    pipeline.write(output / 'candidate.json', candidate)
    pipeline.write(native / 'manifest.json', manifest)
    pipeline.write(output / 'prior-feed.json', prior)
    context = {'repository': repo, 'sourceCommit': source_commit, 'releaseId': release_id, 'sequence': sequence,
               'feedUrl': feed_url, 'baseUrl': 'https://github.com/' + repo + '/releases/download/' + release_id,
               'targets': sorted(RUNNERS), 'state': 'current' if current else 'candidate'}
    pipeline.write(output / 'context.json', context)
    outputs({'build': 'false' if current else 'true', 'release_id': release_id,
             'sequence': str(sequence), 'feed_url': feed_url,
             'matrix': {'include': [{'target': target, 'runner': runner} for target, runner in RUNNERS.items()]}})
    print(json.dumps(context))


def publish(work, artifacts, repo):
    repo = repository(repo)
    context = pipeline.read(work / 'context.json')
    if context['repository'] != repo:
        raise ValueError('Publication repository drift')
    if sorted(context['targets']) != sorted(RUNNERS):
        raise ValueError('Publication requires every supported target')
    if not re.fullmatch(r'[0-9a-f]{40}', context['sourceCommit']):
        raise ValueError('Invalid converter source commit')
    records, assets = [], []
    for target in context['targets']:
        directory = artifacts / ('runtime-' + target)
        record = pipeline.read(directory / 'release.json')
        if record['target'] != target or record['releaseId'] != context['releaseId'] + '-' + target or record['sequence'] != context['sequence']:
            raise ValueError('Runner release identity mismatch')
        archive = directory / record['archive']['location'].rsplit('/', 1)[-1]
        evidence = directory / 'evidence.json'
        if record['archive']['location'] != context['baseUrl'] + '/' + archive.name:
            raise ValueError('Runner changed archive origin')
        if pipeline.sha(archive.read_bytes()) != record['archive']['sha256'] or pipeline.sha(pipeline.canonical(pipeline.read(evidence))) != record['evidenceSha256']:
            raise ValueError('Runner artifact digest mismatch')
        named_evidence = directory / ('evidence-' + target + '.json')
        named_evidence.write_bytes(pipeline.canonical(pipeline.read(evidence)))
        records.append(record)
        assets.extend([archive, named_evidence])
    tag = context['releaseId']
    # A prerelease is downloadable for verification but never the stable latest release.
    release = existing_release(repo, tag)
    if release is None:
        gh('release', 'create', tag, '--repo', repo, '--target', context['sourceCommit'], '--prerelease', '--title', tag,
           '--notes', 'Compatibility-verified Codex runtime; evidence accompanies each target.')
        release = {'assets': []}
    else:
        if release.get('target_commitish') != context['sourceCommit'] or release.get('draft'):
            raise ValueError('Existing release source identity differs')
    ensure_assets(repo, tag, context['baseUrl'], assets, release)
    published = {}
    for record in records:
        url = record['archive']['location']
        evidence_url = context['baseUrl'] + '/evidence-' + record['target'] + '.json'
        if hosted_digest(url) != record['archive']['sha256'] or pipeline.sha(fetch(evidence_url)) != record['evidenceSha256']:
            raise ValueError('Hosted release verification failed; release remains prerelease')
        published[url] = {'verified': True, 'sha256': record['archive']['sha256'], 'evidenceSha256': record['evidenceSha256']}
    feed = work / 'compatible-releases.json'
    pipeline.write(feed, pipeline.read(work / 'prior-feed.json'))
    pipeline.advance(feed, records, published)
    ensure_assets(repo, tag, context['baseUrl'], [feed], release)
    if fetch(context['baseUrl'] + '/compatible-releases.json') != feed.read_bytes():
        raise ValueError('Hosted feed differs; release remains prerelease')
    gh('release', 'edit', tag, '--repo', repo, '--prerelease=false', '--latest')
    if fetch(context['feedUrl']) != feed.read_bytes():
        raise ValueError('Latest feed did not advance to verified release')
    print(json.dumps({'state': 'published', 'releaseId': tag, 'feedUrl': context['feedUrl']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['discover', 'publish', 'report-failure'])
    parser.add_argument('--repository', required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    if args.command == 'discover':
        discover(Path(__file__).resolve().parents[1], args.work, args.repository)
    elif args.command == 'publish':
        if args.artifacts is None:
            parser.error('--artifacts is required for publication')
        publish(args.work, args.artifacts, args.repository)
    else:
        repo = repository(args.repository)
        title = 'Codex compatibility verification failed'
        prior = json.loads(gh('issue', 'list', '--repo', repo, '--state', 'open',
                              '--search', title + ' in:title', '--json', 'title,url'))
        if any(item['title'] == title for item in prior):
            print('An open compatibility failure issue already exists; current workflow logs retain this failure.')
            return
        args.work.mkdir(parents=True, exist_ok=True)
        body = args.work / 'issue.md'
        run_url = os.environ.get('RUN_URL', '')
        if not run_url.startswith('https://github.com/' + repo + '/actions/runs/'):
            raise ValueError('Invalid workflow run URL')
        body.write_text('Compatibility release did not complete. Inspect the failed job and hosted feed state before retrying.\n\nRun: ' + run_url + '\n')
        gh('issue', 'create', '--repo', repo, '--title', title, '--body-file', str(body))


if __name__ == '__main__':
    main()
