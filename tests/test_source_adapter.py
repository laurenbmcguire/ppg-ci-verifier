import copy
import unittest
from test_report_workflow import fixture
from hosting.report_workflow import fingerprint,WorkflowError
from hosting.source_adapter import convert_private_envelope


def envelope():
    m=fixture();m['source_snapshot']['source_revision']='a'*64
    outer=dict(site_id=m['scope']['site_id'],list_id=m['scope']['list_id'],business_date=m['scope']['report_date'],shift_key=m['scope']['shift_id'])
    result=dict(schema_version='private-sharepoint-envelope/1',scope=outer,source_revision='a'*64,
        source_snapshot=m['source_snapshot'],field_definitions=m['fields'],report=m['report'],
        report_id=m['report_id'],version=m['version'],generated_from_snapshot_at='2026-11-03T12:00:00Z')
    result['manifest_sha256']=fingerprint(result);return result

class SourceAdapterTests(unittest.TestCase):
    def test_convert_preserves_scope_mapping_and_source(self):
        e=envelope();m=convert_private_envelope(e)
        self.assertEqual(m['source_snapshot']['envelope_hash'],e['manifest_sha256'])
        self.assertEqual(m['report'],e['report']);self.assertEqual(m['fields'],e['field_definitions']);self.assertFalse(m['synthetic'])
    def test_hash_tampering_rejected(self):
        e=envelope();e['report']['totals']['inspected']=0
        with self.assertRaises(WorkflowError):convert_private_envelope(e)
    def test_scope_disagreement_rejected_even_rehashed(self):
        e=envelope();e['scope']['shift_key']='2';del e['manifest_sha256'];e['manifest_sha256']=fingerprint(e)
        with self.assertRaises(WorkflowError):convert_private_envelope(e)
    def test_mapping_disagreement_rejected(self):
        e=envelope();e['field_definitions'][0]['required']=False;del e['manifest_sha256'];e['manifest_sha256']=fingerprint(e)
        with self.assertRaises(WorkflowError):convert_private_envelope(e)
    def test_null_is_not_filled(self):
        e=envelope()
        for side in ('report','source_snapshot'):e[side]['records'][0]['values']['rejected']=None
        e['report']['totals']['rejected']=None;del e['manifest_sha256'];e['manifest_sha256']=fingerprint(e)
        m=convert_private_envelope(e);self.assertIsNone(m['report']['records'][0]['values']['rejected'])

    def test_outer_eligibility_blockers_are_not_dropped(self):
        from hosting.report_workflow import evaluate
        e=envelope();e['eligibility']={'blockers':['unsupported_export_schema']};del e['manifest_sha256'];e['manifest_sha256']=fingerprint(e)
        m=convert_private_envelope(e);r=evaluate(m)
        self.assertIn('source_envelope_blocker',r['finding_counts']);self.assertFalse(r['proposals'])

    def test_nested_source_blockers_survive_conversion(self):
        from hosting.report_workflow import evaluate
        e=envelope();e['source_snapshot']['evidence_blockers']=['provenance_unresolved'];del e['manifest_sha256'];e['manifest_sha256']=fingerprint(e)
        m=convert_private_envelope(e);self.assertIn('provenance_unresolved',m['source_snapshot']['evidence_blockers'])
        self.assertEqual(evaluate(m)['status'],'blocked')
    def test_malformed_declared_evidence_rejected(self):
        for field in ('eligibility','discovery'):
            e=envelope();e[field]='blocked';del e['manifest_sha256'];e['manifest_sha256']=fingerprint(e)
            with self.assertRaises(WorkflowError):convert_private_envelope(e)
    def test_eligibility_flag_cannot_silently_disappear(self):
        from hosting.report_workflow import evaluate
        e=envelope();e['eligibility']={'candidate_eligible':False,'blockers':[]};del e['manifest_sha256'];e['manifest_sha256']=fingerprint(e)
        self.assertEqual(evaluate(convert_private_envelope(e))['status'],'blocked')
