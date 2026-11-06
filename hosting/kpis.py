"""Reporting KPIs for every client, with drill-down by client and job.

One record per job-day (the daily report that counts for that day), then the same totals at every level:
errors caught, errors stopped before the client, errors that reached a client, reports made and sent, on-time
sending, turnaround, jobs, and the inspection numbers (parts inspected, accepted, rejected, reject rate).
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import median

from hosting.overview import _central_day, category
from hosting.periods import pick_daily
from hosting.review_summary import summarize
from hosting.source_compare import identify


def _issues(result):
    return [i for i in summarize(result)['issues'] if i['severity'] != 'info']


def records(results, start, end, client_of):
    """[{job, day, client, ...}] for each job-day in the range, plus a Counter of error kinds caught."""
    groups = defaultdict(list)
    for r in results:
        job, day = identify(r.get('filename'), r)
        if job and day and start.isoformat() <= day <= end.isoformat():
            groups[(job.upper(), day)].append(r)
    out, kinds = [], defaultdict(Counter)
    for (job, day), uploads in groups.items():
        rows = [dict(file_hash=u['file_hash'], received_at=u.get('received_at'), review=summarize(u, with_issues=False), result=u) for u in uploads]
        counted = pick_daily(rows)
        originals = [u for u in uploads if not u.get('fixed_from')] or uploads
        first = min(originals, key=lambda u: u.get('received_at') or '')
        caught = _issues(first)
        remaining = _issues(counted['result']) if counted['result'] is not first else caught
        rv = counted['review']
        sent = [x for x in (counted['result'].get('sent') or []) if x.get('sent')]
        on_time, hours = False, None
        if sent:
            sd = _central_day(sent[0]['at'])
            on_time = bool(sd and sd <= date.fromisoformat(day) + timedelta(days=1))
            try:
                hours = (datetime.fromisoformat(sent[0]['at']) - datetime.fromisoformat(first['received_at'])).total_seconds() / 3600
                hours = hours if hours >= 0 else None
            except (KeyError, TypeError, ValueError):
                hours = None
        client = client_of(job) or '_none'
        for i in caught:
            kinds[client][category(i)] += 1
        out.append(dict(job=job, day=day, client=client, uploads=len(uploads), corrected=any(u.get('fixed_from') for u in uploads),
                        inspected=rv.get('inspected') or 0, rejected=rv.get('rejected') or 0, certified=rv.get('certified') or 0,
                        caught=len(caught), must_fix=sum(1 for i in caught if i['severity'] == 'high'),
                        remaining=len(remaining), remaining_must=sum(1 for i in remaining if i['severity'] == 'high'),
                        sent=bool(sent), on_time=on_time, hours=hours, hold=not sent and rv.get('status') == 'hold'))
    return out, kinds


def totals(recs):
    t = dict(reports=len(recs), jobs=len({r['job'] for r in recs}), clients=len({r['client'] for r in recs}),
             uploads=sum(r['uploads'] for r in recs), corrected=sum(r['corrected'] for r in recs),
             inspected=sum(r['inspected'] for r in recs), rejected=sum(r['rejected'] for r in recs), certified=sum(r['certified'] for r in recs),
             caught=sum(r['caught'] for r in recs), must_fix=sum(r['must_fix'] for r in recs),
             sent=sum(r['sent'] for r in recs), on_time=sum(r['on_time'] for r in recs), hold=sum(r['hold'] for r in recs))
    # errors stopped = caught on the first upload but gone from what counts (fixed, or sent clean); reached = still there when sent
    t['stopped'] = sum(max(r['caught'] - r['remaining'], 0) for r in recs)
    t['stopped_must'] = sum(max(r['must_fix'] - r['remaining_must'], 0) for r in recs)
    t['reached'] = sum(r['remaining'] for r in recs if r['sent'])
    t['reached_must'] = sum(r['remaining_must'] for r in recs if r['sent'])
    t['unsent'] = t['reports'] - t['sent']
    t['waiting'] = sum(r['remaining'] for r in recs if not r['sent'])
    t['waiting_must'] = sum(r['remaining_must'] for r in recs if not r['sent'])
    t['rate'] = round(t['rejected'] / t['inspected'] * 100, 2) if t['inspected'] else None
    t['on_time_pct'] = round(t['on_time'] / t['sent'] * 100, 1) if t['sent'] else None
    hours = [r['hours'] for r in recs if r['hours'] is not None]
    t['turnaround'] = round(median(hours), 1) if hours else None
    return t


def build(results, days, end, clients, client_of, client=None, job=None):
    start = end - timedelta(days=days - 1)
    recs, kinds = records(results, start, end, client_of)
    names = {c['key']: c['client'] for c in clients or []}
    names['_none'] = 'Other (job not on a client site)'
    scope = [r for r in recs if (not client or r['client'] == client) and (not job or r['job'] == job.upper())]
    by_day = defaultdict(list)
    for r in scope:
        by_day[r['day']].append(r)
    trend = []
    for n in range(days):
        d = (start + timedelta(days=n)).isoformat()
        t = totals(by_day.get(d, []))
        trend.append(dict(day=d, reports=t['reports'], caught=t['caught'], stopped=t['stopped'], reached=t['reached'], inspected=t['inspected'], rejected=t['rejected'], rate=t['rate']))
    cat = Counter()
    for k, c in kinds.items():
        if not client or k == client:
            cat.update(c)
    if job:   # error kinds for one job
        cat = Counter()
        for r in results:
            j, d = identify(r.get('filename'), r)
            if j and j.upper() == job.upper() and d and start.isoformat() <= d <= end.isoformat() and not r.get('fixed_from'):
                cat.update(category(i) for i in _issues(r))
    by_client = []
    for key in sorted({r['client'] for r in recs}, key=lambda k: (k == '_none', names.get(k, k))):
        by_client.append(dict(key=key, name=names.get(key, key), **totals([r for r in recs if r['client'] == key])))
    by_job = []
    if client:
        for j in sorted({r['job'] for r in scope}):
            by_job.append(dict(job=j, **totals([r for r in scope if r['job'] == j])))
    label = f'{start.strftime("%b")} {start.day}{", " + str(start.year) if start.year != end.year else ""} to {end.strftime("%b")} {end.day}, {end.year}'
    return dict(days=days, start=start.isoformat(), end=end.isoformat(), label=label, client=client, client_name=names.get(client) if client else None, job=job,
                totals=totals(scope), trend=trend, categories=[dict(kind=k, count=v) for k, v in cat.most_common()],
                clients=by_client, jobs=by_job)
