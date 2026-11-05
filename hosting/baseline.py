"""Compare a report with the job's usual day: scans, parts inspected, rejects and reject rate.

"Usual" is the median of the job's previous days, taken from earlier reports saved in the verifier
and, when there are too few, from the job's earlier SharePoint daily exports. A day far from usual
is flagged for a person to look at; it is not an error by itself (a short shift is normal).
"""
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median

MIN_DAYS = 3
MAX_DAYS = 15
LOW, HIGH = Decimal('0.5'), Decimal('2')          # flagged below half / above double the usual
FAR_LOW, FAR_HIGH = Decimal('0.25'), Decimal('4')  # "far" → high severity
RATE_GAP = Decimal('0.15')                         # reject rate must also move 15 points


def _num(value):
    try:
        return Decimal(str(value)) if value not in (None, '') else None
    except Exception:
        return None


def stats_from_result(result):
    totals = result.get('totals') or {}
    def known(field):
        t = totals.get(field) or {}
        return _num(t.get('known_sum') if t.get('known_sum') is not None else t.get('total'))
    scans = result.get('record_count')
    return dict(scans=Decimal(scans) if isinstance(scans, int) else None,
                inspected=known('inspected'), rejected=known('rejected'))


def stats_from_rows(rows):
    def total(field):
        vals = [r['values'].get(field) for r in rows if r['values'].get(field) is not None]
        return sum(vals, Decimal(0)) if vals else None
    return dict(scans=Decimal(len(rows)), inspected=total('inspected'), rejected=total('rejected'))


def _rate(stats):
    ins, rej = stats.get('inspected'), stats.get('rejected')
    return (rej / ins) if ins and rej is not None and ins > 0 else None


def _fmt(n):
    if n is None:
        return '—'
    n = Decimal(n)
    return f'{n:,.0f}' if n == n.to_integral_value() or abs(n) >= 100 else f'{n:,.1f}'


def _pct(r):
    return f'{(r * 100):.0f}%'


def compare_to_usual(today, history, job=None, day=None, source_label=None):
    """today: stats dict; history: [{'day', 'source', scans, inspected, rejected}] for other days of the same job."""
    history = sorted((h for h in history if h.get('day') and h.get('day') != day), key=lambda h: h['day'], reverse=True)
    seen, unique = set(), []
    for h in history:  # one entry per day; the caller lists its preferred source first
        if h['day'] not in seen:
            seen.add(h['day']); unique.append(h)
    history = unique[:MAX_DAYS]
    base = dict(job=job, day=day, compared_at=datetime.now(timezone.utc).isoformat(), days=len(history),
                history=[{k: (_fmt(v) if isinstance(v, Decimal) else v) for k, v in h.items()} for h in history],
                today={k: _fmt(v) for k, v in today.items()}, findings=[], usual={})
    if len(history) < MIN_DAYS:
        base.update(status='not_enough_history',
                    message=f'Only {len(history)} earlier day(s) found for {job or "this job"}; at least {MIN_DAYS} are needed to judge what is usual.')
        return base
    span = f'{len(history)} earlier days' + (f' from {source_label}' if source_label else '') + f', {history[-1]["day"]} to {history[0]["day"]}'
    labels = {'scans': ('scans / rows', 'scans'), 'inspected': ('parts inspected', 'inspected'), 'rejected': ('rejects', 'rejected')}
    for field in ('scans', 'inspected', 'rejected'):
        values = [h[field] for h in history if h.get(field) is not None]
        now = today.get(field)
        if len(values) < MIN_DAYS or now is None:
            continue
        usual = median(values)
        base['usual'][field] = dict(median=_fmt(usual), low=_fmt(min(values)), high=_fmt(max(values)))
        if field == 'rejected':
            continue  # rejects are judged by rate below; raw counts swing with volume
        name = labels[field][0]
        if usual == 0:
            continue
        ratio = now / usual
        if now == 0:
            sev, msg = 'high', f'No {name} today. {job or "This job"} usually has about {_fmt(usual)} (range {_fmt(min(values))}–{_fmt(max(values))}, {span}).'
        elif ratio < LOW or ratio > HIGH:
            direction = f'{(1 - ratio) * 100:.0f}% fewer' if ratio < 1 else f'{ratio:.1f}× more'
            sev = 'high' if ratio < FAR_LOW or ratio > FAR_HIGH else 'medium'
            msg = (f'{_fmt(now)} {name} today vs. about {_fmt(usual)} on a usual day ({direction}; range {_fmt(min(values))}–{_fmt(max(values))}, {span}). '
                   'Check for missing shifts or scans, a duplicated upload, or a short/extra shift.')
        else:
            continue
        base['findings'].append(dict(code=f'unusual_{field}_volume', severity=sev, field=field, message=msg,
                                     today=_fmt(now), usual=_fmt(usual), sheet='Compared with usual'))
    rates = [r for r in (_rate(h) for h in history) if r is not None]
    now_rate = _rate(today)
    if len(rates) >= MIN_DAYS and now_rate is not None:
        usual_rate = median(rates)
        base['usual']['reject_rate'] = dict(median=_pct(usual_rate), low=_pct(min(rates)), high=_pct(max(rates)))
        base['today']['reject_rate'] = _pct(now_rate)
        gap = now_rate - usual_rate
        if abs(gap) >= RATE_GAP and (usual_rate == 0 or now_rate / usual_rate >= HIGH or now_rate / usual_rate <= LOW):
            base['findings'].append(dict(
                code='unusual_reject_rate', severity='high' if abs(gap) >= 2 * RATE_GAP else 'medium', field='reject_rate',
                today=_pct(now_rate), usual=_pct(usual_rate), sheet='Compared with usual',
                message=(f'Reject rate {_pct(now_rate)} today vs. about {_pct(usual_rate)} usually (range {_pct(min(rates))}–{_pct(max(rates))}, {span}). '
                         + ('Confirm the rejects were keyed correctly, or note a real quality problem.' if gap > 0
                            else 'Confirm rejects were not left out.'))))
    base['status'] = 'unusual' if base['findings'] else 'normal'
    base['message'] = (f'{len(base["findings"])} measure(s) far from the usual day ({span}).' if base['findings']
                       else f'In line with the usual day ({span}).')
    return base
