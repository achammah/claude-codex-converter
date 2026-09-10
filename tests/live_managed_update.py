"""Explicit scratch update/rollback probe using a real, already-compiled runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import managed_update
from converter import native_runtime


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    original=args.package.resolve()
    expected={rel:hashlib.sha256((original/rel).read_bytes()).hexdigest() for rel in ('bin/codex','bin/codex-code-mode-host','codex-path/rg')}
    evidence={'scope':'real compiled package in scratch; patch-only sequence increment is synthetic','source_hashes':expected,'checks':[]}
    with tempfile.TemporaryDirectory(prefix='cue-real-managed-update-') as directory:
        root=Path(directory).resolve();visible=root/'visible';visible.mkdir();target=visible/'codex'
        old=root/'old';candidate=root/'candidate'
        for package,sequence in ((old,2),(candidate,3)):
            (package/'bin').mkdir(parents=True);(package/'codex-path').mkdir()
            for rel in expected:shutil.copy2(original/rel,package/rel)
            metadata=json.loads((original/'codex-package.json').read_text())
            managed_update.stage_manager(package,metadata,target=target,runtime_source=Path(native_runtime.__file__),release_id='real-package-fixture-'+str(sequence),sequence=sequence)
            (package/'codex-package.json').write_text(json.dumps(metadata,indent=2)+'\n')
        for name in ('codex','codex-code-mode-host'):(visible/name).symlink_to(old/'bin'/name)
        config=root/'project/.codex/config.toml';config.parent.mkdir(parents=True);config.write_bytes(b'fixture_setting = true\n')
        config_before=config.read_bytes()
        archive=root/'candidate.zip'
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
            for p in sorted(candidate.rglob('*')):
                if p.is_file():z.write(p,p.relative_to(candidate))
        feed=json.loads((candidate/'codex-resources/compatible-releases.json').read_text())
        feed['releases'][0]['archive']={'location':str(archive),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
        descriptor=root/'releases.json';descriptor.write_text(json.dumps(feed))
        command=[sys.executable,str(old/'codex-resources/cue-update'),'update','--installation',str(old/'codex-package.json'),'--source',str(descriptor)]
        run=subprocess.run(command,cwd='/',capture_output=True,text=True,timeout=180)
        evidence['update']={'returncode':run.returncode,'stdout':run.stdout,'stderr':run.stderr}
        assert run.returncode==0,run.stderr
        result=json.loads(run.stdout);assert result['state']=='installed'
        installed=Path(result['installation']).parent
        for rel,sha in expected.items():assert hashlib.sha256((installed/rel).read_bytes()).hexdigest()==sha
        assert target.resolve()==installed/'bin/codex'
        assert (visible/'codex-code-mode-host').resolve()==installed/'bin/codex-code-mode-host'
        version=subprocess.run([str(target),'--version'],capture_output=True,text=True,timeout=30)
        assert version.returncode==0 and '0.153.4' in version.stdout
        assert config.read_bytes()==config_before
        evidence['checks'].extend(['real complete package updated','CLI/helper/rg hashes preserved','installed CLI launches','project configuration unchanged'])
        evidence['receipt']=json.loads(Path(result['receipt']).read_text())
        rollback=subprocess.run([sys.executable,str(installed/'codex-resources/cue-update'),'rollback','--installation',str(installed/'codex-package.json'),'--receipt',result['receipt']],cwd='/',capture_output=True,text=True,timeout=180)
        evidence['rollback']={'returncode':rollback.returncode,'stdout':rollback.stdout,'stderr':rollback.stderr}
        assert rollback.returncode==0,rollback.stderr
        assert target.resolve()==old/'bin/codex' and not installed.exists()
        assert config.read_bytes()==config_before
        assert all(hashlib.sha256((original/rel).read_bytes()).hexdigest()==sha for rel,sha in expected.items())
        evidence['checks'].extend(['real rollback restores prior package','original candidate remains unchanged'])
    evidence['passed']=True
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps({'passed':True,'checks':len(evidence['checks']),'report':str(args.report)}))


if __name__=='__main__':main()
