"""Actual Today HTTP handlers, synthetic saved settings, no external providers."""
from pathlib import Path
import os,sys,json,tempfile,threading,socket
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from datetime import datetime,timedelta,time
from zoneinfo import ZoneInfo
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright,expect
from hosting.sqlite_storage import SQLiteWorkspace
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting import access,sharepoint,periods,registry,source_date_contracts as contracts
from api import index
OUT=Path(os.environ['EVIDENCE_DIR']);OUT.mkdir(parents=True,exist_ok=True)
CHROMIUM=os.environ['SOURCE_FIRST_CHROMIUM'];external=[];realconnect=socket.socket.connect
catalog=[dict(key='synthetic',client='Synthetic Client',sites=[dict(site='Synthetic',location='Synthetic Plant')],jobs=[dict(job='SYNTHETIC-A'),dict(job='SYNTHETIC-B')])]
today=datetime.now(ZoneInfo('America/Chicago')).date()
columns={'Date':{'id':'00000000-0000-0000-0000-000000000001','name':'Date','dateTime':{'format':'dateOnly'}}}
items=[{'id':str(n),'fields':{'Date':datetime.combine(today if n<7 else today-timedelta(days=1),time(),ZoneInfo('America/Chicago')).isoformat(),'Date0':str(today)+'T17:00:00Z'}} for n in range(9)]
source=('Synthetic',{'id':contracts.LIST,'displayName':'Synthetic','_capture':{'site_id':contracts.SITE,'list_id':contracts.LIST,'columns':list(columns.values())}},{'Date':'Date'},items)
source_before=deepcopy(source)
def local(self,address):
 if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
  external.append(str(address));raise OSError('External network forbidden')
 return realconnect(self,address)
socket.socket.connect=local
with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CI_LOCAL_DEV':'1','VERCEL':'0'},clear=True),patch.object(sharepoint,'configured',return_value=True),patch.object(index,'sharepoint_configured',return_value=True),patch.object(sharepoint,'clients',return_value=catalog),patch.object(sharepoint,'_list_items',return_value=source),patch.object(contracts,'_site_url',return_value=contracts.SITE_URL),patch('hosting.photo_embed.certificate_ready',return_value=True),patch('hosting.photo_embed._get',return_value=(200,contracts.EXPECTED_TIMEZONE)):
 store=SQLiteWorkspace(temp)
 with store.session() as d:
  box=SpreadsheetInbox(d);user=access.save_user(box,dict(email='synthetic-today-admin@example.invalid',role='admin'));token=access.start_session(box,user)
  box.set_setting('today_jobs',['SYNTHETIC-A']);box.set_setting(registry.SNAPSHOT_KEY,catalog)
 def saved():
  with store.session() as d:
   box=SpreadsheetInbox(d);return {'watch':box.setting('today_jobs',[]),'reports':box.list()['spreadsheets'],'catalog':box.setting(registry.SNAPSHOT_KEY,None)}
 original=saved()
 class Quiet(index.handler):
  def log_message(self,*args):pass
 server=ThreadingHTTPServer(('127.0.0.1',0),Quiet);server.workspace_store=store
 threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:'+str(server.server_port)
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(headless=True,executable_path=CHROMIUM)
   for width in (1200,390):
    context=browser.new_context(viewport={'width':width,'height':900});context.add_cookies([dict(name='ppg_session',value=token,url=base)])
    context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base+'/') else (external.append(r.request.url),r.abort())[1])
    page=context.new_page();errors=[];requests=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:requests.append((r.url,r.post_data_json)) if r.method=='POST' else None)
    page.goto(base+'/app#c=synthetic',wait_until='networkidle')
    expect(page.locator('.clienthead')).to_contain_text('2 jobs on SharePoint')
    assert page.get_by_role('button',name='Watch these jobs on Today',exact=True).count()==0
    page.goto(base+'/app#today',wait_until='networkidle')
    expect(page.locator('.today-scope')).to_contain_text('does not establish complete client or source coverage')
    expect(page.locator('.pdays')).to_contain_text('SYNTHETIC-A')
    expect(page.locator('.pdays')).to_contain_text('7 scans')
    expect(page.get_by_role('button',name='Build captured report',exact=True)).to_be_enabled()
    for label in ('Watch job','Stop watching'):
     assert page.get_by_role('button',name=label,exact=True).count()==0
    assert page.get_by_role('textbox',name='Job number',exact=True).count()==0
    # Repeat date changes and reload; these must retain saved settings and never build.
    before=page.locator('.pnav b').inner_text()
    page.get_by_role('button',name='Previous day',exact=True).click();expect(page.locator('.pnav b')).not_to_have_text(before)
    page.get_by_role('button',name='Next day',exact=True).click();expect(page.locator('.pnav b')).to_have_text(before)
    page.reload(wait_until='networkidle');expect(page.locator('.pdays')).to_contain_text('7 scans')
    page.goto(base+'/app#c=synthetic',wait_until='networkidle');expect(page.locator('.clienthead')).to_contain_text('Synthetic Client')
    page.go_back(wait_until='networkidle');expect(page.locator('.today-scope')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(OUT/('today-'+str(width)+'.png')),full_page=True)
    # Missing source dates and genuine ambiguity remain explicit, not zero activity.
    with patch.object(contracts,'capture_contract',return_value=None):
     page.reload(wait_until='networkidle')
     expect(page.get_by_text('Source check incomplete',exact=True)).to_be_visible()
     page.get_by_text('Source check incomplete',exact=True).click()
     expect(page.locator('.source-read-error')).to_contain_text('business dates could not be confirmed')
     assert 'Unknown' in page.locator('.numbers').inner_text()
    with patch.object(sharepoint,'_list_items',side_effect=sharepoint.SharePointError('Ambiguous SharePoint job: select the verified site_id and list_id. No newest-list fallback is allowed.')):
     page.reload(wait_until='networkidle')
     expect(page.get_by_text('Source check incomplete',exact=True)).to_be_visible()
     page.get_by_text('Source check incomplete',exact=True).click()
     expect(page.locator('.source-read-error')).to_contain_text('Ambiguous SharePoint job')
     assert 'Unknown' in page.locator('.numbers').inner_text()
    page.reload(wait_until='networkidle');expect(page.locator('.pdays')).to_contain_text('7 scans')
    assert saved()==original and source==source_before
    assert not errors,errors
    assert all(not any(k in (data or {}) for k in ('add','remove')) for url,data in requests if url.endswith('/api/today'))
    assert not any(any(x in url for x in ('/api/build','/api/autopilot','/api/settings')) for url,data in requests)
    context.close();print(json.dumps(dict(passed=True,width=width,watch_controls_removed=True,client_header_retained=True,navigation_reload_back=True,settings_and_reports_unchanged=True,no_build_or_timer=True,actual_date_contract_handler=True)),flush=True)
   browser.close()
 finally:server.shutdown();server.server_close()
assert not external,external
print('PASS Today actual-handler desktop/mobile removal; no external requests')
