"""A one-page dashboard for an end-of-shift report: built from the report's own numbers (no AI), then
changed on request by Claude. Self-contained HTML (inline styles and SVG, no network), safe to send to a
client: no inspector names, scan times or internal check results.
"""
import html
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from hosting.review_summary import summarize


def _n(v):
    try:
        return float(v) if v is not None and v != '' else None
    except (TypeError, ValueError):
        return None


def _fmt(v, digits=0):
    if v is None:
        return '—'
    return f'{v:,.{digits}f}' if digits or v != int(v) else f'{int(v):,}'


def _pct(a, b):
    return (a / b * 100) if a is not None and b else None


def _day_label(day):
    try:
        d = date.fromisoformat(str(day)[:10])
        return d.strftime('%A, %B ') + str(d.day) + d.strftime(', %Y')
    except ValueError:
        return day or ''


def data_for(item, raw):
    """Everything the dashboard shows, as plain numbers. Claude edits the page from exactly this."""
    from hosting.spreadsheets import report_records
    from hosting.shifts import sheet_shift
    from hosting.adaptive_reports import shift_number
    rv = summarize(item, with_issues=False)
    per_row = rv.get('rejected_unit') == 'rows'
    recs = [r for r in report_records(item['filename'], raw) if r.get('table_kind') == 'detail']
    from hosting.row_scope import saved_detail_count
    saved_rows = saved_detail_count(item)
    if not recs:
        saved_values = [_n(rv.get(k)) for k in ('scans', 'inspected', 'rejected')]
        if all(v == 0 for v in saved_values):
            raise ValueError('No row analytics: the saved summary explicitly records zero inspection rows and quantities. View the saved report summary instead; nothing was changed.')
        if any(v is not None and v != 0 for v in saved_values):
            raise ValueError('Row analytics are unavailable: the saved summary has counts, but no detail rows could be recognized locally. Use recognized column headers or a supported template for row analytics; the saved checks and original file are unchanged.')
        raise ValueError('Row analytics are unavailable: no detail rows could be recognized locally and the quantities are unknown. Use recognized column headers or a supported template; missing quantities were not treated as zero.')
    if saved_rows is not None and saved_rows != len(recs):
        raise ValueError('Row analytics are unavailable: the locally recognized rows do not match the saved report count. Review the report layout before rebuilding this dashboard; nothing was changed.')
    rows = []
    for r in recs:
        lab, val = r.get('labels') or {}, r.get('values') or {}
        shift = shift_number(lab.get('shift')) or sheet_shift(r.get('sheet'))
        rej = _n(val.get('rejected'))
        cond = str(lab.get('condition') or '').strip()
        failed = (rej or 0) > 0 or cond.upper() in ('NOK', 'FAIL', 'FAILED', 'SUSPECT', 'REJECT', 'REJECTED')
        rows.append(dict(identifier=str(lab.get('identifier') or ''), part=str(lab.get('part') or ''), shift=shift,
                         defect=str(lab.get('defect') or '').strip(), condition=cond,
                         inspected=_n(val.get('inspected')), certified=_n(val.get('certified')), rejected=rej, failed=failed))
    def total(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return sum(vals) if vals else None
    inspected, rejected, certified = total('inspected'), total('rejected'), total('certified')
    if per_row:
        rejected = float(sum(1 for r in rows if r['failed']))
    if inspected is None:
        inspected = _n(rv.get('inspected'))
    if rejected is None:
        rejected = _n(rv.get('rejected'))
    if certified is None and inspected is not None and rejected is not None and not per_row:
        certified = inspected - rejected
    base = len(rows) if per_row else inspected
    shifts = defaultdict(lambda: dict(rows=0, inspected=0.0, rejected=0.0))
    for r in rows:
        s = shifts[r['shift'] or '—']
        s['rows'] += 1
        s['inspected'] += r['inspected'] or 0
        s['rejected'] += (1 if r['failed'] else 0) if per_row else (r['rejected'] or 0)
    reasons = Counter()
    for r in rows:
        if r['failed']:
            reason = ' '.join((r['defect'] or 'No reason given').split())
            reasons[reason.title() if reason.islower() else reason] += 1 if per_row else (r['rejected'] or 1)
    parts = defaultdict(lambda: dict(rows=0, inspected=0.0, rejected=0.0))
    for r in rows:
        p = parts[r['part'] or 'Not given']
        p['rows'] += 1
        p['inspected'] += r['inspected'] or 0
        p['rejected'] += (1 if r['failed'] else 0) if per_row else (r['rejected'] or 0)
    bl = item.get('baseline') or {}
    trend = []
    for h in sorted(bl.get('history') or [], key=lambda h: h.get('day') or ''):
        trend.append(dict(day=h.get('day'), scans=_n(str(h.get('scans') or '').replace(',', '')),
                          inspected=_n(str(h.get('inspected') or '').replace(',', '')), rejected=_n(str(h.get('rejected') or '').replace(',', ''))))
    trend.append(dict(day=rv.get('day'), scans=float(len(rows)) if rows else _n(rv.get('scans')), inspected=inspected, rejected=rejected, today=True))
    usual = {k: (v or {}).get('median') for k, v in (bl.get('usual') or {}).items()}
    return dict(
        job=rv.get('job') or item['filename'], day=rv.get('day'), day_label=_day_label(rv.get('day')),
        filename=item['filename'], version=item.get('version'), pass_fail=per_row,
        totals=dict(rows=len(rows) or _n(rv.get('scans')), inspected=inspected, certified=certified, rejected=rejected,
                    rate=_pct(rejected, base)),
        shifts=[dict(shift=k, **v) for k, v in sorted(shifts.items())],
        reasons=[dict(reason=k, count=v) for k, v in reasons.most_common(12)],
        parts=[dict(part=k, **v) for k, v in sorted(parts.items(), key=lambda kv: -kv[1]['inspected'])[:25]],
        rejects=[{k: r[k] for k in ('identifier', 'part', 'shift', 'defect', 'rejected')} for r in rows if r['failed']][:300],
        trend=trend[-15:], usual=usual,
        generated=datetime.now(timezone.utc).strftime('%Y-%m-%d'))


# ---------------------------------------------------------------- the page
CSS = """
:root{--ink:#122b29;--muted:#55706b;--line:#d9e3de;--paper:#eef2ef;--card:#fff;--deep:#143c3b;--green:#20594d;--mint:#9fd3b8;--rej:#c0392b;--rej-soft:#f6d5cf;--amber:#d68910}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.band{background:var(--deep);color:#eef7f3;padding:30px 40px 82px;position:relative;overflow:hidden}
.band:after{content:"";position:absolute;right:-60px;top:-80px;width:340px;height:340px;border-radius:50%;border:44px solid rgba(159,211,184,.09)}
.band .brand{font-weight:700;letter-spacing:.5px;color:var(--mint);font-size:13px}
.band h1{font-family:Georgia,"Times New Roman",serif;font-weight:400;font-size:44px;line-height:1.1;margin:10px 0 6px}
.band p{margin:0;color:#bcd6cd}
.wrap{max-width:1180px;margin:-58px auto 0;padding:0 24px 40px;position:relative}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:14px}
.kpi{background:var(--card);border-radius:12px;padding:16px 18px;box-shadow:0 1px 0 var(--line),0 10px 24px rgba(20,60,59,.08)}
.kpi b{display:block;font-family:Georgia,"Times New Roman",serif;font-weight:400;font-size:36px;line-height:1.1;font-variant-numeric:tabular-nums}
.kpi span{font-size:13px;color:var(--muted)}
.kpi em{display:block;font-style:normal;font-size:12px;color:var(--muted);margin-top:4px}
.kpi.rej b{color:var(--rej)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.card.wide{grid-column:1/-1}
.card h2{font-size:16px;margin:0 0 2px}
.card .sub{font-size:13px;color:var(--muted);margin:0 0 14px}
svg text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
table{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
th{text-align:left;font-size:12px;color:var(--muted);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid #edf1ee}
td.num,th.num{text-align:right}
.bar{height:8px;border-radius:4px;background:#e3ece7;overflow:hidden;min-width:80px}
.bar i{display:block;height:100%;background:var(--green)}
.bar i.r{background:var(--rej)}
.scroll{max-height:420px;overflow:auto}
.empty{color:var(--muted);font-size:14px;margin:6px 0}
footer{color:var(--muted);font-size:12px;margin-top:22px;text-align:center}
@media (max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.band{padding:24px 20px 76px}.band h1{font-size:34px}}
@media print{body{background:#fff}.band{-webkit-print-color-adjust:exact;print-color-adjust:exact}.card,.kpi{break-inside:avoid;box-shadow:none}.scroll{max-height:none;overflow:visible}}
"""


def _ordinal(n):
    return f'{n}{ {"1": "st", "2": "nd", "3": "rd"}.get(str(n), "th") }' if n and str(n).isdigit() else (n or '')


def _e(v):
    return html.escape(str(v if v is not None else ''))


def _hbars(items, label, value, color='var(--green)', unit=''):
    if not items:
        return '<p class="empty">Nothing to show.</p>'
    top = max(value(i) for i in items) or 1
    h, out = 30, []
    for n, i in enumerate(items):
        w = max(2, value(i) / top * 250)
        y = n * h
        out.append(f'<text x="0" y="{y + 20}" font-size="14" fill="#122b29">{_e(label(i))[:24]}</text>'
                   f'<rect x="180" y="{y + 7}" width="{w:.1f}" height="18" rx="4" fill="{color}"/>'
                   f'<text x="{188 + w:.1f}" y="{y + 21}" font-size="14" fill="#55706b">{_fmt(value(i))}{unit}</text>')
    return f'<svg viewBox="0 0 500 {len(items) * h}" width="100%" role="img" aria-label="Bar chart">{"".join(out)}</svg>'


def _shift_bars(shifts, pass_fail):
    if not shifts:
        return '<p class="empty">No shift information in this report.</p>'
    top = max(s['inspected'] if not pass_fail else s['rows'] for s in shifts) or 1
    h, out = 44, []
    for n, s in enumerate(shifts):
        whole = s['rows'] if pass_fail else s['inspected']
        bad = s['rejected']
        w = max(2, whole / top * 230)
        wb = min(w, (bad / whole * w) if whole else 0)
        y = n * h
        name = f'{s["shift"]}{ {"1": "st", "2": "nd", "3": "rd"}.get(s["shift"], "") } shift' if s['shift'] != '—' else 'No shift'
        rate = _pct(bad, whole)
        out.append(f'<text x="0" y="{y + 25}" font-size="15" font-weight="600" fill="#122b29">{_e(name)}</text>'
                   f'<rect x="88" y="{y + 10}" width="{w:.1f}" height="22" rx="5" fill="#20594d"/>'
                   f'<rect x="{88 + w - wb:.1f}" y="{y + 10}" width="{wb:.1f}" height="22" rx="0" fill="#c0392b"/>'
                   f'<text x="{96 + w:.1f}" y="{y + 26}" font-size="14" fill="#55706b">{_fmt(whole)} · {_fmt(bad)} {"failed" if pass_fail else "rejected"}'
                   f'{f" ({rate:.1f}%)" if rate is not None else ""}</text>')
    return f'<svg viewBox="0 0 500 {len(shifts) * h}" width="100%" role="img" aria-label="Shifts">{"".join(out)}</svg>'


def _trend(trend, usual, pass_fail):
    pts = [t for t in trend if t.get('inspected') is not None or t.get('scans') is not None]
    if len(pts) < 2:
        return '<p class="empty">Earlier days appear here once the job has a few days of history.</p>'
    key = 'scans' if pass_fail or all(t.get('inspected') is None for t in pts) else 'inspected'
    vals = [t.get(key) or 0 for t in pts]
    top = max(vals) or 1
    W, H, pad = 1060, 250, 34
    bw = (W - pad * 2) / len(pts)
    out = []
    try:
        um = float(str(usual.get(key) or '').replace(',', ''))
        uy = H - pad - um / top * (H - pad * 2)
        out.append(f'<line x1="{pad}" x2="{W - pad}" y1="{uy:.1f}" y2="{uy:.1f}" stroke="#d68910" stroke-dasharray="5 5"/>'
                   f'<text x="{pad}" y="{uy - 6:.1f}" font-size="13" fill="#a86a07">usual {_fmt(um)}</text>')
    except ValueError:
        pass
    for n, (t, v) in enumerate(zip(pts, vals)):
        h = v / top * (H - pad * 2)
        x = pad + n * bw + bw * .18
        color = '#143c3b' if t.get('today') else '#9fc9b5'
        out.append(f'<rect x="{x:.1f}" y="{H - pad - h:.1f}" width="{bw * .64:.1f}" height="{h:.1f}" rx="4" fill="{color}"/>')
        if t.get('today'):
            out.append(f'<text x="{x + bw * .32:.1f}" y="{H - pad - h + 18:.1f}" font-size="13" font-weight="700" text-anchor="middle" fill="#ffffff">{_fmt(v)}</text>')
        d = str(t.get('day') or '')[5:].replace('-', '/')
        out.append(f'<text x="{x + bw * .32:.1f}" y="{H - 12}" font-size="13" text-anchor="middle" fill="#55706b">{_e(d.lstrip("0"))}</text>')
    label = 'Scans' if key == 'scans' else 'Parts inspected'
    return f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="{label} by day">{"".join(out)}</svg>'


def build(d):
    t, pf = d['totals'], d['pass_fail']
    rej_word = 'Failed audits' if pf else 'Rejected'
    usual = d.get('usual') or {}
    rate_note = f'usual {usual["reject_rate"]}' if usual.get('reject_rate') else ''
    kpis = [('Cars / rows', _fmt(t['rows']), f'usual {usual["scans"]}' if usual.get('scans') else '', ''),
            ('Parts inspected', _fmt(t['inspected']), f'usual {usual["inspected"]}' if usual.get('inspected') else '', ''),
            ('Certified', _fmt(t['certified']), '', ''),
            (rej_word, _fmt(t['rejected']), '', 'rej'),
            ('Fail rate' if pf else 'Reject rate', '—' if t['rate'] is None else f'{t["rate"]:.1f}%', rate_note, 'rej')]
    kpi_html = ''.join(f'<div class="kpi {c}"><b>{v}</b><span>{_e(k)}</span>{f"<em>{_e(n)}</em>" if n else ""}</div>' for k, v, n, c in kpis)
    parts = d['parts']
    ptop = max([p['inspected'] for p in parts] + [1])
    part_rows = ''.join(
        f'<tr><td>{_e(p["part"])}</td><td class="num">{_fmt(p["rows"])}</td><td class="num">{_fmt(p["inspected"])}</td>'
        f'<td class="num">{_fmt(p["rejected"])}</td><td><div class="bar"><i style="width:{p["inspected"] / ptop * 100:.0f}%"></i></div></td></tr>' for p in parts)
    reject_rows = ''.join(
        f'<tr><td>{_e(r["identifier"])}</td><td>{_e(r["part"])}</td><td>{_e(_ordinal(r["shift"]))}</td><td>{_e(r["defect"] or "—")}</td>'
        f'<td class="num">{_fmt(r["rejected"]) if r["rejected"] is not None else "—"}</td></tr>' for r in d['rejects'])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(d['job'])}: End of Shift Dashboard, {_e(d['day'] or '')}</title><style>{CSS}</style></head>
