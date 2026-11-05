"""Executive overview across every job: what the verifier caught before the client saw it, what was corrected,
how reliably reports went out, and each job's numbers. Built from the reports saved in the verifier."""
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

from hosting.periods import pick_daily
from hosting.review_summary import summarize
from hosting.source_compare import identify

CATEGORIES = [
    ('Scans on the wrong shift', re.compile(r'wrong_shift')),
    ('Did not match SharePoint', re.compile(r'sharepoint|not_on_sharepoint|missing_from_report|differs_from|unmatched_report_row')),
    ('Duplicate VINs or rows', re.compile(r'duplicate')),
    ('Typos in VINs or part numbers', re.compile(r'typo')),
    ('Totals that did not add up', re.compile(r'total|summary|mismatch|exceeds|reject_log')),
    ('Blank or missing values', re.compile(r'missing|blank|placeholder')),
    ('Numbers stored as text or bad values', re.compile(r'text|numeric|spaces|fraction|negative|scientific|excel_error')),
    ('Wrong or future dates', re.compile(r'date')),
    ('Unusual day vs the usual', re.compile(r'unusual')),
]
KEY = re.compile(r'overview\|(7|30|90)\|(\d{4}-\d{2}-\d{2})\|(dashboard|slides)')


def key(days, end, what):
    return f'overview|{days}|{end.isoformat()}|{what}'


def parse_key(k):
    m = KEY.fullmatch(k or '')
    return (int(m.group(1)), date.fromisoformat(m.group(2)), m.group(3)) if m else None


def category(issue):
    if issue.get('source') == 'AI review':
        return 'Found by AI review'
    code = issue.get('code') or ''
    for name, pattern in CATEGORIES:
        if pattern.search(code):
            return name
    return 'Other checks'


def _central_day(iso):
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo('America/Chicago')).date()
    except (TypeError, ValueError):
        return None


def build(results, days, end):
    """results: every saved report result (latest check of each upload)."""
    start = end - timedelta(days=days - 1)
    groups = defaultdict(list)
    for r in results:
        job, day = identify(r.get('filename'), r)
        if job and day and start.isoformat() <= day <= end.isoformat():
            groups[(job.upper(), day)].append(r)
    jobs = defaultdict(lambda: dict(days=0, inspected=0.0, rejected=0.0, caught=0, must_fix=0, corrected=0, sent=0, on_time=0, open=0, turnaround=[]))
    cats, totals = Counter(), Counter()
    for (job, day), uploads in groups.items():
        rows = [dict(file_hash=u['file_hash'], received_at=u.get('received_at'), review=summarize(u, with_issues=False), result=u) for u in uploads]
        counted = pick_daily(rows)
        originals = [u for u in uploads if not u.get('fixed_from')] or uploads
        first = min(originals, key=lambda u: u.get('received_at') or '')
        issues = [i for i in summarize(first)['issues'] if i['severity'] != 'info']
        j = jobs[job]
        rv = counted['review']
        j['days'] += 1
        j['inspected'] += rv.get('inspected') or 0
        j['rejected'] += rv.get('rejected') or 0
        j['caught'] += len(issues)
        j['must_fix'] += sum(1 for i in issues if i['severity'] == 'high')
        for i in issues:
            cats[category(i)] += 1
        corrected = [u for u in uploads if u.get('fixed_from')]
        if corrected:
            j['corrected'] += 1
            totals['changes'] += sum(len(u['fixed_from'].get('changes') or []) for u in corrected)
        sent = [x for x in (counted['result'].get('sent') or []) if x.get('sent')]
        if sent:
            j['sent'] += 1
            sd = _central_day(sent[0]['at'])
            if sd and sd <= date.fromisoformat(day) + timedelta(days=1):
                j['on_time'] += 1
            try:
                hours = (datetime.fromisoformat(sent[0]['at']) - datetime.fromisoformat(first['received_at'])).total_seconds() / 3600
                if hours >= 0:
                    j['turnaround'].append(hours)
            except (KeyError, TypeError, ValueError):
                pass
        elif rv['status'] == 'hold':
            j['open'] += 1
        totals['uploads'] += len(uploads)
    job_rows = []
    for job, j in sorted(jobs.items()):
        job_rows.append(dict(job=job, days=j['days'], inspected=j['inspected'], rejected=j['rejected'],
                             rate=(j['rejected'] / j['inspected'] * 100) if j['inspected'] else None,
                             caught=j['caught'], must_fix=j['must_fix'], corrected=j['corrected'], sent=j['sent'], on_time=j['on_time'], open=j['open'],
                             turnaround=round(median(j['turnaround']), 1) if j['turnaround'] else None))
    t = dict(reports=sum(r['days'] for r in job_rows), jobs=len(job_rows), uploads=totals['uploads'],
             caught=sum(r['caught'] for r in job_rows), must_fix=sum(r['must_fix'] for r in job_rows),
             corrected=sum(r['corrected'] for r in job_rows), changes=totals['changes'],
             sent=sum(r['sent'] for r in job_rows), on_time=sum(r['on_time'] for r in job_rows), open=sum(r['open'] for r in job_rows),
             inspected=sum(r['inspected'] for r in job_rows), rejected=sum(r['rejected'] for r in job_rows))
    t['rate'] = (t['rejected'] / t['inspected'] * 100) if t['inspected'] else None
    t['on_time_pct'] = (t['on_time'] / t['sent'] * 100) if t['sent'] else None
    all_turn = [h for j in jobs.values() for h in j['turnaround']]
    t['turnaround'] = round(median(all_turn), 1) if all_turn else None
    label = f'Last {days} days, {start.strftime("%b")} {start.day} to {end.strftime("%b")} {end.day}, {end.year}'
    return dict(days=days, start=start.isoformat(), end=end.isoformat(), label=label, totals=t, jobs=job_rows,
                categories=[dict(reason=k, count=v) for k, v in cats.most_common()],
                generated=datetime.now(timezone.utc).strftime('%Y-%m-%d'))
