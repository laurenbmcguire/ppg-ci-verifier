"""Actual local HTTP/browser recovery controls; synthetic bytes and no providers."""
from pathlib import Path
import json,os,socket,sys,tempfile,threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright,expect
from hosting.sqlite_storage import SQLiteWorkspace
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting import access
from api.index import handler
from eos_fixture import build
CHROMIUM=os.environ['SOURCE_FIRST_CHROMIUM']
OUT=Path(os.environ['EVIDENCE_DIR']);OUT.mkdir(parents=True,exist_ok=True)
external=[];realconnect=socket.socket.connect
def local(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
        external.append(str(address));raise OSError('External network forbidden')
    return realconnect(self,address)
socket.socket.connect=local
with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CI_LOCAL_DEV':'1','VERCEL':'0'},clear=True):
    store=SQLiteWorkspace(temp)
    def seed(label):
        raw=build(overrides={('1st Shift','G17'):'34',('1st Shift','G18'):'34',('1st Shift','L18'):'Synthetic recovery '+label})
        with store.session() as d:
            box=SpreadsheetInbox(d);item=box.ingest('SYNTHETIC-RECOVERY-'+label+'.xlsx',raw)
            key=item['file_hash']
            box._update(key,lambda r:r.update(fixed_from=dict(file_hash=key,revision=1,changes=[dict(sheet='Daily Inspection Summary',cell='C4',before='',after='2026-11-06')]),
                fixes_applied=[dict(fixed_file_hash=key,revision=2)],revision=2,root_hash=key,
                source_reconciliation=dict(status='incomplete',verified=False,stale=True,not_verified_because=['Synthetic source evidence missing.']),
                finding_decisions={'held':dict(action='defer',sheet='1st Shift',cell='G18',reason='Needs exact source')}))
        return key,raw
    def snapshot(key):
        with store.session() as d:
            box=SpreadsheetInbox(d)
            with box.connect() as db:rows=[r[0] for r in db.execute('SELECT result_json FROM spreadsheet_checks WHERE file_hash=? ORDER BY seq',(key,))]
            return box.get(key),rows,box.download(key)[1],box.working_copy(key)
    with store.session() as d:
        box=SpreadsheetInbox(d);user=access.save_user(box,dict(email='synthetic-admin@example.invalid',role='admin'));token=access.start_session(box,user)
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
          key,raw=seed(str(width));before=snapshot(key)
          page.goto(base+'/app#r='+key,wait_until='networkidle')
          button=page.get_by_role('button',name='Review history recovery',exact=True);button.wait_for()
          assert page.locator('.version-readonly').count()==1
          page.once('dialog',lambda d:d.dismiss());button.click()
          expect(button).to_be_enabled();assert snapshot(key)==before
          # A safe refusal retains the read-only state and all history.
          def refuse(route):
            data=route.request.post_data_json
            if data.get('apply'):route.fulfill(status=409,content_type='application/json',body=json.dumps({'error':'Synthetic safe refusal'}))
            else:route.continue_()
          page.route('**/api/spreadsheets/history-recovery',refuse)
          page.once('dialog',lambda d:d.accept());button.click()
          page.locator('.fd-history-recovery').filter(has_text='Synthetic safe refusal').wait_for()
          assert snapshot(key)==before;assert page.locator('.version-readonly').count()==1
          page.unroute('**/api/spreadsheets/history-recovery',refuse)
          page.once('dialog',lambda d:d.accept());button.click()
          page.locator('.version-readonly').wait_for(state='detached')
          if width<900:page.locator('.fd-mobile-panel').click()
          page.locator('#fd-open-review:enabled').wait_for()
          after=snapshot(key)
          assert after[1][:-1]==before[1] and after[2]==raw and after[3]['status']=='current'
          assert after[0]['source_reconciliation']==before[0]['source_reconciliation']
          assert after[0]['finding_decisions']==before[0]['finding_decisions']
          page.locator('#fd-open-review').click()
          review=page.get_by_role('dialog',name='Needs review',exact=True);review.wait_for()
          assert review.get_by_role('button',name='Fix manually',exact=True).count()>0
          review.get_by_role('button',name='Close review',exact=True).click()
          page.get_by_role('button',name='History',exact=True).click()
          expect(page.locator('.fd-utility')).to_contain_text('Audited history recovery')
          page.get_by_text('Earlier self-link claims, retained as unverified history',exact=True).click()
          expect(page.locator('.fd-utility')).to_contain_text('does not prove they were applied')
          assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
          page.screenshot(path=str(OUT/('recovery-history-'+str(width)+'.png')),full_page=True)
          # Change stored evidence after the exact preview, before confirmation.
          stale,_=seed('stale-'+str(width));page.goto(base+'/app#r='+stale,wait_until='networkidle')
          def race(route):
            data=route.request.post_data_json
            if not data.get('apply'):
                response=route.fetch()
                with store.session() as d:SpreadsheetInbox(d)._update(stale,lambda r:r.update(note='Concurrent synthetic evidence'))
                route.fulfill(response=response)
            else:route.continue_()
          page.route('**/api/spreadsheets/history-recovery',race)
          page.once('dialog',lambda d:d.accept());page.get_by_role('button',name='Review history recovery',exact=True).click()
          page.locator('.fd-history-recovery').filter(has_text='The report changed').wait_for()
          assert snapshot(stale)[3]['status']=='unavailable'
          assert page.locator('.version-readonly').count()==1
          page.unroute('**/api/spreadsheets/history-recovery',race)
          assert not errors,errors
          context.close()
          print(json.dumps(dict(passed=True,width=width,cancel_no_write=True,failure_readonly=True,exact_apply_restores_controls=True,stale_preview_refused=True,history_and_source_holds_preserved=True)),flush=True)
        browser.close()
    finally:server.shutdown();server.server_close()
assert not external,external
print('PASS actual-handler recovery controls; no external requests')
