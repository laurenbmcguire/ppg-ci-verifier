"""Build an explicit request from a displayed synthetic report for legacy tests."""
from hosting.review_summary import summarize


def decide_displayed(box, file_hash, **kwargs):
    item = box.get(file_hash)
    review = summarize(item)
    kwargs.setdefault('expected_rev', int(item.get('finding_decisions_rev') or 0))
    kwargs.setdefault('expected_run', review['run'])
    kwargs.setdefault('expected_fps', {x['key']: x['fp'] for x in review['issues'] + review['dismissed']})
    return box.decide_findings(file_hash, **kwargs)


def apply_displayed(box, file_hash, changes):
    displayed = box.get(file_hash)
    return box.apply_fixes(file_hash, changes, expected_check_hash=displayed['check_hash'])


def approve_displayed(box, file_hash, approver, **kwargs):
    displayed = box.get(file_hash)
    return box.approve(file_hash, approver, expected_check_hash=displayed['check_hash'],
                       expected_run=summarize(displayed)['run'], **kwargs)
