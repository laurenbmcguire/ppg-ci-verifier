"""Synthetic count/identifier evidence only; no files, databases or providers used."""
import copy
import unittest

from hosting import recommend
from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.spreadsheets import CHECKER_VERSION


class IdentifierRecommendations(unittest.TestCase):
    def recommendation(self, identifier, count=1):
        facts = recommend.report_facts({}, dict(status="check", counts={}, sharepoint={}), 0)
        facts.update(vins_on_other_days_count=count,
                     vins_on_other_days=[dict(vin=identifier, day="2026-11-01")])
        before = copy.deepcopy(facts)
        result = next(r for r in recommend.report_rules(facts) if "identifier/day" in r["title"])
        self.assertEqual(facts, before)
        self.assertEqual(result["kind"], "check")
        self.assertIsNone(result["do"])
        self.assertIn("not proof of a duplicate inspection", result["why"])
        self.assertIn("do not delete rows", result["why"])
        self.assertNotIn("VIN", result["title"])
        self.assertNotIn("car counted", result["why"])
        self.assertNotIn("billed", result["why"])
        return result

    def test_generic_punctuation_collision_is_only_normalized_identifier_evidence(self):
        self.assertEqual(SpreadsheetInbox.vin_key("LOT-17"), SpreadsheetInbox.vin_key("LOT 17"))
        result = self.recommendation("LOT-17")
        self.assertIn("1 normalized identifier/day match across reports", result["title"])
        self.assertIn("LOT-17", result["why"])

    def test_vin_shaped_value_does_not_prove_duplicate_inspection(self):
        self.recommendation("1M8GDM9AXKP042788")

    def test_capped_matches_not_unique_vehicles_or_inspections(self):
        self.assertIn("20+ normalized identifier/day matches", self.recommendation("LOT-17", 20)["title"])


class ListedFindingCoverage(unittest.TestCase):
    def test_complete_truncated_and_history_are_distinct_units(self):
        report = dict(filename="Synthetic.csv", file_hash="synthetic", checker_version=CHECKER_VERSION,
                      record_count=1, findings=[
                          dict(code="missing_identifier", sheet="S", row=2, column="A"),
                          dict(code="extra_spaces", sheet="S", row=2, column="A"),
                          dict(code="external_hyperlink", sheet="S", row=2, column="B")],
                      finding_count=3, finding_counts=dict(missing_identifier=1, extra_spaces=1, external_hyperlink=1))
        complete = summarize(report)
        self.assertFalse(complete["coverage"]["truncated"])
        self.assertEqual(complete["reconcile"]["open"], 2)
        self.assertEqual(complete["reconcile"]["informational"], 1)
        report["finding_count"] = 33
        report["finding_counts"]["external_hyperlink"] = 31
        truncated = summarize(report)
        self.assertEqual(truncated["coverage"]["not_listed"], 30)
        self.assertTrue(truncated["coverage"]["truncated"])
        self.assertEqual(truncated["reconcile"]["total"], 3)
        self.assertTrue(truncated["reconcile"]["adds_up"], "Partition arithmetic does not prove complete coverage")
        report["fixes_applied"] = [dict(changes=[dict(sheet="S", cell="A2", before=" A ", after="A")], revision=2)]
        historical = summarize(report)
        self.assertEqual(historical["reconcile"]["corrected"], 2, "One cell change can address two findings")
        self.assertEqual(historical["reconcile"]["open"], 0)
        self.assertEqual(len(historical["dismissed"]), 2)
        self.assertEqual(historical["coverage"]["not_listed"], 30)


