"""Shared, read-only reporting statistics. Counts describe saved evidence, not client outcomes."""
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from statistics import median

from hosting.review_summary import summarize
from hosting.source_compare import identify

CATEGORIES = [
    ('Scans on the wrong shift', re.compile(r'wrong_shift')),
    ('Did not match SharePoint', re.compile(r'sharepoint|not_on_sharepoint|missing_from_report|differs_from|unmatched_report_row')),
    ('Duplicate VINs or rows', re.compile(r'duplicate')),
    ('Typos in VINs or part numbers', re.compile(r'typo|vin_|identifier_unusual|fill_series|leading_zeros')),
    ('Already reported on another day', re.compile(r'other_day')),
    ('Totals that did not add up', re.compile(r'total|summary|mismatch|exceeds|reject_log|defect_reject')),
    ('Blank or missing values', re.compile(r'missing|blank|placeholder')),
    ('Numbers stored as text or bad values', re.compile(r'text|numeric|spaces|fraction|negative|scientific|excel_error')),
    ('Wrong or future dates', re.compile(r'date')),
    ('Unusual day vs the usual', re.compile(r'unusual')),
]
QUANTITIES = ('inspected', 'rejected', 'certified')
DEFINITIONS = dict(
    reports='One selected version per source, job and report date. Date filters use the report date, not the upload date.',
    selection='Recorded sent version, otherwise currently approved version, otherwise corrected copy, otherwise newest upload. Ties use the file ID.',
    caught='Listed non-informational findings in the current review of the earliest original upload per daily report, or earliest saved copy if the original is absent. These are findings, not confirmed distinct errors or an immutable first-check history.',
    current_issues='Listed non-informational findings in the current review of each selected version. Different checks may describe the same underlying problem.',
    corrected='Daily reports with a saved corrected-copy record. This does not prove a defect was fixed or that a client had not seen it.',
    quantities='Reported quantities, not an independent validation of the source. The saved inspected/rejected/certified schema represents part quantities when no unit override is present. Explicit row counts or generic units are not added to parts. Unknown, partial, non-finite or incompatible quantities do not become zero. Sums use decimal arithmetic; values beyond 10^15, beyond 12 decimal places, or not safely representable in JSON numbers are unknown. Known subtotals and coverage are separate.',
    sent='Daily reports with a successful send recorded by the verifier. External sending and client receipt or viewing are not established.',
    on_time='First recorded successful send by the end of the following report day in America/Chicago. Missing or invalid timestamps make the overall percentage unknown.',
    turnaround='Median hours from the earliest original upload to first recorded successful send, only when both timestamps and their order are known.',
    client_outcomes='Unknown. Saved checks are mutable and do not contain immutable at-send finding snapshots or evidence of what a client saw.',
    coverage='Coverage diagnostics describe all authorized saved reports before client/job filters. They are not denominators for the selected client or job. Reports outside the date range are counted separately.',
)


def category(issue):
    if issue.get('source') == 'AI review':
        return 'Found by AI review'
    for name, pattern in CATEGORIES:
        if pattern.search(issue.get('code') or ''):
            return name
    return 'Other checks'


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(timezone.utc) if dt.tzinfo and dt.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def central_day(value):
    from zoneinfo import ZoneInfo
    dt = timestamp(value)
    return dt.astimezone(ZoneInfo('America/Chicago')).date() if dt else None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        if not n.is_finite() or not 0 <= n <= 10**15:
            return None
        if n and n.normalize().as_tuple().exponent < -12:
            return None
        return n if Decimal(str(float(n))) == n else None
    except (TypeError, ValueError, OverflowError, InvalidOperation):
        return None


def public_number(value):
    n = number(value)
    return (int(n) if n == n.to_integral_value() else float(n)) if n is not None else None


