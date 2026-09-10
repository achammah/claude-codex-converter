import sys,json
from pathlib import Path
pending=None
for line in sys.stdin:
 m=json.loads(line); method=m.get('method'); ident=m.get('id')
 if method=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
 elif method=='tools/list': result={'tools':[{'name':'ask','description':'Synthetic native question','inputSchema':{'type':'object','properties':{}}}]}
 elif method=='tools/call':
  pending=ident
  print(json.dumps({'jsonrpc':'2.0','id':'question','method':'elicitation/create','params':{'message':'Choose a destination','requestedSchema':{'type':'object','properties':{'q0':{'type':'string','title':'Destination','description':'Where should this run?','enum':['Local','Cloud']},'q0_other':{'type':'string','title':'Other'}}},'_meta':{'io.cue/questions':{'version':1,'questions':[{'id':'q0','customField':'q0_other','multiSelect':False}]}}}}),flush=True);continue
 elif ident=='question':
  Path(sys.argv[1]).write_text(json.dumps(m,indent=2))
  print(json.dumps({'jsonrpc':'2.0','id':pending,'result':{'content':[{'type':'text','text':json.dumps(m.get('result'))}]}}),flush=True);continue
 elif method=='notifications/initialized': continue
 elif ident is None: continue
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result}),flush=True)
