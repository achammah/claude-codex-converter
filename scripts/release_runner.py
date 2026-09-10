#!/usr/bin/env python3
"""Build and exercise a compatible release on the current host; never publish it.

Every run uses a new work directory. Failure leaves logs and no passing evidence.
CI must retain the work directory and pass evidence to release_pipeline.package.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from converter import managed_update, native_runtime, release_pipeline as pipeline


def execute(argv, cwd, log, env=None):
    """Stream bounded-memory build output to a durable log; propagate failure."""
    with Path(log).open('xb') as stream:
        stream.write((json.dumps({'argv': argv, 'cwd': str(cwd)})+'\n').encode())
        stream.flush()
        subprocess.run(argv, cwd=cwd, env=env, stdout=stream,
                       stderr=subprocess.STDOUT, check=True)
    return pipeline.sha(Path(log).read_bytes())


def companion_contract(version, target):
    arch = 'arm64' if target.startswith('aarch64-') else 'x64'
    key = ('darwin' if 'apple' in target else 'linux') + '-' + arch
    package_version = version + '-' + key
    url = 'https://registry.npmjs.org/@openai%2fcodex/' + package_version
    with urllib.request.urlopen(url, timeout=60) as response:
        if not response.url.startswith('https://registry.npmjs.org/'):
            raise ValueError('Unexpected npm metadata redirect')
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('npm metadata exceeds bound')
    meta = json.loads(raw)
    dist = meta.get('dist', {})
    expected = 'https://registry.npmjs.org/@openai/codex/-/codex-' + package_version + '.tgz'
    if (meta.get('name') != '@openai/codex' or meta.get('version') != package_version
            or dist.get('tarball') != expected
            or not re.fullmatch(r'sha512-[A-Za-z0-9+/]{86}==', dist.get('integrity', ''))):
        raise ValueError('Official companion metadata does not match candidate')
    return {'package': '@openai/codex', 'version': package_version, 'url': expected,
            'integrity': dist['integrity'], 'platform': key,
            'member': 'package/vendor/' + target.replace('linux-gnu', 'linux-musl') + '/bin/codex-code-mode-host',
            'metadataSha256': pipeline.sha(raw)}


def smoke(name, argv, work, env=None):
    directory = work / name
    log = work / (name + '.log')
    digest = execute(argv + ['--work', str(directory)], ROOT, log, env)
    report = pipeline.read(directory / 'report.json')
    if report.get('passed') is not True:
        raise ValueError(name + ' did not report a passing runtime observation')
    return {'passed': True, 'logSha256': digest,
            'reportSha256': pipeline.sha((directory/'report.json').read_bytes())}


def stage_manager(package, metadata, *, target, release_id, sequence, feed_url):
    """Use the caller's complete release identity without adding another suffix."""
    managed_update.stage_manager(package, metadata, target=target,
        runtime_source=Path(native_runtime.__file__), release_id=release_id,
        sequence=sequence, feed_url=feed_url)


def attribution_name(name):
    return bool(re.match(r'^(LICENSE|LICENCE|NOTICE|COPYING|COPYRIGHT|UNLICENSE)(?:[._-].*)?$',
                         Path(name).name, re.IGNORECASE))