<body>
<header class="band"><div class="brand">PPG QUALITY</div><h1>{_e(d['job'])}</h1><p>End of shift dashboard · {_e(d['day_label'])}</p></header>
<main class="wrap">
<section class="kpis">{kpi_html}</section>
<section class="grid">
<div class="card wide"><h2>Recent days</h2><p class="sub">{'Scans' if pf else 'Parts inspected'} per day; today in dark green.</p>{_trend(d['trend'], usual, pf)}</div>
<div class="card"><h2>By shift</h2><p class="sub">Green: {'audits' if pf else 'parts inspected'}; red: {'failed' if pf else 'rejected'}.</p>{_shift_bars(d['shifts'], pf)}</div>
<div class="card"><h2>{'Failure' if pf else 'Reject'} reasons</h2><p class="sub">{'Failed audits' if pf else 'Rejected parts'} by reason.</p>{_hbars(d['reasons'], lambda i: i['reason'], lambda i: i['count'], '#c0392b')}</div>
<div class="card wide"><h2>By part</h2><p class="sub">{len(parts)} part number{'s' if len(parts) != 1 else ''}.</p>
<div class="scroll"><table><thead><tr><th>Part</th><th class="num">Rows</th><th class="num">Inspected</th><th class="num">{rej_word}</th><th></th></tr></thead><tbody>{part_rows or '<tr><td colspan="5" class="empty">No part numbers in this report.</td></tr>'}</tbody></table></div></div>
<div class="card wide"><h2>{rej_word} units</h2><p class="sub">{len(d['rejects'])} unit{'s' if len(d['rejects']) != 1 else ''} with {'a failed audit' if pf else 'rejects'}.</p>
<div class="scroll"><table><thead><tr><th>VIN / identifier</th><th>Part</th><th>Shift</th><th>Reason</th><th class="num">{'Qty' if not pf else ''}</th></tr></thead><tbody>{reject_rows or '<tr><td colspan="5" class="empty">None. Every unit passed.</td></tr>'}</tbody></table></div></div>
</section>
<footer>Prepared by PPG Quality from {_e(d['filename'])} (version {_e(d['version'])}) on {_e(d['generated'])}.</footer>
</main></body></html>"""


def file_name(item):
    import re
    rv = summarize(item, with_issues=False)
    base = f'{rv.get("job") or "Report"} Dashboard {rv.get("day") or ""}'.strip()
    return re.sub(r'[^A-Za-z0-9 ._()-]', '_', base)[:150] + '.html'


# ---------------------------------------------------------------- weekly / monthly
def _period_word(d):
    return 'Weekly' if d['kind'] == 'week' else 'Monthly'


def _bars_plain(days, key, H, W):
    vals = [(x['day'], x.get(key)) for x in days]
    top = max([v or 0 for _, v in vals] + [1])
    pad, out = 30, []
    bw = (W - pad * 2) / max(len(vals), 1)
    for n, (d, v) in enumerate(vals):
        x = pad + n * bw + bw * .15
        if v:
            h = v / top * (H - pad * 2)
            out.append(f'<rect x="{x:.1f}" y="{H - pad - h:.1f}" width="{bw * .7:.1f}" height="{h:.1f}" rx="3" fill="#20594d"/>')
            if len(vals) <= 10:
                out.append(f'<text x="{x + bw * .35:.1f}" y="{H - pad - h - 6:.1f}" font-size="13" text-anchor="middle" fill="#122b29">{_fmt(v)}</text>')
        if len(vals) <= 10 or n % 2 == 0:
            dd = date.fromisoformat(d)
            lab = dd.strftime('%a ') + str(dd.day) if len(vals) <= 10 else str(dd.day)
            out.append(f'<text x="{x + bw * .35:.1f}" y="{H - 8}" font-size="12" text-anchor="middle" fill="#55706b">{_e(lab)}</text>')
    label = 'Scans' if key == 'scans' else 'Parts inspected'
    return f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="{label} by day">{"".join(out)}</svg>'


def _rate(x):
    return '—' if not x.get('inspected') or x.get('rejected') is None else f'{x["rejected"] / x["inspected"] * 100:.1f}%'


def _kpis_period(d):
    t, pf = d['totals'], d['pass_fail']
    return [('Days worked', _fmt(t['days_worked']), ''), ('Cars / rows', _fmt(t['scans']), ''), ('Parts inspected', _fmt(t['inspected']), ''),
            ('Failed audits' if pf else 'Rejected', _fmt(t['rejected']), 'rej'),
            ('Fail rate' if pf else 'Reject rate', '—' if t['rate'] is None else f'{t["rate"]:.1f}%', 'rej')]


def build_period(d):
    pf = d['pass_fail']
    rej_word = 'Failed audits' if pf else 'Rejected'
    kpi_html = ''.join(f'<div class="kpi {c}"><b>{v}</b><span>{_e(k)}</span></div>' for k, v, c in _kpis_period(d))
    day_rows = ''.join(
        f'<tr><td>{_e(date.fromisoformat(x["day"]).strftime("%a %b ") + str(date.fromisoformat(x["day"]).day))}</td><td class="num">{_fmt(x["scans"])}</td>'
        f'<td class="num">{_fmt(x["inspected"])}</td><td class="num">{_fmt(x["rejected"])}</td>'
        f'<td class="num">{_rate(x)}</td></tr>'
        for x in d['days'] if x['scans'] or x['inspected'])
    parts = d['parts']
    ptop = max([p['inspected'] for p in parts] + [1])
    part_rows = ''.join(f'<tr><td>{_e(p["part"])}</td><td class="num">{_fmt(p["rows"])}</td><td class="num">{_fmt(p["inspected"])}</td>'
                        f'<td class="num">{_fmt(p["rejected"])}</td><td><div class="bar"><i style="width:{p["inspected"] / ptop * 100:.0f}%"></i></div></td></tr>' for p in parts)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(d['job'])}: {_period_word(d)} Dashboard, {_e(d['label'])}</title><style>{CSS}</style></head>
<body>
<header class="band"><div class="brand">PPG QUALITY</div><h1>{_e(d['job'])}</h1><p>{_period_word(d)} dashboard · {_e(d['label'])}</p></header>
<main class="wrap">
<section class="kpis">{kpi_html}</section>
<section class="grid">
<div class="card wide"><h2>Day by day</h2><p class="sub">{'Scans' if pf else 'Parts inspected'} each day.</p>{_bars_plain(d['days'], 'scans' if pf else 'inspected', 250, 1060)}</div>
<div class="card"><h2>By shift</h2><p class="sub">Green: {'audits' if pf else 'parts inspected'}; red: {'failed' if pf else 'rejected'}.</p>{_shift_bars(d['shifts'], pf)}</div>
<div class="card"><h2>{'Failure' if pf else 'Reject'} reasons</h2><p class="sub">Across the {d['kind']}.</p>{_hbars(d['reasons'], lambda i: i['reason'], lambda i: i['count'], '#c0392b')}</div>
<div class="card wide"><h2>Each day</h2><div class="scroll"><table><thead><tr><th>Day</th><th class="num">Cars / rows</th><th class="num">Inspected</th><th class="num">{rej_word}</th><th class="num">Rate</th></tr></thead><tbody>{day_rows or '<tr><td colspan="5" class="empty">No days with data yet.</td></tr>'}</tbody></table></div></div>
<div class="card wide"><h2>By part</h2><div class="scroll"><table><thead><tr><th>Part</th><th class="num">Rows</th><th class="num">Inspected</th><th class="num">{rej_word}</th><th></th></tr></thead><tbody>{part_rows or '<tr><td colspan="5" class="empty">No part numbers.</td></tr>'}</tbody></table></div></div>
</section>
<footer>Prepared by PPG Quality from the daily end-of-shift reports and SharePoint scans, {_e(d['start'])} to {_e(d['end'])}, on {_e(d['generated'])}.</footer>
</main></body></html>"""


