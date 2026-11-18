"""ONE-HOUR REPORTING REHEARSAL harness. FULLY SYNTHETIC. TEST ONLY. Adds no production trust.

It drives the real code end to end against invented data held in memory:

  schema candidate capture (hosting.last_scan_schema_capture, mocked transport)
    -> a FAKE capture authority records the receipt candidate, a FAKE approver approves the exact snapshot
    -> hosted preflight and run (hosting.last_scan_hosted.handle) with a FAKE identity and a probe backed by the
       same capture adapter
    -> the real source capture, timers, durable state (SQLite file, reopened to imitate restarts)
    -> the real native Excel draft and checklist per client
    -> a FAKE outbox whose only recipient is a fictitious "Lauren" address, behind a FAKE provider contract.

Two invented clients: ALPHA-REHEARSAL / RH-7001 and BRAVO-REHEARSAL / RH-7002. Every identifier, count, name, header
and photo below is made up. Nothing here is an identity system, an approval system, a collector, a delivery provider,
a schedule or a route, and none of it can be imported by application code (it lives under tests/).
"""
import io
import itertools
import json
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from openpyxl import load_workbook

import demo_client_fixture as demo
import readiness_fixture as rf
from hosting import last_scan_dispatch as hosted_dispatch
from hosting import last_scan_hosted as hosted, last_scan_readiness as readiness, last_scan_runner as runner
from hosting import last_scan_transport as transport, source_first as sf
from hosting.last_scan_hosted import CaptureAuthority, Deps, Quota, SchemaProbe
from hosting.last_scan_local import BlobArtifacts, StateStore, StoredDraftRouter
from hosting.last_scan_schema_capture import capture_schema, receipt_candidate
from hosting.last_scan_service import ScheduledWorker, source_adapter
from test_last_scan_durable import FAKE_CONTRACT
from test_last_scan_hosted import FakeApprovals, FakeAuthorizer

DAY, ZONE, HOST = '2026-11-10', 'America/Chicago', 'example.sharepoint.com'
COLLECTOR = 'rehearsal-fake-collector'
LAUREN = 'lauren@rehearsal.invalid'                      # a fictitious address on a reserved, undeliverable domain
ALICE = {'x-test-principal-proof': 'proof-alice'}
VISIBLE = ('Summary', '1st Shift', '2nd Shift', '3rd Shift', 'Rejects & Photos')

CLIENTS = dict(
    alpha=dict(client_id='ALPHA-REHEARSAL', job='RH-7001', tag='ALPHA', part='7001000', line='Alpha Line',
               site_id=HOST + ',a1a1a1a1-0000-4000-8000-000000007001,b1b1b1b1-0000-4000-8000-000000007001',
               list_id='c1c1c1c1-0000-4000-8000-000000007001', shifts=['1', '2', '3'], nok=None,
               header=dict(paying_client='ALPHA REHEARSAL CO (fictitious)', worksite='Alpha Plant (fictitious)', part_name='Alpha bracket',
                           evidence='Synthetic header for the ALPHA rehearsal client'),
               not_before={DAY + '|1': '2026-11-10T18:00:00+00:00', DAY + '|2': '2026-11-10T22:00:00+00:00', DAY + '|3': '2026-11-11T06:00:00+00:00'}),
    bravo=dict(client_id='BRAVO-REHEARSAL', job='RH-7002', tag='BRAVO', part='7002000', line='Bravo Line',
               site_id=HOST + ',a2a2a2a2-0000-4000-8000-000000007002,b2b2b2b2-0000-4000-8000-000000007002',
               list_id='c2c2c2c2-0000-4000-8000-000000007002', shifts=['1', '2'], nok=2,
               header=dict(paying_client='BRAVO REHEARSAL LLC (fictitious)', worksite='Bravo Works (fictitious)', part_name='Bravo housing',
                           evidence='Synthetic header for the BRAVO rehearsal client'),
               not_before=None))