def copy_archive_attributions(archive, destination):
    """Preserve regular attribution files from an already integrity-checked tar."""
    records = []
    total = 0
    with tarfile.open(archive, 'r:*') as tar:
        seen = set()
        for member in tar.getmembers():
            if not attribution_name(member.name):
                continue
            if member.isdir():
                continue
            if not member.isfile() or member.name in seen:
                raise ValueError('Nonregular or duplicate archive attribution')
            seen.add(member.name)
            total += member.size
            if member.size < 0 or total > 16 * 1024 * 1024:
                raise ValueError('Archive attribution exceeds size bound')
            target = managed_update.confined(destination, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = tar.extractfile(member).read()
            target.write_bytes(data)
            target.chmod(0o644)
            records.append({'path': member.name, 'sha256': pipeline.sha(data)})
    return records


def download_attribution_archive(contract, archive, algorithm):
    """Re-read the pinned distribution to preserve its bundled attribution."""
    digest = hashlib.new(algorithm)
    size = 0
    with urllib.request.urlopen(contract['url'], timeout=60) as response, archive.open('xb') as out:
        if not response.url.startswith('https://'):
            raise ValueError('Attribution archive redirected outside HTTPS')
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 512 * 1024 * 1024:
                raise ValueError('Attribution archive exceeds bound')
            digest.update(chunk); out.write(chunk)
    expected = contract['integrity'] if algorithm == 'sha512' else contract['sha256']
    actual = 'sha512-'+base64.b64encode(digest.digest()).decode() if algorithm == 'sha512' else digest.hexdigest()
    if actual != expected:
        raise ValueError('Attribution archive integrity mismatch')
    return {'url': contract['url'], 'integrity': actual, 'bytes': size}


def preserve_attributions(source, package, companion, ripgrep, work):
    """Preserve supplied attribution; this is not a transitive license audit."""
    license_root = package/'licenses'
    license_root.mkdir()
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=source).decode().split('\0')
    records = []
    for relative in filter(None, tracked):
        if not attribution_name(relative):
            continue
        original = managed_update.confined(source, relative)
        if not original.resolve().is_relative_to(source.resolve()) or not original.is_file():
            raise ValueError('Source attribution is not a regular file: '+relative)
        target = managed_update.confined(license_root/'codex', relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        records.append({'path': relative, 'sha256': pipeline.sha(target.read_bytes())})
    for name in ('LICENSE', 'NOTICE'):
        if not (license_root/'codex'/name).is_file():
            raise ValueError('Upstream source lacks required root attribution: '+name)
        shutil.copyfile(license_root/'codex'/name, package/name)
    evidence = {'scope': 'available upstream source and binary-distribution attribution; not a transitive dependency audit',
                'source': records, 'archives': {}}
    for name, contract, algorithm in (('official-companions', companion, 'sha512'), ('ripgrep', ripgrep, 'sha256')):
        archive = work/(name+'-attribution.tar.gz')
        provenance = download_attribution_archive(contract, archive, algorithm)
        provenance['files'] = copy_archive_attributions(archive, license_root/name)
        provenance['attributionPresent'] = bool(provenance['files'])
        evidence['archives'][name] = provenance
    (package/'CUE-MODIFICATIONS.txt').write_text(
        'This distribution modifies upstream OpenAI Codex with the SHA-pinned patches recorded in the release evidence.\n'
        'The patches add the status provider, question form, and managed updater.\n'
        'The bundled code-mode companion and ripgrep are obtained from their recorded upstream distributions.\n'
        'Original available attribution is preserved under licenses/.\n', encoding='utf-8')
    pipeline.write(license_root/'provenance.json', evidence)
    return evidence


def run(args):
    candidate = pipeline.read(args.candidate)
    if (candidate.get('state') != 'candidate'
            or not re.fullmatch(r'[a-f0-9]{40}', candidate.get('commit', ''))
            or not re.fullmatch(r'\d+\.\d+\.\d+', candidate.get('version', ''))
            or candidate.get('tag') not in ('rust-v'+candidate['version'], 'v'+candidate['version'], candidate['version'])):
        raise ValueError('Expected independently resolved stable candidate')
    if args.sequence < 1 or not args.release_id.strip():
        raise ValueError('Explicit positive release sequence and identity required')
    target = native_runtime._host_package_target()[0]
    if target not in pipeline.TARGETS:
        raise ValueError('Unsupported native host')
    work, output = args.work.resolve(), args.output.resolve()
    work.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    source = work/'source'
    source.mkdir()
    execute(['git', 'init', str(source)], work, work/'git-init.log')
    execute(['git', '-C', str(source), 'fetch', '--depth=1', native_runtime.UPSTREAM_REPOSITORY,
             'refs/tags/'+candidate['tag']], work, work/'git-fetch.log')
    execute(['git', '-C', str(source), 'checkout', '--detach', 'FETCH_HEAD'], work, work/'git-checkout.log')
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != candidate['commit']:
        raise ValueError('Fetched release tag differs from candidate commit')
    manifest = pipeline.read(args.manifest)
    pipeline.write(output/'input-manifest.json', manifest)
    bundle = work/'patches'; bundle.mkdir()
    for patch in [manifest] + manifest.get('additional_patches', []):
        original = managed_update.confined(args.manifest.resolve().parent, patch['patch_file'])
        destination = managed_update.confined(bundle, patch['patch_file'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    preparation = manifest.get('sourcePreparation')
    if preparation:
        original = managed_update.confined(args.manifest.resolve().parent, preparation['program'])
        destination = managed_update.confined(bundle, preparation['program'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    manifest['upstream_commit'] = candidate['commit']
    manifest['release_id'], manifest['release_sequence'] = args.release_id, args.sequence
    manifest_path = bundle/'manifest.json'; pipeline.write(manifest_path, manifest)
    proof = pipeline.validate_patches(source, manifest_path)
    pipeline.write(output/'patch-proof.json', proof)
    application = pipeline.apply_candidate_source(source, manifest_path)
    pipeline.write(work/'source-application.json', application)
    diff = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], cwd=source)
    if pipeline.sha(diff) != proof['patchedDiffSha256']:
        raise ValueError('Build source differs from the independently checked patch application')
    cargo_root = source/'codex-rs'
    cargo = tomllib.loads((cargo_root/'Cargo.toml').read_text())
    if cargo['workspace']['package']['version'] != candidate['version']:
        raise ValueError('Source Cargo version differs from release candidate')
    env = dict(os.environ)
    for key in ('CARGO_BUILD_TARGET', 'RUSTC_WRAPPER', 'RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS'):
        env.pop(key, None)
    env.update(CARGO_BUILD_JOBS='1', CARGO_TARGET_DIR=str(work/'target'))
    execute(['cargo', 'build', '--locked', '--release', '-j1', '--bin', 'codex'], cargo_root, work/'build.log', env)
    package = output/'package'; (package/'bin').mkdir(parents=True); (package/'codex-path').mkdir()
    shutil.copy2(work/'target/release/codex', package/'bin/codex')
    binary = package/'bin/codex'
    if any(marker.encode() not in binary.read_bytes() for marker in managed_update.REQUIRED_MARKERS):
        raise ValueError('Compiled CLI lacks a required native patch marker')
    version = subprocess.check_output([str(binary), '--version'], text=True, timeout=30).strip()
    if version != 'codex-cli '+candidate['version']:
        raise ValueError('Compiled CLI version differs from candidate')
    contract = companion_contract(candidate['version'], target)
    pinned_companion = manifest.get('official_companion_packages', {}).get(contract['platform'])
    if pinned_companion != {key: contract[key] for key in ('version', 'url', 'integrity')}:
        raise ValueError('Candidate companion metadata differs from the reviewed manifest pin')
    pipeline.write(output/'companion-contract.json', contract)
    native_runtime._fetch_official_companion(contract, package/'bin/codex-code-mode-host')
    if 'linux' in target:
        sandbox = dict(contract, member=contract['member'].replace('codex-code-mode-host', 'codex-linux-sandbox'))
        native_runtime._fetch_official_companion(sandbox, package/'bin/codex-linux-sandbox')
    ripgrep = native_runtime._ripgrep_contract(source)
    native_runtime._fetch_ripgrep(ripgrep, package/'codex-path/rg')
    preserve_attributions(source, package, contract, ripgrep, work)
    execute([str(package/'bin/codex-code-mode-host'), '--help'], work, work/'helper-launch.log')
    execute([str(package/'codex-path/rg'), '--version'], work, work/'rg-launch.log')
    metadata = {'layoutVersion': 1, 'version': candidate['version'], 'target': target,
                'variant': 'codex', 'entrypoint': 'bin/codex', 'resourcesDir': 'codex-resources', 'pathDir': 'codex-path'}
    stage_manager(package, metadata, target=work/'installed/codex', release_id=args.release_id,
        sequence=args.sequence, feed_url=args.feed_url)
    pipeline.write(package/'codex-package.json', metadata)
    before = pipeline.inventory(package)
    native_log = work/'native-tests.log'
    native_digest = execute(['cargo', 'test', '--locked', '--release', '-j1', '-p',
        'codex-install-context', '-p', 'codex-tui', '--lib'], cargo_root, native_log, env)
    if not re.search(rb'test result: ok\. [1-9][0-9]* passed;', native_log.read_bytes()):
        raise ValueError('Native test command did not execute any passing tests')
    checks = {'native_tests': {'passed': True, 'logSha256': native_digest}}
    focused = smoke('native_focused', [sys.executable, str(ROOT/'tests/native_smoke/native_tests.py'),
        '--source', str(source), '--cargo', 'cargo', '--target-dir', str(work/'focused-target')], work, env)
    native_checks = {'full_library_tests': checks['native_tests'], 'focused_actual_modules': focused}
    pipeline.write(work/'native-tests-combined.json', native_checks)
    checks['native_tests'] = {'passed': True,
        'logSha256': pipeline.sha((work/'native-tests-combined.json').read_bytes()), 'subchecks': native_checks}
    for name, script, option, path in (
        ('cli_helper_execution', 'helper.py', '--binary', package/'bin/codex'),
        ('question_ui', 'question.py', '--package', package),
        ('update_route', 'update_route.py', '--package', package),
        ('update_rollback', 'update_package.py', '--package', package)):
        checks[name] = smoke(name, [sys.executable, str(ROOT/'tests/native_smoke'/script), option, str(path)], work)
    prompt = smoke('update_prompt', [sys.executable, str(ROOT/'tests/native_smoke/update_prompt.py'), '--package', str(package)], work)
    route = checks['update_route']
    combined = {'route': route, 'prompt': prompt}
    pipeline.write(work/'update-route-combined.json', combined)
    checks['update_route'] = {'passed': True, 'logSha256': pipeline.sha((work/'update-route-combined.json').read_bytes()),
                              'subchecks': combined}
    if pipeline.inventory(package) != before:
        raise ValueError('Runtime checks mutated candidate package')
    cue = metadata['cueUpdate']
    evidence = {'schemaVersion': 1, 'scope': 'native current host only; no cross-platform claim',
        'binding': {'candidateSha256': pipeline.sha(pipeline.canonical(candidate)),
        'patchProofSha256': pipeline.sha(pipeline.canonical(proof)),
        'inventorySha256': pipeline.sha(pipeline.canonical(before)), 'target': target,
        'releaseId': cue['releaseId'], 'sequence': cue['sequence']}, 'checks': checks}
    pipeline.write(output/'candidate.json', candidate)
    pipeline.write(output/'evidence.json', evidence)
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'manifest', 'work', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--release-id', required=True, help='Complete immutable release identity, including target if desired; used verbatim')
    parser.add_argument('--sequence', type=int, required=True)
    parser.add_argument('--feed-url')
    args = parser.parse_args(argv)
    try:
        run(args)
    except Exception as exc:
        print('Release held: '+str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