SLIDE_CSS = """
:root{--ink:#122b29;--muted:#55706b;--deep:#143c3b;--green:#20594d;--mint:#9fd3b8;--rej:#c0392b;--paper:#eef2ef}
*{box-sizing:border-box}html,body{margin:0;background:#0d2423;font:18px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--ink)}
.deck{display:flex;flex-direction:column;align-items:center;gap:24px;padding:24px}
.slide{width:min(1280px,96vw);aspect-ratio:16/9;background:#fff;border-radius:10px;position:relative;overflow:hidden;padding:5.2% 6%;display:flex;flex-direction:column}
.slide h2{font-family:Georgia,"Times New Roman",serif;font-weight:400;font-size:clamp(22px,3.2vw,44px);margin:0 0 .3em;color:var(--deep)}
.slide .lede{color:var(--muted);margin:0 0 1.2em;font-size:clamp(13px,1.4vw,20px)}
.slide .body{flex:1;min-height:0;display:flex;flex-direction:column;justify-content:center}
.slide svg{max-height:100%}
.slide .foot{position:absolute;left:6%;right:6%;bottom:3.2%;display:flex;justify-content:space-between;font-size:clamp(10px,1vw,13px);color:var(--muted)}
.title{background:var(--deep);color:#eef7f3;justify-content:flex-end}
.title:after{content:"";position:absolute;right:-8%;top:-20%;width:45%;aspect-ratio:1;border-radius:50%;border:clamp(30px,5vw,70px) solid rgba(159,211,184,.09)}
.title .brand{color:var(--mint);font-weight:700;letter-spacing:.06em;font-size:clamp(12px,1.2vw,16px)}
.title h1{font-family:Georgia,"Times New Roman",serif;font-weight:400;font-size:clamp(34px,6vw,84px);line-height:1.05;margin:.2em 0 .15em}
.title p{margin:0;color:#bcd6cd;font-size:clamp(14px,1.8vw,26px)}
.title .foot{color:#8fb5a9}
.tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:2%}
.tile{background:var(--paper);border-radius:10px;padding:8% 9%}
.tile b{display:block;font-family:Georgia,"Times New Roman",serif;font-weight:400;font-size:clamp(22px,3.6vw,52px);line-height:1.1}
.tile span{color:var(--muted);font-size:clamp(11px,1.2vw,16px)}
.tile.rej b{color:var(--rej)}
table{width:100%;border-collapse:collapse;font-size:clamp(11px,1.25vw,17px);font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--muted);font-weight:600;padding:.4em .5em;border-bottom:1px solid #d9e3de}
td{padding:.45em .5em;border-bottom:1px solid #edf1ee}.num{text-align:right}
.nav{position:fixed;right:18px;bottom:16px;display:none;gap:8px;align-items:center;color:#cfe3dc;font-size:14px}
.nav button{background:#20594d;color:#fff;border:0;border-radius:6px;padding:8px 14px;font-size:14px;cursor:pointer}
body.present .deck{height:100vh;justify-content:center;padding:0}
body.present .slide{display:none;width:min(96vw,calc(96vh*16/9))}
body.present .slide.on{display:flex}
body.present .nav{display:flex}
@media print{@page{size:13.333in 7.5in;margin:0}html,body{background:#fff}.deck{padding:0;gap:0}.slide{width:13.333in;height:7.5in;border-radius:0;break-after:page;display:flex!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}.nav{display:none!important}}
"""