def utc(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(timezone.utc)


def png(colour=(10, 120, 200)):
    from PIL import Image
    out = io.BytesIO(); Image.new('RGB', (4, 3), colour).save(out, format='PNG')
    return out.getvalue()


class Clock:
    """The clock the rehearsal INJECTS: the hosted entry point, the runner, the timers, the worker, the probe, the
    schema capture and the stamp on each source capture all read it.

    It is not the only clock in the process. Still on real time, and known:
      * the run budget (``time.monotonic``), which only bounds how long one firing may take;
      * ``source_first_capture.cooldown``, which also works out a wall-clock ``not_before`` for a relative Retry-After
        (kept for the existing source-first service). The runner no longer uses that value: it counts a relative wait
        on its own clock (tests/test_last_scan_cooldown_clock.py);
      * timestamps written into failure evidence by the capture code (``sf.now()``), which are records, not decisions;
      * the real capture's own ``captured_at``, which the tenant below overwrites with this clock before sealing."""
    def __init__(self, start):
        self.at = utc(start)
    def __call__(self):
        return self.at.isoformat()
    def advance(self, **kw):
        self.at += timedelta(**kw); return self()
    def set(self, text):
        assert utc(text) >= self.at, 'the rehearsal clock never goes backwards'
        self.at = utc(text); return self()


class Tenant(demo.Source):
    """One invented client's list, in memory. Answers the read requests the real capture code makes; has no way to
    accept a write. ``down`` imitates an outage of the item collection; ``stale_by`` an out-of-order capture."""
    def __init__(self, spec, clock, scans):
        self.spec, self.clock = spec, clock
        self.assignment = None
        rows = []
        for r in demo.items(scanned_at='2026-11-10T12:00:00+00:00', nok=spec['nok'], other_day=True):
            f = r['fields']; shift = f[demo.MAP['fields']['shift']]
            if f[demo.MAP['fields']['date']] == DAY and shift not in scans:
                continue                                              # this shift has not scanned anything yet
            r = deepcopy(r)
            text = json.dumps(r).replace('00TEST', spec['tag']).replace('0009001', spec['part']).replace('Demo Line', spec['line']) \
                       .replace('Invented test row', spec['tag'] + ' rehearsal row').replace('demo-defect', spec['tag'].lower() + '-defect')
            r = json.loads(text)
            if r['fields'][demo.MAP['fields']['date']] == DAY:
                r['fields']['ActualScan'] = scans[shift]
            rows.append(r)
        super().__init__(rows=rows, photo_status=403, list_id=spec['list_id'])
        self.down, self.stale_by, self.columns_now = None, None, None

    # ---- what the source would answer
    def get(self, url):
        assert HOST in self.spec['site_id'] and 'graph.microsoft.com' not in url.split('/v1.0')[0].replace('https://graph.microsoft.com', ''), url
        if '/items' in url and self.down:
            self.requests.append(url)
            return self.down
        if '/columns?' in url:
            self.requests.append(url)
            return 200, {'value': deepcopy(self.columns_now)}, {}
        if '/items' not in url:                                       # the list resource itself
            self.requests.append(url)
            return 200, dict(id=self.spec['list_id'], displayName=self.spec['job'] + ' rehearsal list', eTag='"e-%d"' % len(self.rows),
                             webUrl='https://%s/sites/%s/Lists/%s' % (HOST, self.spec['tag'].title(), self.spec['job']),
                             lastModifiedDateTime='2026-11-10T12:00:00Z', parentReference=dict(siteId=self.spec['site_id'])), {}
        return super().get(url)

    def capture(self, scope):
        """The REAL capture code against this tenant, stamped with the rehearsal clock instead of the wall clock."""
        from hosting import source_first_capture as source
        from source_first_fixture import date_contract
        with mock.patch('hosting.photo_embed._host', return_value=HOST):
            cap, blobs = source.capture(sf.scope(scope), fetch=self.get, photo_fetch=self.photo, date_capture=date_contract)
        at = utc(self.clock()) - (self.stale_by or timedelta(0))
        cap = {k: v for k, v in cap.items() if k not in ('snapshot_hash', 'envelope_hash')}
        cap['captured_at'] = at.isoformat()
        return sf.seal(cap), blobs

    # ---- what happens on the floor
    def scan(self, shift, at, count=1, **extra):
        """New parts are scanned: ``count`` more rows for this shift, with the given scan time."""
        f = demo.MAP['fields']
        template = next(r for r in self.rows if r['fields'][f['date']] == DAY)
        for _ in range(count):
            ident = str(max(int(r['id']) for r in self.rows) + 1)
            fields = dict(template['fields'], **{f['shift']: shift, f['identifier']: '%s%05d' % (self.spec['tag'], int(ident)),
                                                 f['location']: '%s %s' % (self.spec['line'], shift), 'ActualScan': at})
            fields.pop('DefectPhoto', None); fields.update(extra)
            self.rows.append(dict(id=ident, eTag='"%s,1"' % ident, lastModifiedDateTime=DAY + 'T12:00:00Z', fields=fields))


class RecordedCaptures(CaptureAuthority):
    """TEST ONLY stand-in for a capture authority: remembers what it was asked to record. Not origin proof."""
    def __init__(self): self.records = {}
    def record(self, candidate):
        self.records[candidate['snapshot_identity']] = {k: v for k, v in candidate.items() if k != 'status'}
    def receipt(self, identity): return deepcopy(self.records.get(identity))


class AdapterProbe(SchemaProbe):
    """TEST ONLY live-schema look: asks each tenant for its columns through the schema capture adapter."""
    def __init__(self, tenants, clock): self.tenants, self.clock, self.calls = tenants, clock, 0
    def current(self, site_id, list_id):
        self.calls += 1
        tenant = next(t for t in self.tenants.values() if (t.spec['site_id'], t.spec['list_id']) == (site_id, list_id))
        return capture_schema(site_id, list_id, fetch=tenant.get, clock=self.clock)['snapshot']


class FakeLaurenOutbox:
    """TEST ONLY delivery sink. Keeps messages in a list. Its one recipient is a fictitious address."""
    def __init__(self): self.messages, self.calls, self.fail = [], 0, None
    def send(self, *, idempotency_key, attempt_id, audience, payload):
        self.calls += 1
        if self.fail:
            raise self.fail
        assert audience == 'Lauren', audience
        self.messages.append(dict(to=LAUREN, operation_id=idempotency_key, attempt_id=attempt_id, payload=deepcopy(payload)))
        return dict(operation_id=idempotency_key, attempt_id=attempt_id, provider_receipt='fake-receipt-%d' % len(self.messages))


class Rehearsal:
    """Everything one rehearsal needs. ``poll()`` is one hosted run; ``restart()`` reopens the durable store."""
    START = '2026-11-10T19:00:00+00:00'
    SCANS = dict(alpha={'1': '2026-11-10T17:50:00+00:00', '2': '2026-11-10T18:55:00+00:00'},          # shift 3 has not started
                 bravo={'1': '2026-11-10T18:20:00+00:00', '2': '2026-11-10T18:40:00+00:00'})

    def __init__(self, test, clients=('alpha', 'bravo')):
        self.test = test
        self.folder = tempfile.TemporaryDirectory(); test.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'rehearsal-state.db'
        self.clock = Clock(self.START)
        self.tenants = {name: Tenant(CLIENTS[name], self.clock, self.SCANS[name]) for name in clients}
        self.n, self.last, self.dispatch_n = 0, {}, itertools.count(1)
        self.lauren = FakeLaurenOutbox()
        self.approvals, self.captures = FakeApprovals(), RecordedCaptures()
        self.artifacts_log = {}
        # 1. schema candidate capture, half an hour before the rehearsal starts, through the adapter and a mocked transport
        capture_clock = Clock('2026-11-10T18:30:00+00:00')
        assignments, self.schemas, self.schema_artifacts = [], [], {}
        for name, tenant in self.tenants.items():
            spec = tenant.spec
            bare = dict(verified=True, active=True, timestamp_semantics_verified=True, client_id=spec['client_id'], job=spec['job'],
                        site_id=spec['site_id'], list_id=spec['list_id'], zone=ZONE, scan_field='ActualScan', shift_field=demo.MAP['fields']['shift'],
                        recorded_shifts=list(spec['shifts']), business_dates=[DAY], mapping=deepcopy(demo.MAP), header=dict(spec['header']))
            if spec['not_before']:
                bare['not_before'] = dict(spec['not_before'])
            columns = rf.schema_for(bare)['columns']
            tenant.columns_now = list(columns.values())
            artifact = capture_schema(spec['site_id'], spec['list_id'], fetch=tenant.get, clock=capture_clock)
            self.schema_artifacts[name] = artifact
            self.schemas.append(artifact['snapshot'])
            # 2. a FAKE authority records the receipt candidate; the TEST-ONLY fixture reviewer pins and attests the mapping
            self.captures.record(receipt_candidate(artifact, COLLECTOR))
            tenant.assignment = rf.attested(bare, artifact['snapshot'])[0]
            assignments.append(tenant.assignment)
        self.config = dict(enabled=True, assignments=assignments)
        self.entries = [dict(client_key=t.spec['client_id'], site_id=t.spec['site_id'], list_id=t.spec['list_id'], job=t.spec['job'], verified=True)
                        for t in self.tenants.values()]
        self.store = StateStore(self.path)
        # 3. a FAKE approver approves exactly the snapshot a dry run reports
        code, dry = self.call(mode='dry_run')
        assert code == 200 and dry['ready_to_run'] is False and [c['check'] for c in dry['preflight'] if not c['ok']] == ['approval_attested_for_this_snapshot_and_principal'], dry['preflight']
        self.dry_run = dry
        self.approvals.records[dry['snapshot_digest']] = dict(snapshot_digest=dry['snapshot_digest'], approved_by='rehearsal fake approver',
                                                              approved_at=DAY, principals=['alice'])

    # ---- wiring
    def fetch(self, scope, settings):
        tenant = next(t for t in self.tenants.values() if (t.spec['site_id'], t.spec['list_id']) == (scope['site_id'], scope['list_id']))
        return source_adapter(scope, settings, capture_fn=lambda s: tenant.capture(s))

    def deps(self, **kw):
        base = dict(authorizer=FakeAuthorizer(), approvals=self.approvals, quota=Quota(10000), config=self.config, entries=self.entries,
                    schemas=self.schemas, captures=self.captures, collectors=(COLLECTOR,), schema_probe=AdapterProbe(self.tenants, self.clock),
                    store=self.store, artifacts=BlobArtifacts(self.store), fetch=self.fetch, clock=self.clock)
        base.update(kw)
        return Deps(**base)

    def call(self, mode='run', run_id=None, deps=None, **body):
        self.n += 1
        request = dict(dict(run_id=run_id or 'rehearsal-%04d' % self.n, mode=mode, timestamp=self.clock(), nonce='rehearsal-nonce-%06d' % self.n), **body)
        return hosted.handle('POST', ALICE, request, deps or self.deps())

    def poll(self, **kw):
        """One scheduled firing: a hosted run. Returns {(client, shift): result row}."""
        code, body = self.call(**kw)
        self.last_response = (code, body)
        if code != 200 or body.get('status') != 'ran':
            return {}
        by_job = {t.spec['job']: name for name, t in self.tenants.items()}
        out = {(by_job[r['job']], r['shift']): r for r in body['summary']['results']}
        self.last.update({k: v for k, v in out.items() if v.get('operation_id')})
        return out

    def restart(self):
        """The runner's host process dies and comes back. ONLY the durable store handle is reopened over the same
        database file; ``deps()`` already builds a new authorizer, probe, artifact store and worker for every call, so
        nothing of the runner's survives in memory.

        Deliberately NOT reset, because they stand for systems outside the runner that a restart would not touch:
        the tenants (the source), the fake approval and capture records, the fake outbox's list of messages, and the
        rehearsal clock. A restart of those is not rehearsed here."""
        self.store = StateStore(self.path)

    # ---- looking at what was produced
    def router(self):
        return StoredDraftRouter(BlobArtifacts(self.store), self.config['assignments'])

    def item(self, client, shift):
        scopes, _ = runner.plan(self.config, self.entries, self.clock(), self.router())
        job = self.tenants[client].spec['job']
        return next(x for x in scopes if x['scope']['job'] == job and x['scope']['shift'] == shift)

    def draft(self, client, shift, op=None):
        op = op or self.last[(client, shift)]['operation_id']
        publisher = self.router().for_scope(self.tenants[client].assignment)
        receipt = publisher.lookup(op)
        workbook = publisher.read(op, 'draft.xlsx')
        return dict(op=op, receipt=receipt, workbook=workbook, book=load_workbook(io.BytesIO(workbook), data_only=True),
                    checklist=json.loads(publisher.read(op, 'checklist.json')), note=publisher.read(op, 'checklist.txt').decode(),
                    export=publisher.export(op))

    def outbox(self, client, shift):
        with self.store.transaction('outbox:' + self.item(client, shift)['key']) as box:
            return deepcopy(box.get('entries', {}))

    def dispatch(self, client, shift, op=None, sender=None, contract=FAKE_CONTRACT):
        """TEST-ONLY DIRECT SEAM. Calls the worker's test transport (``dispatch_test``) straight away, to exercise the
        worker's own claim, recapture and outbox behaviour. It is NOT a production path and authorizes nothing: no
        identity, approval, recipient policy, capture receipt or live schema probe runs here. The production door
        (``transport.deliver``) refuses this same call for want of a dispatch grant (``through_the_door_without_a_grant``);
        the authorized path is ``hosted_dispatch`` below."""
        item = self.item(client, shift)
        worker = ScheduledWorker(self.store, self.fetch, item['render'], clock=self.clock, enabled=True, provider_contract=contract)
        if not transport.verified(contract):
            return dict(status='transport_disabled')
        return worker.dispatch_test(item['scope'], op or self.last[(client, shift)]['operation_id'], sender or self.lauren.send, item['settings'])

    def through_the_door_without_a_grant(self, client, shift, op=None, grant=None):
        """What the old helper did: bring a worker, a contract and a sender to transport.deliver, with no hosted grant."""
        item = self.item(client, shift)
        worker = ScheduledWorker(self.store, self.fetch, item['render'], clock=self.clock, enabled=True, provider_contract=FAKE_CONTRACT)
        return transport.deliver(worker, item['scope'], op or self.last[(client, shift)]['operation_id'], self.lauren.send, item['settings'], grant=grant)

    # ---- the authorized dispatch path (hosting.last_scan_dispatch), with FAKE authorities only
    def dispatch_deps(self, **kw):
        base = dict(authorizer=FakeAuthorizer(), approvals=self.approvals, quota=Quota(10000), config=self.config, entries=self.entries,
                    schemas=self.schemas, captures=self.captures, collectors=(COLLECTOR,), schema_probe=AdapterProbe(self.tenants, self.clock),
                    store=self.store, artifacts=BlobArtifacts(self.store), fetch=self.fetch, clock=self.clock,
                    provider_contract=FAKE_CONTRACT, recipients={t.spec['client_id']: 'Lauren' for t in self.tenants.values()},
                    sender=self.lauren.send, dispatch_quota=Quota(10000))
        base.update(kw)
        return hosted_dispatch.Deps(**base)

    def dispatch_request(self, client, shift, op=None, **change):
        s = self.item(client, shift)['scope']
        op = op or (self.last.get((client, shift)) or {}).get('operation_id') or 'no-operation-yet'
        receipt = self.outbox(client, shift).get(op, {}).get('receipt') or {}
        n = next(self.dispatch_n)
        body = dict({k: s[k] for k in hosted_dispatch.SCOPE}, dispatch_id='rehearsal-dispatch-%04d' % n, mode='dispatch', timestamp=self.clock(),
                    nonce='rehearsal-dispatch-nonce-%06d' % n, recipient='Lauren', operation_id=op,
                    **{k: receipt.get(k) or 'unknown' for k in hosted_dispatch.ARTIFACT if k != 'operation_id'})
        body.update(change)
        return body

    def hosted_dispatch(self, client, shift, headers=ALICE, deps=None, op=None, **change):
        return hosted_dispatch.handle('POST', headers, self.dispatch_request(client, shift, op, **change), deps or self.dispatch_deps())

    def approve_dispatch(self, principals=('alice',), deps=None, **record):
        """A FAKE approver approves exactly the dispatch digest a dry run reports (TEST ONLY; not an approval system)."""
        client, tenant = next(iter(self.tenants.items()))
        code, dry = self.hosted_dispatch(client, tenant.spec['shifts'][0], deps=deps, mode='dry_run')
        assert code == 200 and dry['dispatch_digest'], dry
        self.approvals.records[dry['dispatch_digest']] = dict(dict(snapshot_digest=dry['dispatch_digest'], action=hosted_dispatch.ACTION,
                                                                    approved_by='rehearsal fake approver', approved_at=DAY, principals=list(principals)), **record)
        return dry['dispatch_digest']


def texts(book, names=VISIBLE):
    return ' | '.join(str(c.value) for n in names if n in book.sheetnames for row in book[n].iter_rows() for c in row if c.value is not None)
