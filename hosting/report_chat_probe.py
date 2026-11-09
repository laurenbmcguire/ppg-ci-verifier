"""TEMPORARY admin-only test. Remove after the approved synthetic connectivity check."""
import hashlib
from hosting import report_chat


def run_fixed_synthetic():
    # No report store, request-supplied context, external fetch or persisted state.
    raw = b'Part,Inspected,Rejected\nSYNTHETIC-CONNECTION,10,\n'
    item = dict(filename='SYNTHETIC-ASK-DEX-CONNECTION.csv', file_hash=hashlib.sha256(raw).hexdigest(),
                check_hash=hashlib.sha256(b'fixed-synthetic-ask-dex-v1').hexdigest(), revision=1,
                findings=[], sheets=[], totals={}, internal_check_status='synthetic_test_only', release_status='held')
    review = dict(status='check', counts={}, coverage={'complete':True}, issues=[])
    result = report_chat.answer(item, raw, review, [{'code':'synthetic_test','text':'Synthetic data only; never release.'}],
        [{'role':'user','content':'This is fabricated test data. What is the inspected quantity in CSV!B2, and can the blank rejected quantity in CSV!C2 be treated as zero? Answer in one short sentence.'}],
        output_tokens=128, include_usage=True)
    return dict(test='fixed-synthetic-ask-dex-v1', synthetic_only=True, persisted=False, read_only=True,
                provider=result['provider'], model=result['model'], usage=result['usage'], reply=result['reply'][:600])
