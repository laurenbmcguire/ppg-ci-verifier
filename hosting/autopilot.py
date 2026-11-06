"""Autopilot: the daily work done for you, with a person still checking anything uncertain.

When it is on, opening the app (at most every 15 minutes) does three things for every job on Today:
1. builds yesterday's and today's report from SharePoint once the shift is over (45 minutes after the last scan),
2. makes only the *sure* fixes (hidden characters and spaces, numbers stored as text, values SharePoint confirms,
   part numbers that differ only in capitals) in a corrected copy; the original is never changed,
3. leaves every uncertain item (typos, spelling guesses, math guesses, AI suggestions, heuristic checks) for a person.
Nothing is ever approved or sent automatically.
"""
import time
from datetime import datetime, timedelta, timezone

from hosting.spreadsheet_inbox import SpreadsheetInbox

EVERY = timedelta(minutes=15)
KEY = 'autopilot'


def enabled():
    """Autopilot is paused for now; set PPG_AUTOPILOT=1 to bring it back."""
    import os
    return os.environ.get('PPG_AUTOPILOT') == '1'


def settings(inbox):
    return inbox.setting(KEY, {}) or {}


def set_on(inbox, on, by=None):
    cfg = settings(inbox)
    return inbox.set_setting(KEY, dict(cfg, on=bool(on), changed_by=by, changed_at=datetime.now(timezone.utc).isoformat()))


def _jobs(h, inbox, today):
    watch = inbox.setting('today_jobs', []) or []
    recent = set()
    for r in inbox.list()['spreadsheets']:
        rv = r.get('review') or {}
        if rv.get('job') and rv.get('day') and rv['day'] >= (today - timedelta(days=21)).isoformat():
            recent.add(rv['job'].upper())
    return [j for j in sorted(set(watch) | recent) if h.can_job(j)]


def clean(h, file_hash):
    """Corrected copy with only the sure fixes. Returns (new_file_hash or None, sure_count, left_for_a_person)."""
    with h.store().session() as directory:
        props = SpreadsheetInbox(directory).fix_proposals(file_hash)['proposals']
    sure = [p for p in props if p.get('sure')]
    if not sure:
        return None, 0, len(props)
    with h.store().session() as directory:
        fixed = SpreadsheetInbox(directory).apply_fixes(file_hash, [dict(p, source=f"{p.get('source') or 'Fix'} (autopilot)") for p in sure])
    from hosting.public_api import _after_change
    _after_change(h, fixed['file_hash'])
    return fixed['file_hash'], len(sure), len(props) - len(sure)


def run(h, today, force=False, budget=120.0, max_builds=8):
    """One pass. `today` is the date at PPG (America/Chicago). Returns a summary for the screen."""
    from hosting import public_api as ops
    started, now = time.monotonic(), datetime.now(timezone.utc)
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        cfg = settings(inbox)
        if not cfg.get('on') and not force:
            return dict(on=False, ran=False)
        last = cfg.get('last_run')
        if not force and last and now - datetime.fromisoformat(last) < EVERY:
            return dict(on=bool(cfg.get('on')), ran=False, last=cfg.get('last'))
        inbox.set_setting(KEY, dict(cfg, last_run=now.isoformat()))   # claim the pass so two people opening the app don't both run it
        jobs = _jobs(h, inbox, today)
    days = [today - timedelta(days=1), today]
    actions, builds = [], 0
    for job in jobs:
        for d in days:
            if time.monotonic() - started > budget:
                break
            with h.store().session() as directory:
                rep = SpreadsheetInbox(directory).reports_for(job, d, d).get(d.isoformat())
            if not rep and builds < max_builds:
                try:
                    out = ops.op_build(h, job, d.isoformat())
                    builds += 1
                    rep = dict(file_hash=out['report']['file_hash'], approved=None, sent_at=None, fixed_from=None, fixes_applied=0)
                    actions.append(dict(job=job, day=d.isoformat(), did='built', file_hash=rep['file_hash']))
                except ops.ApiError as exc:
                    if 'still be going' in str(exc):
                        actions.append(dict(job=job, day=d.isoformat(), did='waiting', why=str(exc)))
                    continue
                except Exception as exc:   # one job's SharePoint trouble never stops the others
                    actions.append(dict(job=job, day=d.isoformat(), did='error', why=str(exc)[:200]))
                    continue
            if not rep or rep.get('approved') or rep.get('sent_at') or rep.get('fixed_from') or rep.get('fixes_applied'):
                continue
            try:
                new, n, left = clean(h, rep['file_hash'])
            except Exception as exc:
                actions.append(dict(job=job, day=d.isoformat(), did='error', why=str(exc)[:200]))
                continue
            if new:
                actions.append(dict(job=job, day=d.isoformat(), did='cleaned', file_hash=new, fixed=n, to_check=left))
            elif left:
                actions.append(dict(job=job, day=d.isoformat(), did='to_check', file_hash=rep['file_hash'], to_check=left))
    summary = dict(at=now.isoformat(), built=sum(a['did'] == 'built' for a in actions), cleaned=sum(a['did'] == 'cleaned' for a in actions),
                   fixed=sum(a.get('fixed', 0) for a in actions), to_check=sum(a.get('to_check', 0) for a in actions),
                   waiting=sum(a['did'] == 'waiting' for a in actions), errors=sum(a['did'] == 'error' for a in actions), actions=actions[:60],
                   unfinished=time.monotonic() - started > budget)
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        inbox.set_setting(KEY, dict(settings(inbox), last=summary))
    return dict(on=True, ran=True, last=summary)
