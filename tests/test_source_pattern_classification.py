"""Exact source-confirmed pattern hints are information, never corrections."""
import copy
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from hosting.adaptive_reports import inspect_adaptive
from hosting import reconcile
from hosting.branded_reports import fingerprint
from hosting.review_summary import summarize, check_run
from hosting.spreadsheets import CHECKER_VERSION
from hosting.source_patterns import capture

CONTEXT = dict(valid=True, site_id='site-1', list_id='list-1')


def fixture(value='00356870-V-LH'):
    rows = [['Identifier', 'Part number', 'Quantity Inspected', 'OK', 'NOK']]
    rows += [[f'LOT-{i:02}', value if i == 14 else '12345678 AA', 1, 1, 0] for i in range(1, 28)]
    item = inspect_adaptive([('Inspection Detail', rows)], {'limitations': []})
    item.update(file_hash='a'*64, checker_version=CHECKER_VERSION)
    for group in (item['records'], item['_detail_records']):
        for r in group:
            r['source'] = dict(site_id='site-1', list_id='list-1', item=str(r['row']))
    snap = dict(site_id='site-1', list_id='list-1', captured_at=datetime.now(timezone.utc).isoformat(),
                columns={k: dict(name=k, displayName=v) for k,v in [('Part','Part number'),('Identifier','Identifier'),('Qty','Quantity inspected'),('NOK','Qty NOK'),('OK','Quantity OK')]},
                quantity_candidates=dict(inspected=['Qty'], rejected=['NOK'], certified=['OK']),
                coverage=dict(complete=True, pages=1, items=27), items=[])
    for r in item['records']:
        snap['items'].append(dict(item=r['source']['item'], day='2026-11-10', version='1.0', raw=dict(Part=r['exact_labels']['part'], Identifier=r['exact_labels']['identifier'], Qty=1, NOK=0, OK=1)))
    def compare():
        item['source_reconciliation'] = reconcile.compare('2026-11-10', item['_detail_records'], [], snap,
            report=dict(file_hash=item['file_hash'], checker_version=item['checker_version']))
    compare()
    return item, snap, compare


def pattern(item, context=CONTEXT):
    return next(i for i in summarize(item, source_context=context)['issues'] if i['code']=='part_format_differs')


