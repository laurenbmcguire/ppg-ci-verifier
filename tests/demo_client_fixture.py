"""Production-LIKE synthetic source for end-to-end tests. Entirely fictitious.

Client DEMO-CLIENT, job TEST-9001, every identifier, part number, count, name and photo file name below is
invented for testing. Nothing here was copied or derived from a real list, report, client or person.

It answers the same requests the read-only capture makes (list metadata, columns, items, one item, a photo
download), so the REAL capture code runs against it. The shape imitates conditions seen in production-like lists:
  * dozens of rows over three shifts, inspected and OK (certified) quantities present and positive;
  * the NOK (rejected) quantity is ABSENT on every row: unknown, not zero;
  * some rows name a defect photo, and the photo download answers 403 (no permission);
  * some rows carry a lookup column value as a structured object, whose text was never verified.
"""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hosting import source_first as sf
from source_first_fixture import MAP as BASE_MAP

HOST = 'example.sharepoint.com'
SITE = HOST + ',aaaaaaaa-0000-4000-8000-000000009001,bbbbbbbb-0000-4000-8000-000000009001'
LIST = 'cccccccc-0000-4000-8000-000000009001'
CLIENT, JOB, DAY = 'DEMO-CLIENT', 'TEST-9001', '2026-11-10'
SCOPE = dict(site_id=SITE, list_id=LIST, job=JOB, business_date=DAY)
MAP = deepcopy(BASE_MAP)
HEADER = dict(paying_client='DEMO-CLIENT (fictitious)', worksite='Demo Plant 1 (fictitious)', part_name='Demo bracket',
              evidence='Synthetic header for the DEMO-CLIENT test fixture')
ROWS_PER_SHIFT = 12
PHOTO_ROWS = (3, 8, 15, 27)            # item ids that name a defect photo
LOOKUP_ROWS = (5, 11, 20, 26, 33)      # item ids whose "inspected for" arrives as a lookup object


def columns():
    names = sorted(set(MAP['fields'].values()) | {'ActualScan', 'DefectPhoto'})
    out = {n: {'name': n, 'displayName': n} for n in names}
    out['Date'].update(id='dddddddd-0000-4000-8000-000000009001', dateTime={'format': 'dateOnly'})
    out['DefectPhoto']['thumbnail'] = {}
    out[MAP['fields']['inspected_for']]['lookup'] = {'listId': 'eeeeeeee-0000-4000-8000-000000009001', 'columnName': 'Title'}
    return out


def items(scanned_at=None, nok=None, other_day=True):
    """36 invented rows for DAY (12 per shift) plus, optionally, 2 rows of another day that must be ignored.
    ``nok`` None leaves the NOK column out of every row; a number puts that NOK on every row."""
    scanned_at = scanned_at or (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    f, out = MAP['fields'], []
    for i in range(1, 3 * ROWS_PER_SHIFT + 1):
        shift = str((i - 1) // ROWS_PER_SHIFT + 1)
        inspected = 40 + (i * 7) % 23                     # invented, always positive
        ok = inspected - (nok or 0) if nok is not None else inspected - (i % 4)      # OK recorded; NOK not recorded
        fields = {f['date']: DAY, f['shift']: shift, f['location']: 'Demo Line %s' % shift, f['part']: '0009001%03d' % (i % 5),
                  f['identifier']: '00TEST%05d' % i, f['inspected_for']: 'Surface finish', f['inspected']: inspected,
                  f['condition']: 'See count', f['reject_reason']: None, f['certified']: ok,
                  f['comments']: 'Invented test row %d' % i, 'ActualScan': scanned_at}
        if nok is not None:
            fields[f['rejected']] = nok
            fields[f['reject_reason']] = 'Invented defect'
        if i in LOOKUP_ROWS:
            fields[f['inspected_for']] = {'LookupId': 70 + i, 'LookupValue': 'Edge burr (unverified lookup text)'}
        if i in PHOTO_ROWS:
            fields['DefectPhoto'] = json.dumps({'fileName': 'demo-defect-%02d.jpg' % i, 'type': 'thumbnail'})
        out.append(dict(id=str(i), eTag='"%d,1"' % i, lastModifiedDateTime=DAY + 'T12:%02d:00Z' % (i % 60), fields=fields))
    if other_day:
        for i in (901, 902):
            fields = dict(out[0]['fields'], **{f['date']: '2026-11-09', f['identifier']: '00TEST%05d' % i})
            out.append(dict(id=str(i), eTag='"%d,1"' % i, lastModifiedDateTime='2026-11-09T12:00:00Z', fields=fields))
    return out


class Source:
    """A fake read-only source. Counts every request; has no way to accept a write.

    ``page_size`` splits the item list into pages linked by @odata.nextLink, as the real service does.
    ``answers`` maps a piece of a URL to a canned (status, body, headers) answer, to imitate failures."""
    def __init__(self, rows=None, photo_status=403, photo_bytes=b'', page_size=None, answers=None, list_id=LIST):
        self.rows = items() if rows is None else rows
        self.photo_status, self.photo_bytes = photo_status, photo_bytes
        self.page_size, self.answers, self.list_id = page_size, dict(answers or {}), list_id
        self.requests, self.photo_requests = [], []

    def get(self, url):
        self.requests.append(url)
        for piece, answer in self.answers.items():
            if piece in url:
                return answer(url) if callable(answer) else answer
        if '/columns?' in url:
            return 200, {'value': list(columns().values())}
        if '/items?' in url:
            if not self.page_size:
                return 200, {'value': deepcopy(self.rows)}
            start = int(url.split('$skiptoken=')[1]) if '$skiptoken=' in url else 0
            page = {'value': deepcopy(self.rows[start:start + self.page_size])}
            if start + self.page_size < len(self.rows):
                page['@odata.nextLink'] = url.split('&$skiptoken=')[0] + '&$skiptoken=%d' % (start + self.page_size)
            return 200, page
        if '/items/' in url:
            ident = url.split('/items/')[1].split('?')[0]
            return 200, deepcopy(next(r for r in self.rows if r['id'] == ident))
        return 200, {'id': self.list_id, 'displayName': 'TEST-9001 demo list', 'webUrl': 'https://%s/sites/DemoClient/Lists/TEST9001' % HOST}

    def photo(self, url):
        self.photo_requests.append(url)
        return self.photo_status, self.photo_bytes if self.photo_status == 200 else b''

    def capture(self, scope=None):
        from unittest.mock import patch
        from hosting import source_first_capture as source
        from source_first_fixture import date_contract
        with patch('hosting.photo_embed._host', return_value=HOST):
            return source.capture(sf.scope(scope or SCOPE), fetch=self.get, photo_fetch=self.photo, date_capture=date_contract)


def assignment(**kw):
    return dict(dict(verified=True, active=True, timestamp_semantics_verified=True, client_id=CLIENT, job=JOB, site_id=SITE,
                     list_id=LIST, zone='America/Chicago', scan_field='ActualScan', shift_field=MAP['fields']['shift'],
                     recorded_shifts=['1', '2', '3'], business_dates=[DAY], mapping=MAP, header=HEADER), **kw)


REGISTRY = [dict(client_key=CLIENT, site_id=SITE, list_id=LIST, job=JOB, verified=True)]
