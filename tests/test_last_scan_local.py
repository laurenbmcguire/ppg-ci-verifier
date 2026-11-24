import json
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from hosting import source_first as sf
from hosting.last_scan_drafts import Runner,capture_adapter
from hosting.last_scan_local import StateStore,NativeDraftPublisher
from source_first_fixture import capture,MAP,SCOPE

class EndToEnd(unittest.TestCase):
    def test_native_draft_restart_concurrency_unknown_quantity(self):
        with tempfile.TemporaryDirectory() as folder:
            scope=dict(SCOPE,client_id='synthetic',shift='1',zone='America/Chicago')
            cap=capture();cap['records'][0]['raw']['ActualScan']='2026-11-10T20:00:00Z'
            cap['records'][0]['raw']['Rejected']=None
            cap['captured_at']='2026-11-10T21:00:00Z';cap=sf.seal({k:v for k,v in cap.items() if k not in ('snapshot_hash','envelope_hash')})
            def fetch(s):return capture_adapter(s,scan_field='ActualScan',shift_field='Shift',capture_fn=lambda s:(cap,{}),not_before=None)
            pub=NativeDraftPublisher(Path(folder)/'reports',MAP,{})
            def run(_):return Runner(StateStore(Path(folder)/'state.db').transaction,fetch,pub,enabled=True).poll(scope,'2026-11-10T21:00:00Z')
            results=list(ThreadPoolExecutor(2).map(run,range(2)))
            self.assertTrue(any(r['status']=='draft_ready' for r in results))
            self.assertEqual(len(list((Path(folder)/'reports').iterdir())),1)
            self.assertEqual(run(None)['status'],'draft_ready')
            receipt=next((Path(folder)/'reports').glob('*/receipt.json'));bundle=json.loads((receipt.parent/'checklist.json').read_text())
            self.assertEqual(bundle['source_ids'],['1'])
            self.assertTrue(any(f['field']=='rejected' and f['code']=='missing_value' for f in bundle['findings']))
            self.assertEqual(bundle['client_release'],'held')
            self.assertTrue(pub.lookup(bundle['operation_id']))
            from openpyxl import load_workbook
            workbook=load_workbook(receipt.parent/'draft.xlsx',data_only=True)
            self.assertIsNone(workbook['Summary']['K24'].value)
            self.assertEqual(workbook['Summary']['G24'].value,10)
            self.assertEqual(workbook['Summary']['I24'].value,8)
            self.assertIn('UNKNOWN',workbook['Summary']['B5'].value)
            self.assertTrue(bundle['verification']['native_layout_preserved'])
            workbook.close()
    def test_capture_failure_keeps_timer_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            store=StateStore(Path(folder)/'state.db')
            def fail(s):raise ValueError('partial pagination')
            with self.assertRaises(ValueError):Runner(store.transaction,fail,None,enabled=True).poll(dict(SCOPE,client_id='x',shift='1',zone='America/Chicago'),'2026-11-10T21:00:00Z')