class SourcePatterns(unittest.TestCase):
    def test_exact_requested_shape_informational_without_mutation(self):
        item, _, _ = fixture(); before=copy.deepcopy(item)
        out=pattern(item)
        self.assertEqual(out['row'],15); self.assertIn('other 26',out['text'])
        self.assertEqual(out['severity'],'info'); self.assertIn('cell was not changed',out['fix'])
        self.assertEqual(out['source_support']['cell'],'B15')
        self.assertEqual(item,before)
        self.assertEqual(pattern(json.loads(json.dumps(item, default=str)))['severity'],'info')
        self.assertEqual(pattern(item, None)['severity'],'high')
        from api.index import findings_binding
        original_binding=findings_binding(item,summarize(item,source_context=CONTEXT))
        self.assertNotEqual(original_binding,findings_binding(item,summarize(item)))

    def test_unrelated_missing_nok_hold_does_not_invalidate_exact_part(self):
        item,snap,compare=fixture()
        for row in snap['items']: row['raw'].pop('NOK')
        compare()
        self.assertFalse(reconcile.evidence_verified(item['source_reconciliation']))
        self.assertEqual(pattern(item)['severity'],'info')
        self.assertFalse(summarize(item,source_context=CONTEXT)['sharepoint']['source_evidence_complete'])

    def test_exact_equality_not_cosmetic_or_normalized(self):
        for value in ('00356870-v-lh','0356870-V-LH','OO356870-V-LH','00356870 V LH','00356870-V-LH ', ''):
            item,snap,compare=fixture(); snap['items'][13]['raw']['Part']=value; compare()
            self.assertEqual(pattern(item)['severity'],'high',repr(value))
        item,snap,compare=fixture('00356870-V-LH ')
        snap['items'][13]['raw']['Part']='00356870-V-LH';compare()
        self.assertEqual(pattern(item)['severity'],'high')

    def test_ambiguous_partial_wrong_source_and_stale_proof_fail_closed(self):
        mutations = [lambda i,s:s['coverage'].update(complete=False),
            lambda i,s:s['items'].append(copy.deepcopy(s['items'][13])),
            lambda i,s:s['columns'].update(Part2=dict(name='Part2',displayName='Part number')),
            lambda i,s:i['_detail_records'][13]['source'].update(list_id='other'),
            lambda i,s:s['items'][13].update(day='2026-11-09'),
            lambda i,s:s.update(captured_at=(datetime.now(timezone.utc)-timedelta(seconds=601)).isoformat()),
            lambda i,s:s.update(captured_at='invalid')]
        for mutate in mutations:
            item,snap,compare=fixture();mutate(item,snap);compare()
            self.assertEqual(pattern(item)['severity'],'high')
        for key,value in [('file_hash','b'*64),('checker_version','old'),('records_truncated',True)]:
            item,_,_=fixture();item[key]=value
            self.assertEqual(pattern(item)['severity'],'high')
        item,_,_=fixture();item['source_reconciliation']['snapshot_id']='other'
        self.assertEqual(pattern(item)['severity'],'high')
        item,_,_=fixture();item['source_reconciliation']['date_contract']={}
        with patch('hosting.source_date_contracts.receipt_contract_current',return_value=False):
            self.assertEqual(pattern(item)['severity'],'high')
        for context in (dict(CONTEXT,valid=False),dict(CONTEXT,list_id='other')):
            item,_,_=fixture();self.assertEqual(pattern(item,context)['severity'],'high')

    def test_ambiguous_workbook_headers_do_not_create_exact_labels(self):
        rows=[['Identifier','Part number','Part number','Inspected','OK','NOK'],['LOT','00356870-V-LH','other',1,1,0]]
        item=inspect_adaptive([('Inspection Detail',rows)],{'limitations':[]})
        self.assertNotIn('part',item['records'][0]['exact_labels'])

    def test_proof_tampering_and_ambiguous_records_fail_closed(self):
        item,_,_=fixture();item['source_reconciliation']['identifier_pattern_evidence']['proofs'][0]['value']='tampered'
        self.assertEqual(pattern(item)['severity'],'high')
        item,_,_=fixture();item['records'][0]['source']=item['records'][13]['source']
        self.assertEqual(pattern(item)['severity'],'high')
        item,_,_=fixture();item['records'].pop()
        self.assertEqual(pattern(item)['severity'],'high')

    def test_ai_verdict_does_not_turn_source_support_into_a_correction_or_dismissal(self):
        item,_,_=fixture();finding=pattern(item)
        item['ai_verify']=dict(status='done',run=check_run(item),verdicts={finding['key']:dict(verdict='not_an_issue',reason='fine')})
        result=summarize(item,source_context=CONTEXT)
        self.assertEqual(pattern(item)['severity'],'info')
        self.assertFalse(result['dismissed']);self.assertEqual(result['reconcile']['corrected'],0)
        self.assertTrue(result['reconcile']['adds_up'])
        item['finding_decisions']={finding['key']:dict(fp=finding['fp'],action='defer',reason='Owner investigating')}
        self.assertEqual(pattern(item)['severity'],'high')
        self.assertTrue(summarize(item,source_context=CONTEXT)['reconcile']['adds_up'])

if __name__=='__main__':unittest.main()

class BoundedSourcePatterns(unittest.TestCase):
    def test_large_outlier_set_has_bounded_receipts_and_one_hash_per_summary(self):
        import time
        from hosting.source_patterns import MAX_RECEIPT_BYTES
        for huge in (False, True):
            rows=[['Identifier','Part number','Inspected','OK','NOK']]
            rows += [[f'LOT-{n:05}', ('X'*3995+f'{n:05}' if huge else f'{n:08}-V-LH') if n>=1500 else '12345678 AA',1,1,0] for n in range(2000)]
            item=inspect_adaptive([('Inspection Detail',rows)],{'limitations':[]})
            item.update(file_hash='a'*64,checker_version=CHECKER_VERSION)
            for group in (item['records'],item['_detail_records']):
                for r in group:r['source']=dict(site_id='site-1',list_id='list-1',item=str(r['row']))
            snap=dict(site_id='site-1',list_id='list-1',captured_at=datetime.now(timezone.utc).isoformat(),columns={'Part':dict(displayName='Part number')},coverage=dict(complete=True),items=[dict(item=r['source']['item'],day='2026-11-10',raw={'Part':r['exact_labels']['part']}) for r in item['records']])
            rec=reconcile.compare('2026-11-10',item['_detail_records'],[],snap,report=dict(file_hash=item['file_hash'],checker_version=CHECKER_VERSION))
            item['source_reconciliation']=rec
            receipt=rec['identifier_pattern_evidence']
            self.assertLessEqual(len(json.dumps(receipt).encode()),MAX_RECEIPT_BYTES)
            started=time.monotonic()
            with patch('hosting.source_patterns.fingerprint',wraps=fingerprint) as hashes:
                review=summarize(item,source_context=CONTEXT)
                self.assertLessEqual(hashes.call_count,1)
            self.assertLess(time.monotonic()-started,5)
            patterns=[i for i in review['issues'] if i['code']=='part_format_differs']
            self.assertEqual(len(patterns),500)
            self.assertTrue(all(i['severity']==('high' if huge else 'info') for i in patterns))
            if huge:self.assertFalse(receipt['complete']);self.assertEqual(receipt['proofs'],[])


