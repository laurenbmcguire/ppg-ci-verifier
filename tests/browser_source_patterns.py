"""Synthetic actual-handler source-pattern classification with no providers."""
from pathlib import Path
import os,sys,json,tempfile,threading,socket
from http.server import ThreadingHTTPServer
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright,expect
from hosting.sqlite_storage import SQLiteWorkspace
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting import access,public_api
from api.index import handler
from tests.test_source_pattern_classification import persisted_fixture_inputs
OUT=Path(os.environ['EVIDENCE_DIR']);OUT.mkdir(parents=True,exist_ok=True)
CHROMIUM=os.environ['SOURCE_FIRST_CHROMIUM']; external=[];realconnect=socket.socket.connect
def local(self,address):
 if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
  external.append(str(address));raise OSError('External network forbidden')
 return realconnect(self,address)
socket.socket.connect=local
raw,snapshot,assigned=persisted_fixture_inputs()
with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CI_LOCAL_DEV':'1','VERCEL':'0'},clear=True),patch.object(SpreadsheetInbox,'assignment_for',return_value=assigned):
 store=SQLiteWorkspace(temp)
 with store.session() as d:
  box=SpreadsheetInbox(d);user=access.save_user(box,dict(email='synthetic-pattern-admin@example.invalid',role='admin'));token=access.start_session(box,user)
  item=box.ingest('Synthetic source pattern 2214.xlsx',raw);key=item['file_hash']
  box.source_reconcile(key,snapshot,'2026-11-06',expected_check_hash=box.get(key)['check_hash'],expected_assignment=assigned)
 class Quiet(handler):
  def log_message(self,*args):pass
 server=ThreadingHTTPServer(('127.0.0.1',0),Quiet);server.workspace_store=store
 threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:'+str(server.server_port)
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(headless=True,executable_path=CHROMIUM)
   for width in (1200,390):
    context=browser.new_context(viewport={'width':width,'height':900});context.add_cookies([dict(name='ppg_session',value=token,url=base)])
    context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base+'/') else (external.append(r.request.url),r.abort())[1])
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(base+'/app#r='+key,wait_until='networkidle')
    if width<900:page.locator('.fd-mobile-panel').click()
    page.locator('#fd-open-review:enabled').wait_for()
    page.locator('#fd-open-review').click()
    review=page.get_by_role('dialog',name='Needs review',exact=True);review.wait_for()
    assert '00356870-V-LH' not in review.inner_text(),review.inner_text()[:2500]
    assert review.get_by_role('button',name='AI fix',exact=True).count()>0
    assert review.get_by_role('button',name='Fix manually',exact=True).count()>0
    assert review.get_by_role('button',name='Ignore',exact=True).count()>0
    review.get_by_role('button',name='Close review',exact=True).click()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(OUT/('source-pattern-'+str(width)+'.png')),full_page=True)
    with store.session() as d:
     box=SpreadsheetInbox(d);item=box.get(key);rv=box.review_summary(item)
     hint=next(x for x in rv['issues'] if x['code']=='part_format_differs')
     assert hint['severity']=='info' and hint['source_support']['kind']=='source_confirmed_pattern'
     assert box.original_bytes(key)[1]==raw
     assert any(h['code']=='source_evidence_incomplete' and h['hard'] for h in box.holds(item))
     assert not item.get('approval') and not item.get('sent') and not item.get('fixed_from')
     assert not any(i['code']=='part_format_differs' for i in box.ai_fix_inputs(key)[2])
    assert not errors,errors
    context.close()
    print(json.dumps(dict(passed=True,width=width,source_pattern_absent_from_open_review=True,normal_controls_preserved=True,nok_hold_retained=True,original_bytes_preserved=True,no_approval_or_send=True)),flush=True)
   browser.close()
 finally:server.shutdown();server.server_close()
assert not external,external
print('PASS source-supported pattern actual-handler desktop/mobile; no external requests')