SLIDE_JS = """
(function(){var s=[].slice.call(document.querySelectorAll('.slide')),i=0,n=document.getElementById('n');
function go(k){i=Math.max(0,Math.min(s.length-1,k));s.forEach(function(e,j){e.classList.toggle('on',j===i)});if(n)n.textContent=(i+1)+' / '+s.length}
document.body.classList.add('present');go(0);
document.addEventListener('keydown',function(e){if(e.key==='ArrowRight'||e.key===' '||e.key==='PageDown'){go(i+1);e.preventDefault()}if(e.key==='ArrowLeft'||e.key==='PageUp'){go(i-1);e.preventDefault()}});
document.getElementById('prev').onclick=function(){go(i-1)};document.getElementById('next').onclick=function(){go(i+1)};
document.getElementById('all').onclick=function(){document.body.classList.toggle('present')};
document.querySelector('.deck').addEventListener('click',function(e){if(document.body.classList.contains('present')&&!e.target.closest('button'))go(i+1)})})();
"""


def build_slides(d):
    pf = d['pass_fail']
    rej_word = 'Failed audits' if pf else 'Rejected'
    word = _period_word(d)
    foot = f'<div class="foot"><span>PPG Quality · {_e(d["job"])}</span><span>{_e(d["label"])}</span></div>'
    tiles = ''.join(f'<div class="tile {c}"><b>{v}</b><span>{_e(k)}</span></div>' for k, v, c in _kpis_period(d))
    top_parts = ''.join(f'<tr><td>{_e(p["part"])}</td><td class="num">{_fmt(p["inspected"])}</td><td class="num">{_fmt(p["rejected"])}</td>'
                        f'<td class="num">{_rate(p)}</td></tr>'
                        for p in d['parts'][:8])
    t = d['totals']
    best = max((x for x in d['days'] if x['inspected']), key=lambda x: x['inspected'], default=None)
    lede = (f'{_fmt(t["inspected"])} parts inspected over {_fmt(t["days_worked"])} days'
            + (f', {t["rate"]:.1f}% {"failed" if pf else "rejected"}' if t['rate'] is not None else '') + '.')
    slides = [
        f'<section class="slide title"><div class="brand">PPG QUALITY</div><h1>{_e(d["job"])}</h1><p>{word} inspection report · {_e(d["label"])}</p>{foot}</section>',
        f'<section class="slide"><h2>At a glance</h2><p class="lede">{_e(lede)}</p><div class="body"><div class="tiles">{tiles}</div></div>{foot}</section>',
        f'<section class="slide"><h2>Day by day</h2><p class="lede">{"Scans" if pf else "Parts inspected"} each day'
        + (f'; busiest day {date.fromisoformat(best["day"]).strftime("%A, %B ")}{date.fromisoformat(best["day"]).day} with {_fmt(best["inspected"])}.' if best else '.')
        + f'</p><div class="body">{_bars_plain(d["days"], "scans" if pf else "inspected", 330, 1060)}</div>{foot}</section>',
        f'<section class="slide"><h2>By shift</h2><p class="lede">Green: {"audits" if pf else "parts inspected"}; red: {"failed" if pf else "rejected"}.</p><div class="body">{_shift_bars(d["shifts"], pf)}</div>{foot}</section>',
        f'<section class="slide"><h2>{"Failure" if pf else "Reject"} reasons</h2><p class="lede">What the {"failed audits" if pf else "rejects"} were for, across the {d["kind"]}.</p><div class="body">{_hbars(d["reasons"][:8], lambda i: i["reason"], lambda i: i["count"], "#c0392b")}</div>{foot}</section>',
        f'<section class="slide"><h2>By part</h2><p class="lede">Top part numbers by parts inspected.</p><div class="body"><table><thead><tr><th>Part</th><th class="num">Inspected</th><th class="num">{rej_word}</th><th class="num">Rate</th></tr></thead><tbody>{top_parts or "<tr><td colspan=4>No part numbers.</td></tr>"}</tbody></table></div>{foot}</section>',
        f'<section class="slide title"><div class="brand">PPG QUALITY</div><h1>Thank you</h1><p>Prepared from the daily end-of-shift reports, {_e(d["start"])} to {_e(d["end"])}.</p>{foot}</section>',
    ]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(d['job'])}: {word} Report, {_e(d['label'])}</title><style>{SLIDE_CSS}</style></head>