def persisted_fixture_inputs():
    from tests.sl_daily_fixture import sl_daily, records
    from tests.test_reconcile import snap, sp_item, COLS, SITE, LIST
    data=records(27,inspected=1)
    for r in data:r['part']='12345678 AA'
    data[11]['part']='00356870-V-LH'  # physical row 15
    source=[sp_item(432+i,inspected=1) for i in range(27)]
    for it,r in zip(source,data):it['fields']['PartNumber']=r['part']
    snapshot=snap([source],columns=COLS+[dict(name='PartNumber',displayName='Part number')])
    assigned=dict(status='manual',site=SITE,site_id='site-1',list_id=LIST,needs_person=False)
    return sl_daily(recs=data,nok_blank=True), snapshot, assigned

class PersistedSourcePatterns(unittest.TestCase):
    def test_saved_recheck_and_current_assignment_mapping_are_required(self):
        import tempfile
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        raw, snapshot, assigned = persisted_fixture_inputs()
        SITE, LIST = assigned['site'], assigned['list_id']
        with tempfile.TemporaryDirectory() as root, patch.object(SpreadsheetInbox,'assignment_for',return_value=assigned):
            box=SpreadsheetInbox(root)
            original=box.ingest('Synthetic 2214.xlsx',raw);h=original['file_hash']
            box.source_reconcile(h,snapshot,'2026-11-06',expected_check_hash=box.get(h)['check_hash'],expected_assignment=assigned)
            def finding():return next(x for x in box.review_summary(box.get(h))['issues'] if x['code']=='part_format_differs')
            self.assertEqual(finding()['severity'],'info')
            from api.index import handler
            from types import SimpleNamespace
            view=handler.report_view(SimpleNamespace(can_stored_report=lambda *a:True),box,h)
            self.assertEqual(next(x for x in view['review']['issues'] if x['code']=='part_format_differs')['severity'],'info')
            self.assertFalse(view['checked_copy']['ready'])
            import contextlib
            from hosting import public_api, delivery, ai_proposals
            store=SimpleNamespace(session=lambda:contextlib.nullcontext(root))
            api_handler=SimpleNamespace(store=lambda:store)
            public=public_api.op_get(api_handler,h)
            self.assertEqual(public['counts'],view['review']['counts'])
            self.assertEqual(next(x for x in public['issues'] if x['code']=='part_format_differs')['severity'],'info')
            self.assertEqual(delivery._load(store,h)[2]['counts'],view['review']['counts'])
            current=box.get(h); finding_now=finding()
            payload=dict(file_hash=h,expected_check_hash=current['check_hash'],run=check_run(current),expected_rev=0,ids=[finding_now['key']],seen={finding_now['key']:finding_now['fp']})
            with self.assertRaisesRegex(ValueError,'now informational'):
                ai_proposals.prepare(box,payload,lambda:True,lambda *a:self.fail('Provider must not run'))
            self.assertFalse(any(x['code']=='part_format_differs' for x in box.ai_fix_inputs(h)[2]))
            self.assertTrue(any(x['code']=='source_evidence_incomplete' for x in box.holds(box.get(h))))
            box.propose_recheck(h);box.apply_recheck(h,'Synthetic reviewer')
            box=SpreadsheetInbox(root)
            self.assertEqual(finding()['severity'],'high')  # recheck invalidates earlier source proof
            box.source_reconcile(h,snapshot,'2026-11-06',expected_check_hash=box.get(h)['check_hash'],expected_assignment=assigned)
            self.assertEqual(finding()['severity'],'info')
            self.assertEqual(box.original_bytes(h)[1],raw)
            assigned['list_id']='wrong-list'
            self.assertEqual(finding()['severity'],'high')
            assigned['list_id']=LIST
            box.set_setting(box.source_mapping_key('site-1',LIST),dict(mapping={},revision='changed'))
            self.assertEqual(finding()['severity'],'high')
