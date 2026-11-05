"""Shift times: which shift (and which report day) a scan belongs to, from its plant-time clock time.

Defaults come from the end-of-shift routine (plant time, US Central), with the overlaps it notes removed:
1st 6:30 AM-2:30 PM, 2nd 2:30-10:30 PM, 3rd 10:30 PM-6:30 AM (the routine cuts 3rd shift at 6:31 AM). Each job can have its own times. A shift that runs past midnight belongs to the day it
started: a 3rd-shift scan at 1:15 AM on the 6th is part of the 5th's 3rd-shift report (the "midnight problem").
The Shift an inspector picks at login is not trusted; the scan time decides.
"""
import re
from datetime import datetime, timedelta

ZONE = 'America/Chicago'
DEFAULT = [dict(name='1st', start='06:30', end='14:30'),
           dict(name='2nd', start='14:30', end='22:30'),
           dict(name='3rd', start='22:30', end='06:30')]
HHMM = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
NUMBER = {'1st': '1', '2nd': '2', '3rd': '3', '4th': '4'}


class ShiftError(ValueError):
    pass


def _minutes(hhmm):
    m = HHMM.match(str(hhmm or '').strip())
    if not m:
        raise ShiftError(f'"{hhmm}" is not a time like 06:00 or 14:30.')
    return int(m.group(1)) * 60 + int(m.group(2))


def clean(shifts):
    """Validate a list of {name, start, end}; returns it tidied. Raises ShiftError."""
    if not isinstance(shifts, list) or not 1 <= len(shifts) <= 4:
        raise ShiftError('Give one to four shifts.')
    out, names = [], set()
    for s in shifts:
        if not isinstance(s, dict):
            raise ShiftError('Each shift needs a name, a start and an end.')
        name = ' '.join(str(s.get('name') or '').split())[:20]
        if not name:
            raise ShiftError('Each shift needs a name, for example 1st.')
        if name.lower() in names:
            raise ShiftError(f'Two shifts are called {name}.')
        start, end = _minutes(s.get('start')), _minutes(s.get('end'))
        if start == end:
            raise ShiftError(f'{name} shift starts and ends at the same time.')
        names.add(name.lower())
        out.append(dict(name=name, start=f'{start // 60:02d}:{start % 60:02d}', end=f'{end // 60:02d}:{end % 60:02d}'))
    covered = [0] * 1440
    for s in out:
        a, b = _minutes(s['start']), _minutes(s['end'])
        for t in (range(a, b) if a < b else list(range(a, 1440)) + list(range(0, b))):
            covered[t] += 1
    if any(c > 1 for c in covered):
        raise ShiftError('Two shifts overlap. Make each shift start when the one before it ends (or later).')
    return out


def number(name):
    """'1st' -> '1' (matches how reports name shifts); other names as given."""
    n = str(name or '').strip().lower()
    m = re.match(r'^([1-4])(st|nd|rd|th)?\b', n)
    return m.group(1) if m else NUMBER.get(n, str(name))


def clock(hhmm):
    h, m = divmod(_minutes(hhmm), 60)
    return f'{(h % 12) or 12}:{m:02d} {"AM" if h < 12 else "PM"}'


def label(shift):
    return f'{shift["name"]} shift ({clock(shift["start"])} to {clock(shift["end"])})'


def for_job(settings, job):
    """The job's own times if set, else the default."""
    settings = settings or {}
    jobs = {k.upper(): v for k, v in (settings.get('jobs') or {}).items()}
    return jobs.get((job or '').upper()) or settings.get('default') or DEFAULT


def assign(local, schedule):
    """(shift dict, report day ISO) for a plant-time datetime, or (None, local date) outside every shift."""
    t = local.hour * 60 + local.minute
    for s in schedule:
        a, b = _minutes(s['start']), _minutes(s['end'])
        if a < b and a <= t < b:
            return s, local.date().isoformat()
        if a > b and (t >= a or t < b):  # runs past midnight: the early-morning part belongs to the day before
            day = local.date() - timedelta(days=1) if t < b else local.date()
            return s, day.isoformat()
    return None, local.date().isoformat()


def local_time(stamp, zone=ZONE):
    """A SharePoint timestamp (ISO, usually UTC) in plant time, or None."""
    from zoneinfo import ZoneInfo
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.astimezone(ZoneInfo(zone)) if dt.tzinfo else dt


def sheet_shift(name):
    """'End of Shift Report-1st Shift' / '2nd Shift' / 'Shift 3' -> '1' / '2' / '3', else None."""
    t = str(name or '').lower()
    m = re.search(r'\b([1-4])(?:st|nd|rd|th)\b', t) or re.search(r'\bshift\s*([1-4])\b', t)
    if m:
        return m.group(1)
    for word, n in (('first', '1'), ('second', '2'), ('third', '3')):
        if re.search(rf'\b{word}\b', t):
            return n
    return None