class GenericOverviewGuidance(unittest.TestCase):
    def test_generic_findings_use_evidence_based_guidance_without_assumed_cause(self):
        from hosting.report_statistics import category
        cases = [
            ("duplicate_identifier", "Duplicate VINs or rows", "does not prove a duplicate inspection"),
            ("identifier_unusual", "Typos in VINs or part numbers", "does not establish a typo"),
            ("missing_identifier", "Blank or missing values", "Keep unknown values unknown"),
        ]
        for code, expected_category, evidence_limit in cases:
            with self.subTest(code=code):
                reason = category(dict(code=code))
                self.assertEqual(reason, expected_category, "Existing category contract is unchanged")
                overview = dict(categories=[dict(reason=reason, count=1)], jobs=[],
                                totals=dict(on_time_pct=None, reports=1, sent=1))
                before = copy.deepcopy(overview)
                result = recommend.overview_rules(overview)[0]
                self.assertEqual(overview, before)
                self.assertEqual(result["kind"], "process")
                self.assertIsNone(result["do"])
                self.assertIn(expected_category, result["title"])
                self.assertIn(evidence_limit, result["why"])
                for unsupported in ("same car", "two logins", "come from typing", "usually mean", "skipped field"):
                    self.assertNotIn(unsupported, result["why"])


class RecommendationCompleteness(unittest.TestCase):
    def facts(self, mutate=None):
        report = dict(filename="Synthetic.csv", file_hash="synthetic", checker_version=CHECKER_VERSION,
                      record_count=1, findings=[], finding_count=0, finding_counts={})
        review = summarize(report)
        if mutate:
            mutate(review)
        return recommend.report_facts(report, review, 0)

    def guidance(self, facts):
        return next(r["why"] for r in recommend.report_rules(facts) if r["do"] == "send_for_review")

    def test_missing_stored_findings_cannot_produce_all_clear(self):
        facts = self.facts(lambda rv: rv["coverage"].update(findings_total=30, not_listed=30, truncated=True))
        self.assertEqual(facts["counts"]["high"], 0)
        self.assertFalse(facts["review_detail"]["workbook_storage_complete"])
        self.assertIn("incomplete or unknown", self.guidance(facts))

    def test_classification_mismatch_cannot_produce_all_clear(self):
        facts = self.facts(lambda rv: rv["reconcile"].update(open=1))
        self.assertFalse(facts["review_detail"]["classification_complete"])
        self.assertIn("incomplete or unknown", self.guidance(facts))
        mismatched_counts = self.facts(lambda rv: rv["counts"].update(medium=1))
        self.assertFalse(mismatched_counts["review_detail"]["classification_complete"])
        self.assertIn("incomplete or unknown", self.guidance(mismatched_counts))

    def test_missing_and_partial_details_cannot_produce_all_clear(self):
        for mutate in (lambda rv: rv.pop("coverage"), lambda rv: rv.pop("issues"),
                       lambda rv: rv.update(page=dict(issues_total=1, dismissed_total=0, complete=False))):
            with self.subTest(mutate=mutate):
                self.assertIn("incomplete or unknown", self.guidance(self.facts(mutate)))

    def test_complete_no_required_correction_is_qualified_not_source_verification(self):
        facts = self.facts()
        self.assertTrue(all(facts["review_detail"][k] for k in ("workbook_storage_complete", "classification_complete", "finding_details_complete")))
        self.assertIn("No high-severity findings", self.guidance(facts))
        self.assertIn("does not establish source completeness or release readiness", self.guidance(facts))
        self.assertNotIn("Nothing must be fixed", self.guidance(facts))

    def test_completeness_changes_and_legacy_facts_invalidate_cached_all_clear(self):
        complete = self.facts()
        legacy = copy.deepcopy(complete)
        legacy.pop("review_detail")
        partial = self.facts(lambda rv: rv["coverage"].update(findings_total=30, not_listed=30, truncated=True))
        for old, current in ((legacy, complete), (complete, partial)):
            cached = dict(fp=recommend.fingerprint(old), recs=dict(items=[dict(why="Nothing must be fixed")], source="ai"))
            out, saved = recommend.answer(cached, current, recommend.report_rules, "report", False, False)
            self.assertEqual(out["source"], "rules")
            self.assertIsNone(saved)
            self.assertNotIn("Nothing must be fixed", str(out["items"]))
