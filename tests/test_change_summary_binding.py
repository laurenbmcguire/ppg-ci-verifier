import sys,unittest,copy,io
from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch
from hosting import change_summary as c
from hosting.spreadsheets import CHECKER_VERSION
class TestBinding(unittest.TestCase):
 assignment = dict(site_id='site',list_id='list',status='source')
 def item(self):
  from test_r8_count_contract import fixture, compare
  records,snap=fixture();counts=compare(records,snap)['count_reconciliation']
  return dict(file_hash='abc',checker_version=CHECKER_VERSION,filename='job.xlsx',source_reconciliation=dict(bound_to=dict(file_hash='abc',checker_version=CHECKER_VERSION),day='2026-11-06',site_id='site',list_id='list',snapshot_id='snap',captured_at='2026-11-08',coverage=dict(complete=True),count_reconciliation=counts,mapping_binding=dict(site_id='site',list_id='list',mapping={},revision=None,legacy_requires_confirmation=False),status='verified',verified=True,report_rows=2,sharepoint_items_for_day=2,fields_checked=['inspected','rejected'],rows=[]))
 def test_valid(self): self.assertEqual(c.source_receipt(self.item(),'2026-11-06',self.assignment)['status'],'verified')
 def test_mutations(self):
  for key,value in [('report_rows',6),('fields_checked',[]),('stale',True),('verified',False),('rows',[{'kind':'wrong_list'}]),('rows_truncated',True),('status','incomplete'),('day','wrong'),('site_id',None),('snapshot_id',None),('coverage',{'complete':False}),('bound_to',{}),('bound_to',{'file_hash':'other','checker_version':'v1'}),('bound_to',{'file_hash':'abc','checker_version':'old'})]:
   with self.subTest(key=key,value=value):
    i=self.item();i['source_reconciliation'][key]=value
    self.assertEqual(c.source_receipt(i,'2026-11-06',self.assignment)['status'],'held')
 def test_assignment_and_coverage(self):
  for assignment in ({},dict(site_id='wrong',list_id='list'),dict(site_id='site',list_id='wrong')):
   self.assertEqual(c.source_receipt(self.item(),'2026-11-06',assignment)['status'],'held')
  for status in (None,'future_unknown'):
   self.assertEqual(c.source_receipt(self.item(),'2026-11-06',dict(self.assignment,status=status))['status'],'held')
  for key in ('undated','date_fallbacks'):
   i=self.item();i['source_reconciliation']['coverage'][key]=1
   self.assertEqual(c.source_receipt(i,'2026-11-06',self.assignment)['status'],'held')
 def test_missing(self): self.assertEqual(c.source_receipt({'file_hash':'abc'},'2026-11-06',self.assignment)['status'],'held')
 def collect(self,i):
  rv=dict(job='2214',day='2026-11-06',issues=[],dismissed=[],inspected=400,rejected=None)
  class Inbox:
   def source_mapping(self,*args): return dict(site_id='site',list_id='list',mapping={},revision=None,legacy_requires_confirmation=False)
   def assignment_for(self,*args): return dict(site_id='site',list_id='list',status='source')
   def get(self,h): return i
  with patch('hosting.review_summary.summarize',return_value=rv): return c.collect(Inbox(),'abc')
 def test_pdf_current_wins_legacy_absent(self):
  from pypdf import PdfReader
  d=self.collect(self.item());txt=' '.join(p.extract_text() for p in PdfReader(io.BytesIO(c.render(d))).pages)
  self.assertIn('2 report / 2 source',txt);self.assertNotIn('not reachable',txt);self.assertNotIn('No SharePoint job list',txt);self.assertIn('Check SHA-256',txt)
 def test_legacy_does_not_override(self):
  i=self.item();i['sharepoint']={'findings':[{'message':'old mismatch'}]};d=self.collect(i)
  self.assertFalse(c.differs(d))
  from pypdf import PdfReader
  txt=' '.join(p.extract_text() for p in PdfReader(io.BytesIO(c.render(d))).pages)
  self.assertIn('Matched SharePoint',txt);self.assertNotIn('Did not match SharePoint',txt);self.assertIn('Historical upload-time',txt)
 def test_held_not_success(self):
  from pypdf import PdfReader
  i=self.item();i.pop('source_reconciliation');d=self.collect(i)
  txt=' '.join(p.extract_text() for p in PdfReader(io.BytesIO(c.render(d))).pages)
  self.assertIn('HOLD:',txt);self.assertNotIn('Matched SharePoint and needed no changes.',txt)
 def test_wrong_final_never_inherits_original(self):
  first=self.item();last=copy.deepcopy(first);last['file_hash']='new';last['fixed_from']={'file_hash':'abc'}
  class Inbox:
   def source_mapping(self,*args): return dict(site_id='site',list_id='list',mapping={},revision=None,legacy_requires_confirmation=False)
   def assignment_for(self,*args): return dict(site_id='site',list_id='list',status='source')
   def get(self,h): return first if h=='abc' else last
  rv=dict(job='2214',day='2026-11-06',issues=[],dismissed=[],inspected=400,rejected=None)
  with patch('hosting.review_summary.summarize',return_value=rv):d=c.collect(Inbox(),'new')
  self.assertEqual(d['source_receipt']['status'],'held')
 def test_save_rejects_concurrent_change(self):
  data=self.collect(self.item());changed=dict(data,action_snapshot_sha256='changed')
  class Store:
   @contextmanager
   def session(self): yield '/unused'
  class Handler:
   def store(self): return Store()
  inbox=MagicMock();inbox.setting.return_value=None
  with patch('hosting.spreadsheet_inbox.SpreadsheetInbox',return_value=inbox), patch.object(c,'collect',side_effect=[data,changed]), patch.object(c,'render',return_value=b'%PDF test'),patch('hosting.spreadsheet_inbox.files_for') as files:
   with self.assertRaisesRegex(ValueError,'changed while'): c.make(Handler(),'abc')
   files.assert_not_called()
 def test_action_and_check_fingerprints(self):
  i=self.item();a=self.collect(i);i['accepted_issues']=[{'text':'x','reason':'user decision'}];b=self.collect(i)
  self.assertNotEqual(a['action_snapshot_sha256'],b['action_snapshot_sha256'])
  self.assertEqual(a['source_receipt']['check_sha256'],b['source_receipt']['check_sha256'])
  i['source_reconciliation']['captured_at']='later';d=self.collect(i)
  self.assertNotEqual(b['source_receipt']['check_sha256'],d['source_receipt']['check_sha256'])
if __name__=='__main__':unittest.main()
