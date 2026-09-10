#!/usr/bin/env python3
"""Build a distributable zipapp and source ZIP; bundles pure-Python PyYAML."""
import argparse
import hashlib
from pathlib import Path
import shutil
import tempfile
import zipfile
import yaml
from scripts.export_public import ALLOWLIST, export

ROOT = Path(__file__).resolve().parent
BOOTSTRAP = '''import pathlib, runpy, sys, tempfile, zipfile
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required.")
with tempfile.TemporaryDirectory(prefix="claude-codex-converter-") as directory:
    with zipfile.ZipFile(sys.argv[0]) as archive:
        archive.extractall(directory)
    sys.path.insert(0, directory)
    from cue_converter.cli import main
    raise SystemExit(main())
'''


def member(archive, name, data):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data, compresslevel=9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='converter-build-') as temp:
        work = Path(temp)
        reviewed = work / 'reviewed-source'
        export(ROOT, reviewed, work / 'public-scan.json')
        app_work = work / 'app'
        app_work.mkdir()
        shutil.copytree(reviewed / 'converter', app_work / 'cue_converter', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        if (reviewed / 'native').is_dir():
            shutil.copytree(reviewed / 'native', app_work / 'cue_converter/native',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        yaml_dir = Path(yaml.__file__).resolve().parent
        shutil.copytree(yaml_dir, app_work / 'yaml', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.so', '*.pyd'))
        licenses = list(yaml_dir.parent.glob('PyYAML-*.dist-info/licenses/LICENSE')) + list(yaml_dir.parent.glob('pyyaml-*.dist-info/licenses/LICENSE'))
        if not licenses:
            raise SystemExit('PyYAML license not found; refusing to distribute an unlicensed dependency bundle.')
        shutil.copy2(licenses[0], app_work / 'PyYAML-LICENSE')
        shutil.copy2(reviewed / 'LICENSE', app_work / 'LICENSE')
        (app_work / '__main__.py').write_text(BOOTSTRAP)
        app = args.output / 'claude-codex-converter.pyz'
        with app.open('wb') as stream:
            stream.write(b'#!/usr/bin/env python3\n')
            with zipfile.ZipFile(stream, 'w') as archive:
                for path in sorted(app_work.rglob('*')):
                    if path.is_file():
                        member(archive, str(path.relative_to(app_work)), path.read_bytes())
        app.chmod(0o755)
        source = args.output / 'claude-codex-converter-source.zip'
        with zipfile.ZipFile(source, 'w', zipfile.ZIP_DEFLATED) as archive:
            for relative in sorted(ALLOWLIST):
                member(archive, 'claude-codex-converter/' + relative, (reviewed / relative).read_bytes())
    checksum = args.output / 'SHA256SUMS'
    checksum.write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in (app, source)))
    print(app)
    print(source)


if __name__ == '__main__':
    main()
