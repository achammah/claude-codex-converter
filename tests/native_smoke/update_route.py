"""Exercise real native updater routing and corrupt-manifest rejection."""
import argparse, hashlib, json, os, shlex, shutil, subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--package',type=Path,required=True);p.add_argument('--work',type=Path,required=True);a=p.parse_args()
root=a.work.resolve();root.mkdir(parents=True,exist_ok=False)
package=root/'package';shutil.copytree(a.package,package)
home=root/'home';home.mkdir();project=root/'project';project.mkdir()
fake=root/'fake';fake.mkdir();marker=root/'brew-called'
(fake/'brew').write_text('#!/bin/sh\nprintf called > '+shlex.quote(str(marker))+'\nexit 99\n');(fake/'brew').chmod(0o755)
env={k:os.environ[k] for k in ('PATH','HOME','TMPDIR','LANG') if k in os.environ};env['CODEX_HOME']=str(home);env['PATH']=str(fake)+os.pathsep+env.get('PATH','')
manifest=package/'codex-package.json';metadata=json.loads(manifest.read_text());metadata['cueUpdate'].pop('feedUrl',None);manifest.write_text(json.dumps(metadata))
binary=package/'bin/codex'
def run():
 r=subprocess.run([str(binary),'update'],cwd=project,env=env,capture_output=True,text=True,timeout=30)
 return {'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
normal=run();manifest=package/'codex-package.json';manifest.write_text('{"cueUpdate":')
corrupt=run()
try: state=json.loads(normal['stdout'].splitlines()[0])['state']
except (ValueError,KeyError,IndexError):state=None
passed=normal['exit']==0 and state=='current' and corrupt['exit']!=0 and 'ownership' in corrupt['stderr'].lower() and not marker.exists()
report={'passed':passed,'normal':normal,'corrupt':corrupt,'brew_invoked':marker.exists(),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'scope':'actual CLI and bundled updater in disposable copied package'}
(root/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));raise SystemExit(0 if passed else 1)