def export(value):
    """Public JSON contains ordinary finite numbers, never Decimal repr strings."""
    if isinstance(value, Decimal):
        return public_number(value)
    if isinstance(value, dict):
        return {k: export(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [export(v) for v in value]
    return value


def _sum(values):
    with localcontext() as context:
        context.prec = 40
        return sum((Decimal(str(v)) for v in values), Decimal(0))


def quantity(result, field):
    raw = (result.get('totals') or {}).get(field) or {}
    if not isinstance(raw, dict):
        return dict(value=None, known=None, complete=False, unit='unknown')
    # This is the existing saved quantity schema; pass/fail classifiers explicitly
    # override rejected/certified with unit='rows'. No other unit is inferred.
    unit = raw.get('unit') or 'parts'
    full = number(raw.get('total'))
    known = number(raw.get('known_sum')) if raw.get('known_sum') is not None else full
    complete = full is not None and not raw.get('missing_count') and not raw.get('numeric_range_exceeded')
    if full is not None and known is not None and full != known:
        complete = False
    # A row marked Fail is not a quantity of rejected parts.
    if unit != 'parts':
        return dict(value=None, known=None, complete=False, unit=unit)
    return dict(value=full if complete else None, known=known, complete=complete, unit='parts')


def _issues(review):
    return [i for i in review.get('issues') or [] if i.get('severity') != 'info']


def _received(result):
    return timestamp(result.get('received_at'))


def _source(result, client_of):
    job, day = identify(result.get('filename'), result)
    a = result.get('_statistics_assignment')
    if isinstance(a, dict):
        exact_list = bool(a.get('site_id') and a.get('list_id'))
        # Source-only report families use a registry source identity, not a site label.
        exact_family = a.get('status') == 'source' and bool(a.get('source')) and not a.get('list_id')
        if a.get('status') in ('manual', 'verified', 'matched', 'source') and not a.get('needs_person') and (exact_list or exact_family):
            job, day = a.get('job') or job, a.get('business_date') or day
            client = a.get('client_key') or '_none'
            # Prefer immutable IDs; a renamed site or source label must not split one source.
            source = json.dumps([str(x or '') for x in (a.get('site_id'), a.get('list_id'),
                                                       a.get('source') if not a.get('list_id') else '')], separators=(',', ':'))
            return job, day, client, source, False
        # Do not guess an ambiguous client from a job-number substring.
        return job, day, '_none', 'unresolved', True
    return job, day, (client_of(job) if job and client_of else None) or '_none', '', True


def collect(results, start, end, client_of=None):
    groups = defaultdict(list)
    seen = set()
    coverage = dict(excluded_identity=0, excluded_assignment=0, outside_period=0, duplicate_versions=0,
                    competing_original_groups=0, first_original_missing_reports=0, first_order_unknown_reports=0, incomplete_source=0,
                    conflicting_client_groups=0)
    for r in results:
        file_id = r.get('file_hash')
        if file_id and file_id in seen:
            coverage['duplicate_versions'] += 1
            continue
        if file_id:
            seen.add(file_id)
        job, day, client, source, unresolved = _source(r, client_of)
        if isinstance(r.get('_statistics_assignment'), dict) and unresolved:
            coverage['excluded_assignment'] += 1
            a = r['_statistics_assignment']
            coverage['incomplete_source'] += a.get('status') in ('manual', 'verified', 'matched', 'source') and not a.get('needs_person')
            continue
        try:
            valid_day = date.fromisoformat(day)
        except (ValueError, TypeError):
            valid_day = None
        if not isinstance(job, str) or not job.strip() or not valid_day:
            coverage['excluded_identity'] += 1
            continue
        if not start <= valid_day <= end:
            coverage['outside_period'] += 1
            continue
        groups[(job.strip().upper(), valid_day.isoformat(), source)].append((r, unresolved, client))
    records = []
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for (job, day, source), entries in sorted(groups.items()):
        clients = {entry[2] for entry in entries}
        if len(clients) != 1:
            coverage['conflicting_client_groups'] += 1
            continue
        client = next(iter(clients))
        uploads = [r for r, _, _ in entries]
        # Enrichment is outside the persisted approved state. Never invalidate a real
        # approval by inserting reporting metadata into its state-fingerprint input.
        reviews = {id(r): summarize({k: v for k, v in r.items() if k != '_statistics_assignment'}) for r in uploads}
        def rank(r):
            rv = reviews[id(r)]
            return (any(x.get('sent') is True for x in r.get('sent') or []),
                    rv.get('approval_current') is True, bool(r.get('fixed_from')),
                    _received(r) or epoch, str(r.get('file_hash') or ''))
        selected = max(uploads, key=rank)
        originals = [r for r in uploads if not r.get('fixed_from')]
        has_original = bool(originals)
        coverage['competing_original_groups'] += len(originals) > 1
        coverage['first_original_missing_reports'] += not originals
        originals = originals or uploads
        first_order_known = all(_received(r) for r in originals)
        coverage['first_order_unknown_reports'] += not first_order_known
        first = min(originals, key=lambda r: (_received(r) is None, _received(r) or epoch, str(r.get('file_hash') or '')))
        first_review, current = reviews[id(first)], reviews[id(selected)]
        caught, remaining = _issues(first_review), _issues(current)
        successful = [x for r in uploads for x in r.get('sent') or [] if x.get('sent') is True]
        dates = [timestamp(x.get('at')) for x in successful]
        times_complete = bool(dates) and all(dates)
        send_time = min(dates) if times_complete else None
        received = _received(first) if all(_received(r) for r in originals) else None
        timing_valid = bool(has_original and first_order_known and send_time and received and send_time >= received)
        sent_day = central_day(send_time.isoformat()) if send_time and (received is None or send_time >= received) else None
        report_day = date.fromisoformat(day)
        on_time = (sent_day <= report_day + timedelta(days=1)) if sent_day and sent_day >= report_day else None
        corrections = [r for r in uploads if isinstance(r.get('fixed_from'), dict) and r['fixed_from']]
        q = {f: quantity(selected, f) for f in QUANTITIES}
        a = selected.get('_statistics_assignment') or {}
        source_label = ' · '.join(str(x) for x in (a.get('client'), a.get('site') or a.get('source')) if x)
        # Short immutable suffix distinguishes same-named lists without dumping raw IDs.
        if a.get('list_id'):
            source_label = (source_label + ' · ' if source_label else '') + 'Source ' + str(a.get('site_id') or '')[-8:] + '/' + str(a['list_id'])[-8:]
        records.append(dict(job=job, day=day, client=client, source=source, source_label=source_label or 'Source not independently resolved',
            first_file_hash=first.get('file_hash'), selected_file_hash=selected.get('file_hash'),
            uploads=len(uploads), corrected=bool(corrections), competing_originals=len([r for r in uploads if not r.get('fixed_from')]) > 1,
            changes=sum(len(r['fixed_from'].get('changes') or []) for r in corrections),
            quantities=q, **{f: q[f]['value'] for f in QUANTITIES},
            caught=len(caught), must_fix=sum(i.get('severity') == 'high' for i in caught),
            current_issues=len(remaining), current_must_fix=sum(i.get('severity') == 'high' for i in remaining),
            remaining=len(remaining), remaining_must=sum(i.get('severity') == 'high' for i in remaining),
            categories=dict(Counter(category(i) for i in caught)),
            caught_complete=bool(has_original and first_order_known) and not (first_review.get('coverage') or {}).get('truncated', False),
            first_order_known=bool(first_order_known),
            current_issues_complete=not (current.get('coverage') or {}).get('truncated', False),
            sent=bool(successful), on_time=on_time, hours=(send_time - received).total_seconds()/3600 if timing_valid else None,
            hold=not successful and current.get('status') == 'hold',
            unmapped=any(x for _, x, _ in entries), client_outcomes_known=False))
    return records, coverage


def totals(records):
    t = dict(reports=len(records), jobs=len({(r['job'], r.get('source', '')) for r in records}),
             clients=len({r['client'] for r in records if r['client'] != '_none'}))
    for f in ('uploads', 'corrected', 'changes', 'caught', 'must_fix', 'current_issues', 'current_must_fix', 'sent', 'hold'):
        t[f] = sum(r.get(f, 0) for r in records)
    t.update(open=t['hold'], unsent=t['reports']-t['sent'],
             waiting=sum(r.get('remaining', 0) for r in records if not r['sent']),
             waiting_must=sum(r.get('remaining_must', 0) for r in records if not r['sent']),
             unmapped_reports=sum(r.get('unmapped', False) for r in records),
             caught_complete=all(r.get('caught_complete', True) for r in records),
             current_issues_complete=all(r.get('current_issues_complete', True) for r in records))
    for f in QUANTITIES:
        quantities = [r.get('quantities', {}).get(f, dict(value=r.get(f), known=r.get(f), complete=r.get(f) is not None, unit='parts')) for r in records]
        complete = sum(q['complete'] for q in quantities)
        values = [q['value'] for q in quantities if q['complete']]
        known = [q['known'] for q in quantities if q['known'] is not None]
        total, subtotal = (_sum(values) if values else None), (_sum(known) if known else None)
        t[f] = public_number(total) if records and complete == len(records) else None
        t[f+'_known'] = public_number(subtotal) if known else None
        t[f+'_range_exceeded'] = any(x is not None and number(x) is None for x in (total, subtotal))
        t[f+'_complete_reports'] = complete
        t[f+'_missing_reports'] = len(records)-complete
        t[f+'_incompatible_reports'] = sum(q['unit'] not in ('parts', 'unknown') for q in quantities)
    invalid_ratio = any(r.get('inspected') is not None and r.get('rejected') is not None and r['rejected'] > r['inspected'] for r in records)
    t['rate'] = public_number(round(Decimal(str(t['rejected']))/Decimal(str(t['inspected']))*100, 2)) if t['inspected'] and t['rejected'] is not None and not invalid_ratio else None
    t['rate_invalid_reports'] = sum(r.get('inspected') is not None and r.get('rejected') is not None and r['rejected'] > r['inspected'] for r in records)
    t['on_time'] = sum(r.get('on_time') is True for r in records if r['sent'])
    t['on_time_unknown'] = sum(r.get('on_time') is None for r in records if r['sent'])
    t['on_time_pct'] = round(t['on_time']/t['sent']*100, 1) if t['sent'] and not t['on_time_unknown'] else None
    hours = [r['hours'] for r in records if r.get('hours') is not None]
    t['turnaround'] = round(median(hours), 1) if hours else None
    t['turnaround_reports'] = len(hours)
    # Historical mutable checks cannot establish which findings were present at send time.
    t.update(stopped=None, stopped_must=None, reached=None, reached_must=None, client_outcomes_known=False)
    return t


def categories(records, label='kind'):
    counter = Counter()
    for r in records:
        counter.update(r.get('categories') or {})
    return [{label: k, 'count': v} for k, v in sorted(counter.items(), key=lambda p: (-p[1], p[0]))]


def metadata(coverage):
    return dict(version=1, timezone='America/Chicago', definitions=DEFINITIONS, coverage=coverage,
                coverage_scope='All authorized saved reports, before client/job filters; not the selected scope denominator.',
                source='Saved verifier reports and review records. Quantity completeness is not source verification.')
