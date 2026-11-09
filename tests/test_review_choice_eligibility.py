"""Fabricated proposals only; read-only recommendation metadata is not apply authority."""
import unittest
from unittest.mock import Mock
from hosting.spreadsheet_inbox import SpreadsheetInbox, CHECKER_VERSION
from hosting.fixes import FixError

class RecommendationEligibility(unittest.TestCase):
    def setUp(self):
        self.box = object.__new__(SpreadsheetInbox)
        self.box.holds = Mock(return_value=[])
        self.box._check_changes = Mock()
        self.item = dict(file_hash='synthetic', check_hash='check-1', checker_version=CHECKER_VERSION)
    def proposal(self, **kw):
        return dict(dict(id='p',sheet='Synthetic',cell='A2',before='a ',after='a',source='Clean-up',kind='text',sure=True), **kw)
    def mark(self, props):
        self.box._mark_recommended_proposals(self.item, props, b'fabricated-not-a-workbook')
        return [p['recommendation_eligible'] for p in props]
    def test_supported_current_proposal_bound_and_guards_called(self):
        p=self.proposal();self.assertEqual(self.mark([p]),[True]);self.assertEqual(p['recommendation_check_hash'],'check-1');self.box._check_changes.assert_called_once()
    def test_holds_and_locked_versions_fail_closed(self):
        self.box.holds.return_value=[{'hard':True}];self.assertEqual(self.mark([self.proposal()]),[False]);self.box._check_changes.assert_not_called()
        self.box.holds.return_value=[]
        for field,value in [('approval',{'by':'Reviewer'}),('send_pending',True),('fixes_applied',[{}]),('checker_version','old')]:
            with self.subTest(field=field):
                old=self.item.get(field);self.item[field]=value;self.assertEqual(self.mark([self.proposal()]),[False]);self.item[field]=old
    def test_advisory_and_blocking_holds_keep_same_bulk_guard_with_neutral_reason(self):
        for holds in [[{'hard':False}], [{'hard':True}], [{'hard':True},{'hard':False}]]:
            self.box.holds.return_value=holds
            p=self.proposal();self.assertEqual(self.mark([p]),[False])
            self.assertEqual(p['recommendation_reason'],'Review current holds before selecting recommendations.')
        self.box._check_changes.assert_not_called()
    def test_ambiguous_same_cell_and_coordinated_changes_not_bulk(self):
        self.assertEqual(self.mark([self.proposal(),self.proposal(id='p2',after='b')]),[False,False])
        self.assertEqual(self.mark([self.proposal(change_set='coordinated')]),[False])
    def test_ai_guess_noop_and_unknown_are_not_invented(self):
        for kw in [dict(source='AI fix',sure=False),dict(source='Suggested'),dict(source='Known detail'),dict(sure=False),dict(before='a',after='a')]:
            self.assertEqual(self.mark([self.proposal(**kw)]),[False])
        # Zero is an explicit proposed value, not a fallback for unknown/blank.
        p=self.proposal(before='',after='0');self.assertEqual(self.mark([p]),[True]);self.assertEqual(p['after'],'0')
    def test_person_decisions_and_stale_source_remain_held(self):
        self.item['finding_decisions']={'f':dict(action='defer',sheet='Synthetic',cell='A2')}
        self.assertEqual(self.mark([self.proposal()]),[False]);self.item['finding_decisions']={}
        self.box._check_changes.side_effect=FixError('Source snapshot changed')
        p=self.proposal();self.assertEqual(self.mark([p]),[False]);self.assertEqual(p['recommendation_reason'],'Source snapshot changed')

class RecommendationApplyGate(unittest.TestCase):
    def setup_report(self):
        import tempfile
        from tests.test_fixes import seeded, NAME
        box=SpreadsheetInbox(self.enterContext(tempfile.TemporaryDirectory()))
        raw=seeded();item=box.ingest(NAME,raw)
        self.enterContext(__import__('unittest.mock',fromlist=['patch']).patch.object(box,'holds',return_value=[]))
        current=box.get(item['file_hash'])
        props=box.fix_proposals(item['file_hash'])['proposals']
        eligible=[p for p in props if p['recommendation_eligible']]
        self.assertTrue(eligible)
        return box,current,eligible[0],raw
    def test_exact_supported_batch_creates_new_version_preserves_original(self):
        box,item,p,raw=self.setup_report()
        out=box.apply_fixes(item['file_hash'],[p],expected_check_hash=item['check_hash'],recommended_only=True)
        self.assertNotEqual(out['file_hash'],item['file_hash']);self.assertEqual(box.download(item['file_hash'])[1],raw)
    def test_new_hold_at_apply_refuses_all_and_modified_values_never_pass(self):
        box,item,p,raw=self.setup_report()
        box.holds.return_value=[{'hard':True,'text':'New source hold'}]
        with self.assertRaisesRegex(ValueError,'Recommendations changed'):
            box.apply_fixes(item['file_hash'],[p],expected_check_hash=item['check_hash'],recommended_only=True)
        box.holds.return_value=[]
        with self.assertRaisesRegex(ValueError,'Recommendations changed'):
            box.apply_fixes(item['file_hash'],[dict(p,after='9999')],expected_check_hash=item['check_hash'],recommended_only=True)
        self.assertEqual(box.download(item['file_hash'])[1],raw);self.assertFalse(box.get(item['file_hash']).get('fixes_applied'))

class ContextRecommendationBoundary(unittest.TestCase):
    def test_context_only_prepared_by_is_inspectable_but_not_advertised_for_bulk(self):
        import tempfile
        from unittest.mock import patch
        from tests.eos_fixture import build, ROWS
        box=SpreadsheetInbox(self.enterContext(tempfile.TemporaryDirectory()))
        raw=build(ROWS, overrides={('Summary','B5'):'Prepared by'})
        item=box.ingest('2230-OWENS-BASCO - EOS - 2026-10-07.xlsx',raw)
        with patch.object(box,'holds',return_value=[]):
            props=box.fix_proposals(item['file_hash'])['proposals']
        contextual=[p for p in props if p['source']=='Known detail']
        self.assertTrue(contextual, 'The fabricated header generates the exact context-only case.')
        self.assertTrue(all(not p['recommendation_eligible'] for p in contextual))
        self.assertTrue(all(p['recommendation_reason'] for p in contextual))
        self.assertEqual(box.download(item['file_hash'])[1],raw)
