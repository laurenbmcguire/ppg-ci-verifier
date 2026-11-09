"""Client/job drilldowns over the shared saved-report statistics."""
from collections import Counter, defaultdict
from datetime import timedelta

from hosting import report_statistics as stats
from hosting.report_statistics import totals


def records(results, start, end, client_of):
    """Compatibility helper; every chart uses these same selected daily records."""
    recs, _ = stats.collect(results, start, end, client_of)
    kinds = defaultdict(Counter)
    for r in recs:
        kinds[r['client']].update(r['categories'])
    return stats.export(recs), kinds


def build(results, days, end, clients, client_of, client=None, job=None):
    start = end - timedelta(days=days-1)
    recs, coverage = stats.collect(results, start, end, client_of)
    names = {c['key']: c['client'] for c in clients or []}
    names['_none'] = 'Client not mapped'
    scope = [r for r in recs if (not client or r['client'] == client) and (not job or r['job'] == job.upper())]
    trend = []
    for n in range(days):
        day = (start + timedelta(days=n)).isoformat()
        trend.append(dict(totals([r for r in scope if r['day'] == day]), day=day))
    by_client = [dict(totals([r for r in recs if r['client'] == key]), key=key, name=names.get(key, key))
                 for key in sorted({r['client'] for r in recs}, key=lambda k: (k == '_none', names.get(k, k)))]
    by_job = [dict(totals([r for r in scope if r['job'] == j and r['source'] == s]), job=j, source=s,
                   source_label=next(r['source_label'] for r in scope if r['job']==j and r['source']==s))
              for j, s in sorted({(r['job'], r['source']) for r in scope})] if client else []
    label = f'{start.strftime("%b")} {start.day}{", " + str(start.year) if start.year != end.year else ""} to {end.strftime("%b")} {end.day}, {end.year}'
    return dict(days=days, start=start.isoformat(), end=end.isoformat(), label=label,
                client=client, client_name=names.get(client) if client else None, job=job,
                totals=totals(scope), trend=trend, categories=stats.categories(scope), clients=by_client, jobs=by_job,
                statistics=stats.metadata(coverage), records=stats.export(scope))
