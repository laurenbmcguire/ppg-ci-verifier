"""Cold-start activity handlers load the existing certificate before date evidence."""
import io
import json
from contextlib import contextmanager
from copy import deepcopy

import pytest

from api import index
from hosting import photo_embed, recommend, sharepoint, source_date_contracts as contracts


@pytest.fixture
def activity(monkeypatch):
    # A synthetic non-credential exercises real encrypted-setting loading. No
    # private user credential or Microsoft request is needed for this regression.
    monkeypatch.setattr(photo_embed, '_server_cert', {'key': None, 'thumb': None})
    for key in ('MS_SP_CERT_KEY', 'MS_SP_CERT_THUMBPRINT', 'VERCEL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MS_CLIENT_SECRET', 'synthetic-local-test-only')
    stored = {'key_enc': photo_embed._cipher().encrypt(b'synthetic-noncredential').decode(),
              'thumbprint': '00'}
    state = {'active_sessions': 0, 'certificate_reads': 0, 'regional_reads': 0,
             'source_reads': 0, 'stored': stored, 'regional_status': 200}
    columns = [{'id': '00000000-0000-0000-0000-000000000001',
                'name': 'Date', 'dateTime': {'format': 'dateOnly'}}]
    source = ('Synthetic', {'id': contracts.LIST, 'displayName': 'Synthetic',
              '_capture': {'site_id': contracts.SITE, 'list_id': contracts.LIST, 'columns': columns}},
              {'Date': 'Date', 'Quantity': 'Quantity Inspected', 'OKQty': 'Quantity Certified', 'NOKQty': 'Reject Quantity'},
              [{'id': str(i), 'fields': {'Date': day + 'T06:00:00Z', 'Quantity': 10, 'OKQty': 10, 'NOKQty': 0}}
               for i, day in enumerate(('2026-11-10', '2026-11-09'), 1)])
    state['source'] = source

    class Store:
        @contextmanager
        def session(self):
            state['active_sessions'] += 1
            try:
                yield 'synthetic-memory-only'
            finally:
                state['active_sessions'] -= 1

    class Inbox:
        def __init__(self, directory):
            assert directory == 'synthetic-memory-only'

        def shift_schedule(self, job):
            return None

        def setting(self, key, default=None):
            assert state['active_sessions'] == 1
            if key == photo_embed.CERT_KEY:
                state['certificate_reads'] += 1
                return state['stored']
            return default

        def jobs(self):
            return ['SYNTHETIC']

        def reports_for(self, *args):
            return {}

        def dashboard(self, *args):
            return None

        def set_setting(self, *args):
            raise AssertionError('Activity date loading must not change stored settings')

    def source_read(*args, **kwargs):
        assert state['active_sessions'] == 0
        state['source_reads'] += 1
        return source

    def site_read(*args, **kwargs):
        assert state['active_sessions'] == 0
        return contracts.SITE_URL

    def regional_read(*args, **kwargs):
        assert state['active_sessions'] == 0
        state['regional_reads'] += 1
        return state['regional_status'], deepcopy(contracts.EXPECTED_TIMEZONE)

    monkeypatch.setattr(index, 'SpreadsheetInbox', Inbox)
    monkeypatch.setattr(index, 'ai_review_configured', lambda: False)
    monkeypatch.setattr(sharepoint, 'configured', lambda: True)
    monkeypatch.setattr(sharepoint, '_list_items', source_read)
    monkeypatch.setattr(contracts, '_site_url', site_read)
    monkeypatch.setattr(photo_embed, '_get', regional_read)
    monkeypatch.setattr(recommend, 'answer', lambda *args, **kwargs: ([], None))

    def request(route, *, kind='week', denied=False, bad_origin=False):
        h = object.__new__(index.handler)
        body = json.dumps({'job': 'Synthetic', 'day': '2026-11-10', 'kind': kind}).encode()
        h.headers = {'Host': 'localhost', 'Origin': 'http://other.invalid' if bad_origin else 'http://localhost',
                     'X-Review-Token': 'same-origin', 'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        h.rfile = io.BytesIO(body)
        h.checked_route = lambda: route
        h.gate = lambda *args: h.fail(403, 'Synthetic access denied') if denied else None
        h.store = lambda: Store()
        h.can_job = lambda job: True
        h.send = lambda status, body, *args, **kwargs: {'status': status, 'body': json.loads(body)}
        h.fail = lambda status, message: {'status': status, 'error': message}
        return h.do_POST()

    state['request'] = request
    return state


ROUTES = [('/api/today/job', 'week'), ('/api/periods', 'week'), ('/api/periods', 'month')]


@pytest.mark.parametrize('route,kind', ROUTES)
def test_cold_start_loads_stored_certificate_before_activity_capture(activity, route, kind):
    before = deepcopy(activity['source'])
    assert not photo_embed.certificate_ready()
    result = activity['request'](route, kind=kind)
    assert result['status'] == 200
    assert activity['certificate_reads'] == 1
    assert photo_embed.certificate_ready()
    assert activity['regional_reads'] == 1
    assert activity['source_reads'] == 1
    assert activity['active_sessions'] == 0
    assert activity['source'] == before
    if route.endswith('/job'):
        counts = result['body']['sharepoint']
        assert counts['today'] == counts['yesterday'] == 1
        assert not counts.get('error')
    else:
        days = {day['day']: day for day in result['body']['period']['days']}
        assert days['2026-11-10']['sharepoint']['scans'] == 1
        assert days['2026-11-09']['sharepoint']['scans'] == 1


@pytest.mark.parametrize('route,kind', ROUTES)
@pytest.mark.parametrize('fault', ['missing_certificate', 'corrupt_certificate', 'regional_denied', 'unknown_date'])
def test_unconfirmed_evidence_stays_unknown_after_startup(activity, route, kind, fault):
    if fault == 'missing_certificate':
        activity['stored'] = None
    elif fault == 'corrupt_certificate':
        activity['stored']['key_enc'] = 'invalid-synthetic-encrypted-value'
    elif fault == 'regional_denied':
        activity['regional_status'] = 403
    elif fault == 'unknown_date':
        activity['source'][3][0]['fields']['Date'] = '2026-11-10T00:00:00Z'
    result = activity['request'](route, kind=kind)
    assert result['status'] == 200
    assert activity['certificate_reads'] == 1
    assert activity['regional_reads'] == (0 if fault.endswith('certificate') else 1)
    if route.endswith('/job'):
        counts = result['body']['sharepoint']
        assert counts['today'] is counts['yesterday'] is None
        assert counts['complete'] is False
        assert 'unknown' in counts['error'].lower()
    else:
        period = result['body']['period']
        assert all(day['sharepoint'] is None for day in period['days'])
        assert period['totals']['scans'] is None
        assert 'unknown' in period['sharepoint_source'].lower()


@pytest.mark.parametrize('route,kind', ROUTES)
@pytest.mark.parametrize('denied,bad_origin', [(True, False), (False, True)])
def test_activity_access_gates_still_precede_certificate_and_source_reads(activity, route, kind, denied, bad_origin):
    result = activity['request'](route, kind=kind, denied=denied, bad_origin=bad_origin)
    assert result['status'] == 403
    assert activity['certificate_reads'] == activity['source_reads'] == activity['regional_reads'] == 0
    assert not photo_embed.certificate_ready()
