import os,sys,json,time,threading,pty,fcntl,termios,struct,select,subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pyte
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--package',type=Path,required=True)
parser.add_argument('--work',type=Path,required=True)
args=parser.parse_args()
root=args.work.resolve();root.mkdir(parents=True,exist_ok=False)
binary=args.package.resolve()/'bin/codex'
fixture_server=Path(__file__).with_name('question_server.py')
requests=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body); (root/'live-request.json').write_text(json.dumps(body,indent=2))
  if len(requests)==1:
   item={'type':'custom_tool_call','call_id':'ask-call','name':'exec','input':'const t = ALL_TOOLS.find(t => t.name.endsWith("fixture__ask")); text(await tools[t.name]({}));'}
  else:item={'type':'message','id':'done','role':'assistant','content':[{'type':'output_text','text':'SMOKE_FINISHED'}]}
  ev=[{'type':'response.created','response':{'id':'r'+str(len(requests))}},{'type':'response.output_item.done','item':item},{'type':'response.completed','response':{'id':'r'+str(len(requests)),'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0}}}]
  data=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in ev).encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
home=root/'home';home.mkdir(exist_ok=True);project=root/'project';project.mkdir(exist_ok=True)
(home/'config.toml').write_text('check_for_update_on_startup=false\nmodel="gpt-6-astra"\nmodel_provider="fixture"\napproval_policy="on-request"\nsandbox_mode="workspace-write"\n[features]\ncode_mode=false\n[model_providers.fixture]\nname="Local fixture"\nbase_url="http://127.0.0.1:'+str(server.server_port)+'/v1"\nwire_api="responses"\nrequires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n[mcp_servers.fixture]\ncommand='+json.dumps(sys.executable)+'\nargs='+json.dumps([str(fixture_server),str(root/'answer.json')])+'\n[projects.'+json.dumps(str(project))+']\ntrust_level="trusted"\n')
master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',32,100,0,0))
env={k:os.environ[k] for k in ['PATH','HOME','TMPDIR','LANG'] if k in os.environ};env.update(CODEX_HOME=str(home),TERM='xterm-256color')
proc=subprocess.Popen([str(binary),'--no-alt-screen','-C',str(project),'Run the synthetic question fixture.'],stdin=slave,stdout=slave,stderr=slave,env=env,cwd=project,start_new_session=True);os.close(slave)
screen=pyte.Screen(100,32);stream=pyte.Stream(screen);raw=bytearray();sent=False;captured=False;deadline=time.monotonic()+60
try:
 while time.monotonic()<deadline and proc.poll() is None:
  ready,_,_=select.select([master],[],[],.1)
  if not ready:continue
  try:data=os.read(master,65536)
  except OSError:break
  raw.extend(data)
  for query,reply in [(b'\x1b[6n',b'\x1b[1;1R'),(b'\x1b[c',b'\x1b[?1;2c'),(b'\x1b[>c',b'\x1b[>0;0;0c'),(b'\x1b[?u',b'\x1b[?0u')]:
   if query in data:os.write(master,reply)
  stream.feed(data.decode(errors='replace')); visible='\n'.join(screen.display)
  (root/'latest-screen.txt').write_text(visible)
  if 'Allow the fixture MCP server to run tool' in visible:
   os.write(master,b'\r');continue
  if 'Other: type your own answer' in visible and 'Local' in visible and 'Cloud' in visible and not sent:
   (root/'choices-screen.txt').write_text(visible);os.write(master,b'On premises');sent=True
  elif sent and 'On premises' in visible and not captured:
   (root/'custom-screen.txt').write_text(visible);captured=True;os.write(master,b'\r')
  if (root/'answer.json').exists() and 'SMOKE_FINISHED' in visible:break
finally:
 (root/'terminal.raw').write_bytes(raw);(root/'requests.json').write_text(json.dumps(requests,indent=2))
 if proc.poll() is None:os.write(master,b'\x03');time.sleep(.1);proc.terminate();proc.wait(timeout=10)
 os.close(master);server.shutdown()
answer=json.loads((root/'answer.json').read_text()) if (root/'answer.json').exists() else None
passed=bool(sent and captured and answer and answer.get('result',{}).get('content')=={'q0_other':'On premises'} and answer.get('result',{}).get('_meta',{}).get('io.cue/questions')=={'version':1})
report={'passed':passed,'choices_and_custom_same_screen':sent,'typed_custom_visible':captured,'answer':answer,'requests':len(requests),'binary_sha256':__import__('hashlib').sha256(binary.read_bytes()).hexdigest()}
(root/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));sys.exit(0 if passed else 1)
