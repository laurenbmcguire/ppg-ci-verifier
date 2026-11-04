"""Read/write only the explicitly isolated local browser-test session."""
import json, urllib.request, urllib.error, sys
from pathlib import Path
BASE=sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:8765'
passed=[]
def req(path, data=None, token=None, origin=None):
 headers={}
 if token: headers['X-Review-Token']=token
 if origin: headers['Origin']=origin
 if data is not None: headers['Content-Type']='application/json'
 r=urllib.request.Request(BASE+path,data=None if data is None else json.dumps(data).encode(),headers=headers)
 try:
  with urllib.request.urlopen(r) as x:return x.status,x.read(),dict(x.headers)
 except urllib.error.HTTPError as e:return e.code,e.read(),dict(e.headers)
def check(name,value):
 assert value,name
 passed.append('PASS '+name)
_,b,_=req('/api/state');s=json.loads(b);token=s['token'];revision=s['state']['revision']
check('state endpoint returns working state',bool(s['state']['hash']))
check('POST without session token is rejected',req('/api/approve',{'revision':revision})[0]==403)
check('cross-origin POST is rejected',req('/api/request',{'revision':revision},token,'https://example.com')[0]==403)
check('filesystem traversal not served',req('/../README.txt')[0]==404)
code,b,h=req('/api/download',{'revision':revision},token)
check('download has unapproved filename and watermark',code==200 and 'UNAPPROVED' in h['Content-Disposition'] and b'UNAPPROVED' in b)
Path('outputs/http-private-UNAPPROVED.csv').write_bytes(b)
check('stale edit returns conflict',req('/api/edit',{'revision':revision-1,'row':0,'col':2,'value':'Stale'},token)[0]==409)
check('unapproved generation returns conflict',req('/api/generate',{'revision':revision},token)[0]==409)
for _ in range(2):req('/api/request',{'revision':revision},token)
for _ in range(2):req('/api/timeout',{'revision':revision},token)
_,b,_=req('/api/state');state=json.loads(b)['state']
check('duplicate requests are deduplicated',sum(a['event']=='review_request_simulated' for a in state['audit'])==1)
check('duplicate reminders deduplicated and unanswered remains unapproved',sum(a['event']=='reminder_simulated' for a in state['audit'])==1 and state['approval'] is None)
check('private download did not generate report',state['simulated_report'] is None)
Path('outputs/http-results.txt').write_text('\n'.join(passed)+'\n10 passed; 0 failed.\n')
print('\n'.join(passed))