<body><main class="deck">{''.join(slides)}</main>
<div class="nav"><button id="prev" type="button">‹</button><span id="n"></span><button id="next" type="button">›</button><button id="all" type="button">All slides</button></div>
<script>{SLIDE_JS}</script></body></html>"""


# ---------------------------------------------------------------- executive overview (all jobs)
def _ov_number(value):
    return 'Unknown' if value is None else format(Decimal(str(value)), ',f')


def _pctf(v, digits=1):
    return '—' if v is None else f'{v:.{digits}f}%'


def _ov_kpis(o):
    t = o['totals']
    return [('First-original findings (current review)', _fmt(t['caught']), 'rej'), ('Selected daily reports', _fmt(t['reports']), ''),
            ('Reports with correction records', _fmt(t['corrected']), ''), ('Recorded sends on time', 'Unknown' if t['on_time_pct'] is None else f'{t["on_time_pct"]:.1f}%', ''),
            ('Reported parts inspected', _ov_number(t['inspected']), '')]


def _ov_evidence(o):
    t = o['totals']
    lines = ['Client exposure: Unknown. No immutable delivery snapshot establishes what the client saw.',
             'The headline counts findings in current reviews of earliest original uploads, not confirmed distinct errors. '
             'If an original is absent, the earliest saved copy is used. '
             f'{_fmt(t.get("current_issues", 0))} listed findings remain on the selected versions.',
             'Each source/job/report date contributes one selected version: recorded sent, then currently approved, then corrected copy, then newest. '
             'Selection does not prove that independent uploads are revisions of the same report.']
    for field, label in (('inspected', 'Inspected'), ('rejected', 'Rejected'), ('certified', 'Accepted')):
        value = _ov_number(t.get(field))
        known = _ov_number(t.get(field+'_known'))
        lines.append(f'{label}: {value}. Known subtotal: {known}; complete for {_fmt(t.get(field+"_complete_reports", 0))} of {_fmt(t["reports"])} selected reports. '
                     f'{_fmt(t.get(field+"_incompatible_reports", 0))} use incompatible units.')
        if t.get(field+'_range_exceeded'):
            lines.append(f'{label} exceeds supported numeric precision or range; its total and rate are not shown.')
    lines.append('Reject rate: ' + ('Unknown because quantities are incomplete, incompatible, or outside supported numeric precision.' if t.get('rate') is None else _pctf(t['rate'], 2)) )
    lines.append(f'Median upload-to-send time uses {_fmt(t.get("turnaround_reports", 0))} reports with valid timestamps. '
                 f'{_fmt(t.get("on_time_unknown", 0))} recorded sends have unknown timing.')
    cov = (o.get('statistics') or {}).get('coverage') or {}
    lines.append(f'Coverage: {_fmt(cov.get("excluded_identity", 0))} versions excluded for missing or invalid job/date; '
                 f'{_fmt(cov.get("excluded_assignment", 0))} for unresolved source assignment; '
                 f'{_fmt(cov.get("competing_original_groups", 0))} groups contain multiple independent original uploads. '
                 f'{_fmt(cov.get("first_original_missing_reports", 0))} selected reports have no saved original. '
                 f'{_fmt(cov.get("first_order_unknown_reports", 0))} groups have incomplete upload timestamps, so the earliest-original order is uncertain. '
                 f'{_fmt(cov.get("conflicting_client_groups", 0))} groups were excluded for conflicting client assignments.')
    if not t.get('caught_complete', True) or not t.get('current_issues_complete', True):
        lines.append('Some finding lists are truncated or original uploads are absent. The displayed finding counts are known listed findings only.')
    lines.append('Quantity completeness does not independently verify the source data.')
    return '<div class="evidence">' + ''.join(f'<p class="sub">{_e(x)}</p>' for x in lines) + '</div>'


def _ov_table(o):
    rows = ''.join(f'<tr><td>{_e(j["job"])}<br><small>{_e(j.get("source_label") or "")}</small></td><td class="num">{_fmt(j["days"])}</td><td class="num">{_ov_number(j["inspected"])}</td>'
                   f'<td class="num">{_pctf(j["rate"], 2) if j["rate"] is not None else "Unknown"}</td><td class="num">{_fmt(j["caught"])}</td>'
                   f'<td class="num">{_fmt(j["corrected"])}</td><td class="num">{_fmt(j["sent"])} of {_fmt(j["days"])}</td></tr>' for j in o['jobs'])
    return ('<table><thead><tr><th>Job</th><th class="num">Days</th><th class="num">Inspected</th><th class="num">Reject rate</th>'
            '<th class="num">First-original findings</th><th class="num">Correction records</th><th class="num">Recorded sends</th></tr></thead><tbody>'
            + (rows or '<tr><td colspan="7" class="empty">No reports in this period.</td></tr>') + '</tbody></table>')


def build_overview_page(o):
    t = o['totals']
    kpi_html = ''.join(f'<div class="kpi {c}"><b>{v}</b><span>{_e(k)}</span></div>' for k, v, c in _ov_kpis(o))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PPG Quality: Quality Overview, {_e(o['label'])}</title><style>{CSS}</style></head>
<body>
<header class="band"><div class="brand">PPG QUALITY</div><h1>Quality overview</h1><p>{_e(o['label'])} · {_fmt(t['jobs'])} job/source groups</p></header>
<main class="wrap">
<section class="kpis">{kpi_html}</section>
<section class="grid">
<div class="card"><h2>What was caught</h2><p class="sub">{_fmt(t['caught'])} listed first-original findings in current reviews; {_fmt(t['must_fix'])} marked must-fix. These are not verified client outcomes.</p>{_hbars(o['categories'], lambda i: i['reason'], lambda i: i['count'], '#c0392b')}</div>
<div class="card"><h2>Recorded report activity</h2><p class="sub">Verifier records only; client receipt and external sending are unknown.</p>
{_hbars([dict(reason='Recorded sends', count=t['sent']), dict(reason='Known on-time sends', count=t['on_time']), dict(reason='Correction records', count=t['corrected']), dict(reason='Still on Hold', count=t['open'])], lambda i: i['reason'], lambda i: i['count'])}
<p class="sub" style="margin-top:12px">{'Typical time from upload to sent: ' + _fmt(t['turnaround'], 1) + ' hours.' if t['turnaround'] is not None else ''}</p></div>
<div class="card wide"><h2>By job</h2><div class="scroll">{_ov_table(o)}</div></div>
<div class="card wide"><h2>Definitions and data coverage</h2>{_ov_evidence(o)}</div>
</section>
<footer>Prepared by PPG Quality from the daily end-of-shift reports checked in the verifier, {_e(o['start'])} to {_e(o['end'])}, on {_e(o['generated'])}.</footer>
</main></body></html>"""


