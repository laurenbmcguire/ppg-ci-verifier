import unittest
from contextlib import contextmanager
from copy import deepcopy
from hosting.last_scan_drafts import observe, Runner

S=dict(job='4476',site_id='site',list_id='list',business_date='2026-11-17',client_id='demo',shift='1',zone='America/Chicago')
def snap(at='2026-11-17T21:00:00Z',scan='2026-11-17T20:00:00Z',end='2026-11-17T20:30:00Z'):
    return dict(scope=S,terminal=True,captured_at=at,not_before=end,rows=[dict(id='1',scan_at=scan,raw={'NOK':None})])
class Timing(unittest.TestCase):
    def test_exact_hour_and_shift_boundary(self):
        self.assertEqual(observe(None,S,snap(),'2026-11-17T21:00:00Z')['status'],'due')
        self.assertEqual(observe(None,S,snap(at='2026-11-17T20:59:59Z'),'2026-11-17T20:59:59Z')['status'],'waiting')
    def test_lunch_cannot_finish_shift(self):
        x=snap(end='2026-11-18T00:00:00Z'); self.assertEqual(observe(None,S,x,x['captured_at'])['status'],'waiting')
    def test_no_schedule_unknown_not_default(self):
        x=snap(end=None); self.assertEqual(observe(None,S,x,x['captured_at'])['timing_basis'],'provisional_quiet_window_schedule_unknown')
    def test_late_scan_resets_full_hour(self):
        p=observe(None,S,snap(),'2026-11-17T21:00:00Z'); x=snap(at='2026-11-17T21:10:00Z'); x['rows'].append(dict(id='2',scan_at='2026-11-17T19:00:00Z'))
        self.assertEqual(observe(p,S,x,x['captured_at'])['due_at'],'2026-11-17T22:10:00+00:00')
    def test_edit_and_delete_reset(self):
        p=observe(None,S,snap(),'2026-11-17T21:00:00Z'); x=snap(at='2026-11-17T21:10:00Z'); x['rows'][0]['raw']['NOK']=2
        self.assertEqual(observe(p,S,x,x['captured_at'])['status'],'waiting')
        x['rows']=[]; self.assertEqual(observe(p,S,x,x['captured_at'])['status'],'no_scans')
    def test_naive_future_partial_and_duplicate_rejected(self):
        for mutate in [lambda x:x.update(terminal=False),lambda x:x['rows'].append(x['rows'][0]),lambda x:x['rows'][0].update(scan_at='2026-11-17'),lambda x:x['rows'][0].update(scan_at='2026-11-18T00:00:00Z')]:
            x=snap(); mutate(x)
            with self.assertRaises(ValueError): observe(None,S,x,x['captured_at'])
    def test_scope_isolation(self):
        p=observe(None,S,snap(),'2026-11-17T21:00:00Z')
        with self.assertRaises(ValueError):observe(p,dict(S,client_id='other'),snap(),'2026-11-17T21:00:00Z')
    def test_dst_offset_is_elapsed_hour(self):
        x=snap(at='2026-11-01T01:45:00-06:00',scan='2026-11-01T01:30:00-05:00',end='2026-11-01T01:00:00-06:00')
        self.assertEqual(observe(None,S,x,x['captured_at'])['status'],'due')
    def test_runner_repeat_and_uncertain_no_resend(self):
        state={}; calls=[]
        @contextmanager
        def tx(key): yield state.setdefault(key,{})
        def render(s,op):calls.append(op);return dict(operation_id=op,workbook_sha256='a'*64,checklist_sha256='b'*64)
        r=Runner(tx,lambda s:snap(),render,enabled=True)
        self.assertEqual(r.poll(S,'2026-11-17T21:00:00Z')['status'],'draft_ready')
        r.poll(S,'2026-11-17T21:00:00Z');self.assertEqual(len(calls),1)
        state.clear()
        def fail(s,op):calls.append(op);raise TimeoutError()
        r.render=fail;r.poll(S,'2026-11-17T21:00:00Z');r.poll(S,'2026-11-17T21:00:00Z');self.assertEqual(len(calls),2)
    def test_disabled_no_capture(self):
        self.assertEqual(Runner(None,None,None).poll(S,'bad')['status'],'disabled')
if __name__=='__main__':unittest.main()
