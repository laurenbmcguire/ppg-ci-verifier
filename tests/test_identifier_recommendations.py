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
