"""Real Chromium + HTTP + SQLite workflow with synthetic source, no providers."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import json,os,tempfile,threading
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright
from hosting.sqlite_storage import SQLiteWorkspace
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting import access
from hosting import source_first as sf
from api.index import handler
from source_first_fixture import capture,MAP,SCOPE

out=ROOT/'verification'/'source-first';out.mkdir(parents=True,exist_ok=True)
chromium=os.environ.get('SOURCE_FIRST_CHROMIUM')
with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CI_LOCAL_DEV':'1'},clear=True):
    store=SQLiteWorkspace(temp)
    with store.session() as d:
        inbox=SpreadsheetInbox(d);user=access.save_user(inbox,dict(email='browser@example.invalid',role='admin'))
        token=access.start_session(inbox,user)
        inbox.set_setting('job_registry',[dict(SCOPE,verified=True,client_key='Synthetic',client='Synthetic Client',site='SyntheticSite')])
    server=ThreadingHTTPServer(('127.0.0.1',0),handler);server.workspace_store=store
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
    calls=[]
    def source(s):calls.append(s);return capture(),{}
    try:
      with patch('hosting.source_first_capture.capture',side_effect=source),sync_playwright() as p:
        browser=p.chromium.launch(headless=True, **({'executable_path':chromium} if chromium else {}))
        context=browser.new_context(viewport={'width':1440,'height':1000});context.add_cookies([{'name':'ppg_session','value':token,'url':base}])
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base+'/source-review');page.wait_for_selector('#capture button:not([disabled])')
        page.locator('#source-choice').select_option('0')
        page.locator('#capture [name="business_date"]').fill(SCOPE['business_date'])
        page.get_by_role('button',name='Download source snapshot').click()
        page.get_by_role('heading',name='2. Confirm the source mapping').wait_for()
        for key,value in MAP['fields'].items():page.locator('select[name="'+key+'"]').select_option(value)
        page.get_by_role('button',name='Save mapping',exact=True).click();page.locator('table').wait_for()
        assert '0000000123' in page.locator('table').inner_text()
        page.get_by_role('button',name='Save report details',exact=True).click()
        page.wait_for_timeout(300)
        page.get_by_role('checkbox').check();page.get_by_role('button',name='Approve reviewed content',exact=True).click()
        page.get_by_role('button',name='Content approved',exact=True).wait_for()
        page.get_by_role('button',name='Create held daily workbook',exact=True).click()
        page.get_by_role('link',name='Download held workbook',exact=True).wait_for()
        page.screenshot(path=str(out/'browser-desktop.png'),full_page=True)
        saved_hash=page.url.split('#')[1]
        assert len(calls)==1
        page.reload();page.get_by_role('link',name='Download held workbook',exact=True).wait_for()
        assert len(calls)==1
        for width,height,label in ((1440,1000,'desktop-1440'),(1200,1000,'desktop-1200'),(390,844,'mobile')):
            page.set_viewport_size({'width':width,'height':height})
            checkbox=page.get_by_role('checkbox')
            bounds=checkbox.bounding_box()
            assert bounds['width']>=20 and bounds['height']>=20,(label,bounds)
            checkbox.click();assert checkbox.is_checked(),label
            checkbox.click();assert not checkbox.is_checked(),label
            page.screenshot(path=str(out/('browser-'+label+'.png')),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), label
        # Lose a capture response after the server actually committed it.
        def lose(route):route.fetch();route.abort('failed')
        page.route('**/api/source-review/capture',lose)
        # Reload starts a blank capture form. Choose an exact source and date
        # before testing a second request, rather than bypassing browser validity.
        page.locator('#source-choice').select_option('0')
        page.locator('#capture [name="business_date"]').fill(SCOPE['business_date'])
        page.get_by_role('button',name='Download source snapshot',exact=True).click()
        page.locator('#message').filter(has_text='Reload saved receipts').wait_for()
        assert len(calls)==2
        page.unroute('**/api/source-review/capture',lose)
        page.get_by_role('button',name='Reload saved receipts',exact=True).click();page.wait_for_timeout(400)
        assert len(calls)==2
        assert page.evaluate("Boolean(sessionStorage.getItem('sourceReviewPending'))")
        # A saved but undisplayable review shows its held receipt, never action
        # controls for a partial table. Restoring capacity recovers all content.
        with patch.object(sf,'MAX_RESPONSE_BYTES',2000):
            page.goto(base+'/source-review#'+saved_hash)
            page.reload()  # The same hash may not issue a new document/request.
            page.get_by_text('The saved review exceeds the private pilot display limit.',exact=False).wait_for()
            assert page.get_by_role('button',name='Create held daily workbook',exact=True).count()==0
            assert page.get_by_role('button',name='Approve reviewed content',exact=True).count()==0
            assert page.locator('#review table').count()==0
        page.reload();page.get_by_role('link',name='Download held workbook',exact=True).wait_for()
        assert len(calls)==2
        assert not errors,errors
        (out/'browser-result.json').write_text(json.dumps({'passed':True,'synthetic':True,'captures':len(calls),'page_errors':errors,'desktop':True,'mobile_no_horizontal_overflow':True,'reload_does_not_recapture':True,'lost_response_receipt_recovery':True,'oversized_review_shows_held_receipt_only':True},indent=2))
        browser.close()
    finally:server.shutdown();server.server_close()
print('PASS real browser source-first capture → mapping → approval → held report → restart and lost-response recovery')
