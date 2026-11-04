import csv, io, json, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from model import Review, Conflict, digest, csv_bytes

class ReviewTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.r=Review(self.t.name)
 def tearDown(self): self.t.cleanup()
 def act(self,a,**p): return self.r.act(a,self.r.view()['revision'],p)
 def approve(self):
  self.act('validate'); return self.act('approve',role='reviewer',reviewer='Demo reviewer',reviewed=True)
 def edit(self,value='Motor launch',row=0,col=2): return self.act('edit',row=row,col=col,value=value)
 def test_initial_unreviewed(self): self.assertEqual(self.r.view()['status'],'unreviewed')
 def test_original_immutable_through_edit(self):
  p=Path(self.t.name)/'original/SYNTHETIC_original.csv'; before=p.read_bytes(); self.edit(); self.assertEqual(before,p.read_bytes());self.assertEqual(p.stat().st_mode&0o222,0)
 def test_provenance_preserved(self):
  self.edit(); s=self.r.view();self.assertEqual(s['original'][0][2],'M0tor launch');self.assertEqual(s['rows'][0][2],'Motor launch')
 def test_ambiguity_not_silently_corrected(self):
  self.act('validate');s=self.r.view();self.assertEqual(s['rows'][0][2],'M0tor launch');self.assertTrue(any(c['cell']=='C2' for c in s['checks']))
 def test_volume_flag_and_zero_baseline(self):
  cells={c['cell'] for c in self.r.view()['checks']};self.assertTrue({'E2','E4'}<=cells)
 def test_explanation_not_required(self): self.assertEqual(self.approve()['status'],'approved')
 def test_validation_required(self):
  with self.assertRaises(Conflict):self.act('approve',role='reviewer',reviewer='Demo',reviewed=True)
 def test_review_ack_required(self):
  self.act('validate')
  with self.assertRaises(Conflict):self.act('approve',role='reviewer',reviewer='Demo')
 def test_unassigned_reviewer_blocked(self):
  self.act('validate')
  with self.assertRaises(Conflict):self.act('approve',role='reviewer',reviewed=True)
 def test_role_restricted(self):
  self.act('validate')
  with self.assertRaises(Conflict):self.act('approve',role='editor',reviewer='Demo',reviewed=True)
 def test_approved_exact_bytes(self):
  s=self.approve();self.assertEqual((Path(self.t.name)/s['approval']['path']).read_bytes(),csv_bytes(s['rows']));self.assertEqual(s['approval']['hash'],s['hash'])
 def test_audit_sidecar_exists(self):
  s=self.approve();a=json.loads((Path(self.t.name)/(s['approval']['path']+'.approval-1.audit.json')).read_text());self.assertEqual(a['approval'],s['approval']);self.assertEqual(a['history'][-1]['event'],'approved')
 def test_repeated_approval_idempotent(self):
  a=self.approve()['approval'];b=self.approve()['approval'];self.assertEqual(a,b);self.assertEqual(sum(x['event']=='approved' for x in self.r.view()['audit']),1)
 def test_edit_invalidates_approval(self):
  self.approve();s=self.edit();self.assertIsNone(s['approval']);self.assertIsNone(s['validated'])
 def test_stale_approval_rejected(self):
  self.act('validate');old=self.r.view()['revision'];self.edit()
  with self.assertRaises(Conflict):self.r.act('approve',old,dict(role='reviewer',reviewer='Demo',reviewed=True))
 def test_conflicting_edit_rejected(self):
  self.edit()
  with self.assertRaises(Conflict):self.r.act('edit',1,dict(row=0,col=2,value='Other'))
 def test_changed_original_fail_closed(self):
  self.approve();p=Path(self.t.name)/'original/SYNTHETIC_original.csv';p.chmod(0o644);p.write_text('changed')
  with self.assertRaises(Conflict):self.r.view()
 def test_corrupt_approved_fail_closed(self):
  s=self.approve();p=Path(self.t.name)/s['approval']['path'];p.chmod(0o644);p.write_text('changed')
  with self.assertRaises(Conflict):self.act('generate')
 def test_unapproved_report_blocked(self):
  with self.assertRaises(Conflict):self.act('generate')
 def test_generate_approved_and_retry(self):
  s=self.approve();a=self.act('generate')['simulated_report'];b=self.act('generate')['simulated_report'];self.assertEqual(a,b);r=json.loads((Path(self.t.name)/a).read_text());self.assertEqual(r['total_volume'],274);self.assertEqual(r['approved_input']['hash'],s['hash'])
 def test_private_download_unapproved_not_publication(self):
  self.approve();b=self.r.download(1);self.assertIn(b'UNAPPROVED',b);self.assertNotEqual(digest(b),self.r.view()['hash']);self.assertIsNone(self.r.view()['simulated_report'])
 def test_leave_unreviewed_clears_approval(self):
  self.approve();s=self.act('leave');self.assertIsNone(s['approval']);self.assertEqual(s['status'],'unreviewed')
 def test_unanswered_no_approval(self):
  self.act('request');self.act('request');self.act('timeout');self.act('timeout');s=self.r.view();self.assertIsNone(s['approval']);self.assertFalse(s['request']['sent']);self.assertEqual(sum(a['event']=='reminder_simulated' for a in s['audit']),1)
 def test_reload_recovers_exact_approval(self):
  a=self.approve()['approval'];r=Review(self.t.name);self.assertEqual(r.view()['approval'],a)
 def test_block_current_week(self):
  self.edit('2026-10-19',col=1);self.act('validate')
  with self.assertRaises(Conflict):self.act('approve',role='reviewer',reviewer='Demo',reviewed=True)
 def test_ambiguous_number_preserved_and_blocked(self):
  self.edit('1O0',col=4);self.act('validate');self.assertEqual(self.r.view()['rows'][0][4],'1O0')
  with self.assertRaises(Conflict):self.act('approve',role='reviewer',reviewer='Demo',reviewed=True)
 def test_formula_injection_blocked(self):
  self.edit('=WEBSERVICE("https://example.com")');self.act('validate')
  with self.assertRaises(Conflict):self.act('approve',role='reviewer',reviewer='Demo',reviewed=True)
 def test_corrupt_state_fails_closed(self):
  with self.r.connect() as c:c.execute("UPDATE state SET data='{}'")
  with self.assertRaises((KeyError,Conflict)):self.r.view()
 def test_same_value_is_noop(self):
  self.approve();s=self.edit('M0tor launch');self.assertEqual(s['revision'],1);self.assertIsNotNone(s['approval'])
 def test_orphan_artifact_not_authority(self):
  (Path(self.t.name)/'approved').mkdir();(Path(self.t.name)/'approved/orphan.csv').write_text('orphan')
  with self.assertRaises(Conflict):self.act('generate')
 def test_reapprove_after_leaving(self):
  self.approve();self.act('generate');self.act('leave');s=self.approve();self.assertEqual(s['approval']['id'],2);self.act('generate')
 def test_formula_download_blocked(self):
  self.edit('=1+1')
  with self.assertRaises(Conflict):self.r.download(2)
 def test_separate_working_spreadsheet(self):
  s=self.edit();self.assertTrue(s['working_path'].startswith('working/'));self.assertEqual((Path(self.t.name)/s['working_path']).read_bytes(),csv_bytes(s['rows']))

if __name__=='__main__':unittest.main(verbosity=2)
