"""Exercise the actual filesystem config stanza without external runtime setup."""
import ast
import importlib.util
import os
from pathlib import Path
import re
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provenance_protocol', ROOT / 'converter/protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)
SOURCES = [ROOT / 'converter/claude_to_codex.py']
SHARED = ROOT.parent / '.cue/converter/claude_to_codex.py'
if SHARED.exists(): SOURCES.append(SHARED)


def filesystem_stanza(path, rows, directories=()):
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Converter')
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'config')
    start = next(i for i,n in enumerate(fn.body) if isinstance(n, ast.Assign) and any(isinstance(t,ast.Name) and t.id == 'filesystem' for t in n.targets))
    end = next(i for i,n in enumerate(fn.body[start:],start) if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute) and n.value.func.attr=='finding' and n.value.args and isinstance(n.value.args[0],ast.Constant) and n.value.args[0].value=='permissions')
    findings=[]
    owner=types.SimpleNamespace(output=Path('/target'),permission_rules=rows,translate=lambda x:x,
                                finding=lambda *args,**kwargs:findings.append([args,kwargs]))
    permissions={action:[r['rule'] for r in rows if r['action']==action] for action in ('ask','deny','allow')}
    permissions['additionalDirectories']=list(directories)
    env={'self':owner,'permissions':permissions,'Path':Path,'os':os,'re':re,'config':[],
         'permission_path_pattern':protocol.permission_path_pattern,'toml_value':lambda v:repr(v)}
    exec(compile(ast.Module(body=fn.body[start:end],type_ignores=[]),str(path),'exec'),env)
    return env['filesystem'],env['config'],getattr(owner,'filesystem_contributions',[]),findings


class FilesystemProvenance(unittest.TestCase):
    def rows(self,*pairs):
        return [{'action':action,'rule':rule,'source_root':'/source-settings'} for action,rule in pairs]

    def test_same_path_contributions_survive_and_deny_wins(self):
        rows=self.rows(('ask','Read(/same)'),('deny','Read(/same)'),('ask','Edit(/same)'),('deny','Edit(/same)'))
        for source in SOURCES:
            fs,lines,parts,findings=filesystem_stanza(source,rows)
            self.assertEqual(len(parts),4)
            self.assertEqual([r['source_action'] for r in parts].count('ask'),2)
            self.assertEqual(set(fs.values()),{'deny'})
            self.assertEqual({f[1]['emitted_path_access'] for f in findings},{'deny'})
            self.assertEqual({p['reason'] for p in parts},{'ask-fallback','source-deny'})

    def test_resolution_retains_existing_per_surface_semantics(self):
        rows=self.rows(('deny','Read(/private/**)'),('ask','Edit(./output/**)'))
        for source in SOURCES:
            fs,_,parts,_=filesystem_stanza(source,rows)
            if source==SHARED:
                self.assertEqual(fs,{'/private':'deny','/target/output':'read'})
                self.assertIsNone(parts[0]['source_root'])
            else:
                self.assertEqual(fs,{'/source-settings/private/**':'deny','/target/output/**':'read'})
                self.assertEqual(parts[0]['source_root'],'/source-settings')

    def test_effective_bytes_match_previous_set_aggregation(self):
        rows=self.rows(('ask','Read(/a)'),('deny','Edit(/a)'),('ask','Edit(/b)'),('deny','Read(/a)'),('allow','Read(/c)'))
        for source in SOURCES:
            fs,lines,parts,_=filesystem_stanza(source,rows)
            prefix='' if source==SHARED else '/source-settings'
            expected={prefix+'/a':'deny',prefix+'/b':'read'}
            self.assertEqual(fs,expected)
            self.assertEqual(lines,[repr(k)+' = '+repr(v) for k,v in sorted(expected.items())])
            self.assertEqual(len(parts),4)

    def test_findings_deterministic_and_no_rule_dropped(self):
        rows=self.rows(('ask','Read(/a)'),('deny','Read(/a)'),('deny','Edit(/b)'))
        for source in SOURCES:
            first=filesystem_stanza(source,rows)
            self.assertEqual(first,filesystem_stanza(source,rows))
            self.assertEqual(len(first[3]),3)
            self.assertEqual(len(first[2]),3)


if __name__=='__main__': unittest.main()