def build_overview_slides(o):
    t = o['totals']
    foot = f'<div class="foot"><span>PPG Quality · Quality overview</span><span>{_e(o["label"])}</span></div>'
    tiles = ''.join(f'<div class="tile {c}"><b>{v}</b><span>{_e(k)}</span></div>' for k, v, c in _ov_kpis(o))
    worst = sorted([j for j in o['jobs'] if j['rate'] is not None], key=lambda j: -j['rate'])[:6]
    slides = [
        f'<section class="slide title"><div class="brand">PPG QUALITY</div><h1>Quality overview</h1><p>{_e(o["label"])}</p>{foot}</section>',
        f'<section class="slide"><h2>At a glance</h2><p class="lede">{_fmt(t["reports"])} selected daily reports across {_fmt(t["jobs"])} job/source groups; {_fmt(t["caught"])} listed findings on first-original versions in current reviews. Client exposure is unknown.</p><div class="body"><div class="tiles">{tiles}</div></div>{foot}</section>',
        f'<section class="slide"><h2>Listed first-original findings</h2><p class="lede">Current reviews, not confirmed unique errors or proof of prevention. All categories shown.</p><div class="body">{_hbars(o["categories"], lambda i: i["reason"], lambda i: i["count"], "#c0392b")}</div>{foot}</section>',
        f'<section class="slide"><h2>Recorded report activity</h2><p class="lede">{_fmt(t["sent"])} recorded verifier sends{", " + _pctf(t["on_time_pct"], 1) + " on time" if t["on_time_pct"] is not None else "; on-time rate unknown"}{"; median " + _fmt(t["turnaround"], 1) + " hours from upload to send" if t["turnaround"] is not None else ""}. External sending and client receipt are unknown.</p><div class="body">'
        + _hbars([dict(reason='Recorded sends', count=t['sent']), dict(reason='Known on-time sends', count=t['on_time']), dict(reason='Correction records', count=t['corrected']), dict(reason='Still on Hold', count=t['open'])], lambda i: i['reason'], lambda i: i['count'])
        + f'</div>{foot}</section>',
        f'<section class="slide"><h2>Reject rate by job</h2><p class="lede">Up to six highest known rates. Incomplete or incompatible quantities are excluded, not treated as zero.</p><div class="body">{_hbars([dict(reason=j["job"], count=round(j["rate"], 2)) for j in worst], lambda i: i["reason"], lambda i: i["count"], "#20594d", "%")}</div>{foot}</section>',
        f'<section class="slide"><h2>By job</h2><p class="lede">{_fmt(t["jobs"])} job/source groups.</p><div class="body">{_ov_table(o)}</div>{foot}</section>',
        f'<section class="slide"><h2>Definitions and data coverage</h2><div class="body">{_ov_evidence(o)}</div>{foot}</section>',
        f'<section class="slide title"><div class="brand">PPG QUALITY</div><h1>Quality review</h1><p>Check unresolved findings and missing source values before relying on these reports.</p>{foot}</section>',
    ]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PPG Quality: Quality Overview, {_e(o['label'])}</title><style>{SLIDE_CSS}</style></head>
<body><main class="deck">{''.join(slides)}</main>
<div class="nav"><button id="prev" type="button">‹</button><span id="n"></span><button id="next" type="button">›</button><button id="all" type="button">All slides</button></div>
<script>{SLIDE_JS}</script></body></html>"""
