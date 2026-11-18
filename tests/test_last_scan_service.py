import tempfile
import unittest
from pathlib import Path
from copy import deepcopy
from hosting import source_first as sf
from hosting.last_scan_service import ScheduledWorker, discover
from hosting.last_scan_local import StateStore
from hosting.last_scan_drafts import scope_key
from source_first_fixture import SCOPE

class Scheduled(unittest.TestCase):
    def test_late_supersedes_and_delivers_test_only(self):
        with tempfile.TemporaryDirectory() as d:
            now=['2026-11-10T21:00:00Z']
            scope=scope_key(dict(SCOPE,client_id='client',shift='1',zone='America/Chicago'))
            rows=[dict(id='1',scan_at='2026-11-10T20:00:00Z')]
            def fetch(s,settings):return dict(scope=s,rows=deepcopy(rows),captured_at=now[0],terminal=True)
            def render(s,op):return dict(operation_id=op,workbook_sha256='test',checklist_sha256='test')
            store=StateStore(Path(d)/'state.db')
            worker=ScheduledWorker(store.transaction,fetch,render,clock=lambda:now[0],enabled=True)
            first=worker.tick(scope,{})
            self.assertEqual(first['status'],'draft_ready')
            now[0]='2026-11-10T21:05:00Z';rows.append(dict(id='2',scan_at='2026-11-10T20:30:00Z'))
            self.assertEqual(worker.tick(scope,{})['status'],'waiting')
            self.assertEqual(worker.dispatch_test(scope,first['operation_id'],lambda **kw:self.fail('stale sent'))['status'],'not_pending')
            now[0]='2026-11-10T22:05:00Z'
            second=worker.tick(scope,{})
            self.assertEqual(second['status'],'draft_ready')
            received=[]
            def sender(**kw):received.append(kw);return dict(operation_id=kw['idempotency_key'])
            self.assertEqual(worker.dispatch_test(scope,second['operation_id'],sender)['status'],'sent')
            worker.dispatch_test(scope,second['operation_id'],sender)
            self.assertEqual(len(received),1)
            self.assertEqual(received[0]['audience'],'Lauren')

    def test_inflight_delivery_ack_retained_when_superseded(self):
        with tempfile.TemporaryDirectory() as d:
            store=StateStore(Path(d)/'state')
            scope=scope_key(dict(SCOPE,client_id='c',shift='1',zone='UTC'))
            worker=ScheduledWorker(store.transaction,None,None)
            key=sf.digest(scope)
            worker._enqueue(key,'old',{})
            def sender(**kw):
                worker._supersede(key,'new')
                return dict(operation_id='old',provider_receipt='synthetic')
            result=worker.dispatch_test(scope,'old',sender)
            self.assertEqual(result,dict(status='superseded',delivery_status='sent'))
            with store.transaction('outbox:'+key) as box:
                self.assertEqual(box['entries']['old']['ack']['provider_receipt'],'synthetic')
                self.assertEqual(box['entries']['old']['status'],'superseded')

    def test_capture_duration_and_lease_expiry(self):
        with tempfile.TemporaryDirectory() as d:
            now=['2026-11-10T21:00:00Z'];store=StateStore(Path(d)/'s')
            s=scope_key(dict(SCOPE,client_id='client',shift='1',zone='UTC'))
            def fetch(scope,settings):
                now[0]='2026-11-10T21:00:02Z'
                return dict(scope=scope,rows=[dict(id='1',scan_at='2026-11-10T20:00:00Z')],captured_at=now[0],terminal=True)
            def render(s,op):return dict(operation_id=op,workbook_sha256='a',checklist_sha256='b')
            worker=ScheduledWorker(store.transaction,fetch,render,clock=lambda:now[0],enabled=True)
            self.assertEqual(worker.tick(s,{})['status'],'draft_ready')
            key=sf.digest(s)
            with store.transaction('lease:'+key) as lease:lease.update(token='other',expires='2026-11-10T22:00:00Z')
            self.assertEqual(worker.tick(s,{})['status'],'busy')
            now[0]='2026-11-10T23:00:00Z'
            self.assertNotEqual(worker.tick(s,{})['status'],'busy')

    def test_terminal_discovery_exact_assignment_only(self):
        site=SCOPE['site_id'];lid=SCOPE['list_id']
        config=dict(sites=[site],assignments={site+'|'+lid:dict(verified=True,active=True,job='X',client_id='C',zone='UTC',business_dates=['2026-11-10'],recorded_shifts=['1','2'])})
        calls=[]
        def fetch(url):calls.append(url);return 200,dict(value=[dict(id=lid),dict(id='unmapped')]),{}
        scopes,issues=discover(config,fetch=fetch)
        self.assertEqual(len(scopes),2);self.assertEqual(issues[0]['code'],'unmapped_list')
        def failed(url):return 503,None,{}
        with self.assertRaises(Exception):discover(config,fetch=failed)

if __name__=='__main__':unittest.main()
