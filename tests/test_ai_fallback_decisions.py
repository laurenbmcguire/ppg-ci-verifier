import unittest
from hosting.review_decisions import decide, DecisionError
from hosting.review_summary import check_run

class AIFallbackGuard(unittest.TestCase):
    def submit(self, issue, reason, evidence):
        result={'file_hash':'h'}
        issue=dict(issue,key='k',fp='f')
        return decide(result,{'k':issue},ids=['k'],action='reject',reason=reason,evidence=evidence,
                      request_id='r',expected_rev=0,expected_run=check_run(result),expected_fps={'k':'f'})
    def test_stale_client_disclaimer_cannot_dismiss_high_or_missing_values(self):
        for issue in ({'severity':'high','code':'quantity_mismatch'}, {'severity':'medium','code':'missing_part_number'},
                      {'severity':'low','code':'identifier_format_differs','source':'SharePoint'}):
            with self.subTest(issue=issue), self.assertRaises(DecisionError):
                self.submit(issue,'Dex had no safe change to make and no correct value to put in', ['Not checked by a person. Left as written by Fix with AI.'])
    def test_fake_evidence_is_rejected_even_if_reason_changed(self):
        with self.assertRaises(DecisionError):
            self.submit({'severity':'high','code':'quantity_mismatch'},'Unchanged',['Not checked by a person.'])
    def test_explicit_reviewer_evidence_still_supported(self):
        e=self.submit({'severity':'high','code':'quantity_mismatch'},'Compared source item and confirmed value',['SharePoint item 19 inspected 10, OK 8, NOK 2'])
        self.assertEqual(e['action'],'reject')
    def test_the_automatic_fallback_is_refused_for_a_format_hint_too(self):
        with self.assertRaises(DecisionError):
            self.submit({'severity':'low','code':'part_format_differs'},'Dex had no safe change to make, so this was left as written by Fix with AI.',[])
    def test_a_person_can_still_leave_a_format_hint_as_written_in_their_own_words(self):
        e=self.submit({'severity':'low','code':'part_format_differs'},'Customer part numbers use this pattern; confirmed on the label.',[])
        self.assertEqual(e['action'],'reject')

    def test_known_fallback_marker_case_does_not_change_guard(self):
        with self.assertRaises(DecisionError):
            self.submit({'severity':'high','code':'quantity_mismatch'},'  DEX HAD NO SAFE CHANGE to make',['Other explanation'])
