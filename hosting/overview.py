"""Executive overview using the same saved-report statistics as the KPI page."""
import re
from datetime import date, datetime, timedelta, timezone

from hosting import report_statistics as stats
from hosting.report_statistics import category, central_day as _central_day, CATEGORIES

KEY = re.compile(r'overview\|(7|30|90|365)\|(\d{4}-\d{2}-\d{2})\|(dashboard|slides)')


def key(days, end, what):
    return f'overview|{days}|{end.isoformat()}|{what}'


def parse_key(k):
    m = KEY.fullmatch(k or '')
    return (int(m.group(1)), date.fromisoformat(m.group(2)), m.group(3)) if m else None


def build(results, days, end, client_of=None):
    start = end - timedelta(days=days-1)
    records, coverage = stats.collect(results, start, end, client_of)
    jobs = []
    for job, source in sorted({(r['job'], r['source']) for r in records}):
        t = stats.totals([r for r in records if r['job'] == job and r['source'] == source])
        jobs.append(dict(t, job=job, source=source, days=t['reports'],
                         source_label=next(r['source_label'] for r in records if r['job']==job and r['source']==source)))
    label = f'Last {days} days, {start.strftime("%b")} {start.day}{", " + str(start.year) if start.year != end.year else ""} to {end.strftime("%b")} {end.day}, {end.year}'
    return dict(days=days, start=start.isoformat(), end=end.isoformat(), label=label,
                totals=stats.totals(records), jobs=jobs, categories=stats.categories(records, 'reason'),
                statistics=stats.metadata(coverage), records=stats.export(records),
                generated=datetime.now(timezone.utc).strftime('%Y-%m-%d'))
