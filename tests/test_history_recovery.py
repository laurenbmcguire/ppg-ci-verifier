"""Synthetic isolated-history recovery, no live reports or providers."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_version_reuse import RAW, NAME, save
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.working_copy import WorkingCopies
from hosting import history_recovery as recovery

ACTOR = dict(id='synthetic-admin', role='admin', email='synthetic@example.invalid')

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.box=SpreadsheetInbox(self.temp.name)
        self.key=self.box.ingest(NAME,RAW)['file_hash']
        self.box._update(self.key,lambda r:r.update(
            fixed_from=dict(file_hash=self.key, revision=1, changes=[dict(cell='C4',after='2026-11-06')]),
            fixes_applied=[dict(fixed_file_hash=self.key,revision=2,changes=[dict(cell='C4',after='2026-11-06')])],
            revision=2,root_hash=self.key,
            source_reconciliation=dict(verified=False,stale=True,snapshot_id='historical-synthetic'),
            finding_decisions={'synthetic':dict(action='defer',reason='Needs evidence',sheet='CSV',cell='G2')},
            finding_decision_log=[dict(action='defer',id='synthetic')]))
    def rows(self):
        with self.box.connect() as db:
            return [r[0] for r in db.execute('SELECT result_json FROM spreadsheet_checks ORDER BY seq')]
    def plan(self):
        return self.box.recover_self_cycle(self.key,actor=ACTOR,expected_check_hash=self.box.get(self.key)['check_hash'])
    def apply(self,plan=None):
        p=plan or self.plan()
        return self.box.recover_self_cycle(self.key,actor=ACTOR,expected_check_hash=p['expected_check_hash'],recovery_token=p['recovery_token'],apply=True)
    def test_preview_is_read_only_recovery_preserves_every_old_record_and_bytes(self):
        before=self.rows();raw=self.box.download(self.key)[1];current=self.box.get(self.key)
        self.assertEqual(self.box.working_copy(self.key)['unavailable_reason'],'history_inconsistent')
        p=self.plan();self.assertEqual(self.rows(),before)
        saved=self.apply(p)
        self.assertEqual(self.rows()[:-1],before)
        self.assertEqual(self.box.download(self.key)[1],raw)
        self.assertEqual(self.box.working_copy(self.key)['status'],'current')
        self.assertEqual(saved['revision'],2)
        for k in ('source_reconciliation','finding_decisions','finding_decision_log'):
            self.assertEqual(saved[k],current[k])
        self.assertNotIn('fixed_from',saved);self.assertEqual(saved['fixes_applied'],[])
        self.assertEqual(saved['history_recovery']['from_check'],current['check_hash'])
        self.assertIsNone(saved.get('approval'));self.assertFalse(saved.get('sent'))
        self.assertEqual(SpreadsheetInbox(self.temp.name).working_copy(self.key)['status'],'current')
    def test_real_new_correction_and_exact_reuse_work_after_recovery(self):
        self.apply()
        child=save(self.box,self.key,'original','second')['file_hash']
        self.assertNotEqual(child,self.key)
        self.assertEqual(self.box.working_copy(self.key)['working_hash'],child)
        self.assertEqual(self.box.working_copy(child)['history'][0]['file_hash'],self.key)
        self.assertEqual(save(self.box,self.key,'original','second')['file_hash'],child)
        self.assertEqual(self.box.download(self.key)[1],RAW)
    def test_recheck_preserves_recovery_receipt(self):
        old=self.apply()
        self.box.propose_recheck(self.key)
        new=self.box.apply_recheck(self.key,'Synthetic')
        self.assertEqual(new['history_recovery'],old['history_recovery'])
        self.assertEqual(self.box.working_copy(self.key)['status'],'current')
    def test_stale_report_or_inventory_refuses_without_append(self):
        p=self.plan()
        self.box.ingest('other.csv',RAW.replace(b'original',b'other'))
        before=self.rows()
        with self.assertRaisesRegex(ValueError,'inventory changed'): self.apply(p)
        self.assertEqual(self.rows(),before)
        p=self.plan();self.box._update(self.key,lambda r:r.update(note='New evidence'))
        before=self.rows()
        with self.assertRaisesRegex(ValueError,'report changed'):self.apply(p)
        self.assertEqual(self.rows(),before)
    def test_member_missing_actor_and_missing_confirmation_refuse(self):
        for actor in (None,{},dict(id='member',role='member'),dict(role='admin')):
            with self.assertRaisesRegex(ValueError,'admin'):
                self.box.recover_self_cycle(self.key,actor=actor,expected_check_hash=self.box.get(self.key)['check_hash'])
        with self.assertRaisesRegex(ValueError,'inventory changed'):
            self.box.recover_self_cycle(self.key,actor=ACTOR,expected_check_hash=self.box.get(self.key)['check_hash'],apply=True)
    def test_sent_approved_or_sending_at_any_revision_refuses(self):
        for state in (dict(approval={'by':'human'}),dict(sent=[{'sent':True}]),dict(send_pending=True),dict(send_in_progress=True)):
            with self.subTest(state=state):
                with self.box.connect() as db:bases,histories=self.box._history_inventory(db)
                histories[self.key][0].update(state)
                with self.assertRaisesRegex(ValueError,'Approved, sent or sending'):
                    recovery.proposal(self.key,bases,histories)
    def test_foreign_parent_child_or_ambiguous_links_never_recovered(self):
        with self.box.connect() as db:bases,histories=self.box._history_inventory(db)
        for field,value in (('fixed_from',{'file_hash':'a'*64}),('fixes_applied',[{'fixed_file_hash':'a'*64}])):
            changed=copy.deepcopy(histories);changed[self.key][-1][field]=value
            with self.assertRaises(ValueError):recovery.proposal(self.key,bases,changed)
        for field,value in (('fixed_from',{'file_hash':self.key}),('fixes_applied',[{'fixed_file_hash':self.key}])):
            changed=copy.deepcopy(histories);changed['b'*64]=[dict(file_hash='b'*64,**{field:value})]
            with self.assertRaisesRegex(ValueError,'Another report'):recovery.proposal(self.key,bases,changed)
    def test_base_only_foreign_references_block_recovery(self):
        with self.box.connect() as db:bases,histories=self.box._history_inventory(db)
        for field,value in (('fixed_from',{'file_hash':self.key}),('fixes_applied',[{'fixed_file_hash':self.key}])):
            changed=copy.deepcopy(bases);changed['b'*64]=dict(file_hash='b'*64,**{field:value})
            with self.assertRaisesRegex(ValueError,'Another report'):
                recovery.proposal(self.key,changed,histories)
    def test_validated_recovery_status_is_not_a_raw_receipt_claim(self):
        self.apply()
        self.assertTrue(self.box.working_copy(self.key)['history_recovery_verified'])
        with self.box.connect() as db:bases,histories=self.box._history_inventory(db)
        histories[self.key][-1]['history_recovery']['prefix_sha256']='f'*64
        graph=WorkingCopies(bases,histories)
        self.assertFalse(graph.resolve(self.key)['history_recovery_verified'])
        self.assertEqual(graph.resolve(self.key)['status'],'unavailable')
    def test_tampered_receipt_prefix_or_state_does_not_unlock_graph(self):
        self.apply()
        with self.box.connect() as db:bases,histories=self.box._history_inventory(db)
        cases=[]
        h=copy.deepcopy(histories);h[self.key][-1]['history_recovery']['prefix_sha256']='f'*64;cases.append(h)
        h=copy.deepcopy(histories);h[self.key][0]['note']='Changed history';cases.append(h)
        h=copy.deepcopy(histories);h[self.key][-1]['source_reconciliation']['verified']=True;cases.append(h)
        h=copy.deepcopy(histories);h[self.key][-1]['finding_decisions']={};cases.append(h)
        h=copy.deepcopy(histories);h[self.key][-1]['history_recovery']['by']['role']='member';cases.append(h)
        h=copy.deepcopy(histories);h[self.key].append(dict(h[self.key][-1]));h[self.key][-1].pop('history_recovery');cases.append(h)
        for h in cases:self.assertEqual(WorkingCopies(bases,h).resolve(self.key)['status'],'unavailable')
    def test_original_integrity_mismatch_refuses(self):
        from hosting.files import files_for
        original=self.box.get(self.key)
        with patch.object(type(files_for(self.box.root)),'get',return_value=b'wrong'):
            with self.assertRaisesRegex(ValueError,'integrity mismatch'):self.plan()
    def test_failed_final_graph_check_rolls_back_entire_recovery(self):
        before=self.rows()
        class Invalid:
            def resolve(self,key):return dict(status='unavailable')
        with patch.object(self.box,'_working_copies',return_value=Invalid()):
            with self.assertRaisesRegex(ValueError,'Nothing was saved'):self.apply()
        self.assertEqual(self.rows(),before)
