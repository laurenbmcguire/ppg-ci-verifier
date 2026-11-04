"""Actual loopback HTTP acceptance run using only packaged synthetic data.
Starts the real CLI in a child process, exercises APIs, restarts it, and removes
its temporary workspace. No external I/O, accounts, credentials or real reports.
"""
from pathlib import Path
import base64, hashlib, json, os, subprocess, sys, tempfile
from io import BytesIO
from openpyxl import load_workbook
import urllib.request, urllib.error
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parents[1]


def run():
    result={'started_at':datetime.now(timezone.utc).isoformat(),'synthetic_only':True,'browser_tested':False,'release_status':'held','checks':[]}
    with tempfile.TemporaryDirectory(prefix='verifier-smoke-') as data:
        proc=None; base=None
        def start():
            nonlocal proc, base
            proc=subprocess.Popen([sys.executable,'hosting/sqlite_server.py','--demo','--port','0','--data',data],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
            line=proc.stdout.readline().strip()
            if not line.startswith('LOCAL SQLITE COPY / PROTOTYPE: http://127.0.0.1:'):
                raise AssertionError('CLI did not start: '+line)
            base=line.split(': ',1)[1]
        def stop():
            nonlocal proc
            if proc:
                proc.terminate();proc.communicate(timeout=15);proc=None
        def request(path,payload=None,expected=200,binary=False):
            headers={'Origin':base,'X-Review-Token':'same-origin','Content-Type':'application/json'}
            req=urllib.request.Request(base+path, data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
            try:
                with urllib.request.urlopen(req,timeout=30) as response:status=response.status;raw=response.read()
            except urllib.error.HTTPError as error:status=error.code;raw=error.read()
            assert status==expected,(path,status,raw[:200])
            return raw if binary else json.loads(raw)
        try:
            start()
            assert request('/api/health')['development_demo'] is True
            result['checks'].append('Real CLI loopback server and demo flag')
            folder=ROOT/'samples/development_examples';index=json.loads((folder/'index.json').read_text());results=[]
            for example in index['examples']:
                raw=(folder/example['artifact']['path']).read_bytes()
                assert hashlib.sha256(raw).hexdigest()==example['artifact']['sha256']
                item=request('/api/spreadsheets/import',dict(filename=example['artifact']['path'],content_base64=base64.b64encode(raw).decode()))['item']
                if example['source']:
                    source=json.loads((folder/example['source']['path']).read_text())
                    item=request('/api/spreadsheets/reconcile',dict(file_hash=item['file_hash'],source=source))['item']
                for key,value in example['expected'].items():
                    if value is not None:assert item.get(key)==value,(example['id'],key,item.get(key),value)
                results.append(dict(id=example['id'],file_hash=item['file_hash'],record_count=item.get('record_count'),numeric_values_compared=item.get('numeric_values_compared'),source_difference_count=item.get('source_difference_count'),release_status=item['release_status']))
            result['samples']=results;result['checks'].append('All four packaged examples match expected findings and hashes over actual HTTP')
            mismatch=next(item for item in results if item['id']=='synthetic_ppg_source_mismatch')
            review=request('/api/spreadsheets/demo/review?id='+mismatch['file_hash'])
            assert review['eligible'] and len(review['proposals'])==1
            proposal=review['proposals'][0];assert proposal['cell']=='K17' and proposal['before']==24 and proposal['after']==23
            sbinding={key:review[key] for key in ('file_hash','check_hash','source_sha256')};choice=dict(sbinding,proposal_ids=[proposal['id']])
            request('/api/spreadsheets/demo/decisions',dict(choice,decision='deny'))
            request('/api/spreadsheets/demo/apply',choice,409)
            request('/api/spreadsheets/demo/decisions',dict(choice,decision='approve'))
            fixed=request('/api/spreadsheets/demo/apply',choice)
            assert fixed['item']['source_difference_count']==0 and fixed['item']['release_status']=='held'
            assert not fixed['review']['decisions'] and fixed['review']['current_version']
            fixed_raw=request(fixed['download_url'],binary=True)
            assert hashlib.sha256(fixed_raw).hexdigest()==fixed['item']['file_hash']
            before=load_workbook(folder/'synthetic_ppg_source_mismatch.xlsx',data_only=False)
            after=load_workbook(BytesIO(fixed_raw),data_only=False)
            changes=[]
            for sheet in before:
                for row in sheet:
                    for cell in row:
                        if cell.value!=after[sheet.title][cell.coordinate].value:changes.append((sheet.title,cell.coordinate,cell.value,after[sheet.title][cell.coordinate].value))
            assert changes==[('1st Shift','K17',24,23)],changes
            assert after['1st Shift']['J16'].value is None and after['1st Shift']['J17'].value is None
            old_review=request('/api/spreadsheets/demo/review?id='+mismatch['file_hash'])
            assert old_review['current_version'] is False and old_review['superseded_by']==fixed['item']['file_hash']
            request('/api/spreadsheets/demo/decisions',dict(choice,decision='approve'),409)
            request('/api/spreadsheets/demo/apply',choice,409)
            original_bytes=(folder/'synthetic_ppg_source_mismatch.xlsx').read_bytes()
            repeated=request('/api/spreadsheets/import',dict(filename='synthetic_ppg_source_mismatch.xlsx',content_base64=base64.b64encode(original_bytes).decode()))['item']
            assert repeated['idempotent'] and repeated['file_hash']==mismatch['file_hash'] and repeated['source_difference_count']==1
            result['spreadsheet_draft']={'original_hash':mismatch['file_hash'],'draft_hash':fixed['item']['file_hash'],'changed_cells':changes,'source_differences_before':1,'source_differences_after':0,'unknown_rejects_preserved':True,'release_status':'held'}
            result['checks']+=['Synthetic XLSX per-item reject/accept and explicit selection produce corrected downloadable workbook','Only K17 changes 24 to 23; other cell values/formulas and unknown rejects preserved','Corrected XLSX recheck reports zero numeric source differences but remains held','Superseded original refuses repeated decisions/apply and remains byte-identical']
            manifest=json.loads((ROOT/'samples/workflow-report.json').read_text())
            state=request('/api/workflow/import',manifest)['item'];original=state['manifest_hash'];ids=[p['id'] for p in state['proposals']]
            assert len(ids)==3
            binding={key:state[key] for key in ('manifest_hash','report_hash','source_hash')}
            request('/api/workflow/demo/decision',dict(binding,proposal_id=ids[0],decision='deny'))
            request('/api/workflow/demo/apply',dict(binding,proposal_ids=[ids[0]],new_version='must-not-exist'),409)
            state=request('/api/workflow/demo/decisions',dict(binding,proposal_ids=ids[:2],decision='approve'))['item']
            assert len(state['demo_decisions'])==2 and all(d['decision']=='approve' for d in state['demo_decisions'])
            request('/api/workflow/demo/decisions',dict(binding,proposal_ids=[ids[0],'invalid'],decision='deny'),409)
            state=request('/api/workflow/version?hash='+original);assert all(d['decision']=='approve' for d in state['demo_decisions'])
            request('/api/workflow/demo/decisions',dict(binding,proposal_ids=[ids[2]],decision='hold'))
            request('/api/workflow/demo/apply',dict(binding,proposal_ids=ids,new_version='must-not-exist'),409)
            request('/api/workflow/approve',dict(binding,reviewed=True),409)
            draft=request('/api/workflow/demo/apply',dict(binding,proposal_ids=ids[:2],new_version='smoke-selected-draft'))['item']
            assert draft['proposal_count']==1 and draft['release_status']=='held' and not draft['approval_effective']
            assert draft['demo_decisions']==[]
            request('/api/workflow/demo/decision',dict(binding,proposal_id=ids[0],decision='approve'),409)
            result['workflow']={'initial_proposals':3,'selected_accepted_applied':2,'remaining_proposals':1,'draft_manifest_hash':draft['manifest_hash'],'original_manifest_hash':original,'approval_effective':False}
            result['checks']+=['Individual rejection blocks apply','Atomic explicit batch accept and invalid batch rollback','Hold/unreviewed decisions block mixed apply','Selected accepted JSON draft only; original history stale and locked','Real approval remains unavailable']
            # Full process restart, not merely a new model instance.
            stop();start()
            reopened=request('/api/workflow/version?hash='+draft['manifest_hash'])
            assert reopened['manifest_hash']==draft['manifest_hash'] and reopened['proposal_count']==1
            old=request('/api/workflow/version?hash='+original);assert len(old['demo_decisions'])==3 and not old['current_version']
            for item in results:assert request('/api/spreadsheet?id='+item['file_hash'])['file_hash']==item['file_hash']
            persisted=request('/api/spreadsheets/demo/review?id='+fixed['item']['file_hash']);assert persisted['download_url']==fixed['download_url']
            assert request(fixed['download_url'],binary=True)==fixed_raw
            result['checks'].append('All uploads, comparison revisions, decisions, JSON/Excel drafts, downloads and audit survive actual CLI process restart')
            result['status']='passed'
        finally:stop()
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    return result

if __name__=='__main__':
    result=run();destination=ROOT/'verification/development-http-smoke.json';destination.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
