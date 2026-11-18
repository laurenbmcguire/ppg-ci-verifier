"""Shared synthetic scenarios for the durable last-scan worker. Run against SQLite and, separately, PostgreSQL.
Synthetic sources only: nothing here reads SharePoint or any live system."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hosting import source_first as sf
from hosting.last_scan_drafts import scope_key
from hosting.last_scan_service import ScheduledWorker
from hosting.last_scan_store import claim
from source_first_fixture import SCOPE


class Crash(BaseException):
    """The process died here (not an ordinary error: nothing gets to handle it)."""


class Env:
    def __init__(self, store, zone='America/Chicago', shift='1'):
        self.store = store
        self.now = '2026-11-10T21:00:00Z'
        self.rows = [dict(id='1', scan_at='2026-11-10T20:30:00Z')]
        self.photos, self.blobs, self.extra = [], {}, {}
        self.fetches, self.renders, self.published, self.before_fetch = 0, [], {}, {}
        self.scope = scope_key(dict(SCOPE, client_id='client', shift=shift, zone=zone))
        self.key = sf.digest(self.scope)

    def clock(self):
        return self.now

    def fetch(self, scope, settings):
        self.fetches += 1
        hook = self.before_fetch.pop(self.fetches, None)
        if hook:
            hook()
        return dict(dict(scope=scope, rows=deepcopy(self.rows), captured_at=self.now, terminal=True,
                         source_capture=dict(photos=deepcopy(self.photos)), photo_blobs=dict(self.blobs)), **self.extra)

    def render(self, snapshot, op):
        self.renders.append(op)
        self.published[op] = dict(operation_id=op, workbook_sha256='w' + op[:8], checklist_sha256='c' + op[:8])
        return dict(self.published[op])

    def worker(self, render=None, **kw):
        return ScheduledWorker(self.store, self.fetch, render or self.render, clock=self.clock, enabled=True, **kw)

    def record(self, key):
        with self.store.transaction(key) as r:
            return deepcopy(r)

    def state(self):
        return self.record(self.key).get('state')

    def entries(self):
        return self.record('outbox:' + self.key).get('entries') or {}


class Publisher:
    """Renderer that can be asked what it already published."""
    def __init__(self, env, authoritative=False, version=None):
        self.env, self.lookup_is_authoritative, self.version = env, authoritative, version
        if version is not None:
            self.render_version = lambda: self.version

    def __call__(self, snapshot, op):
        return self.env.render(snapshot, op)

    def lookup(self, op):
        found = self.env.published.get(op)
        return dict(found) if found else None


DUE = '2026-11-10T21:35:00Z'       # the only scan was 20:30, so the quiet hour has passed
AFTER_LEASE = '2026-11-10T21:41:00Z'  # a 300 second lease taken at 21:35 has run out


def stale_worker_cannot_enqueue(store):
    """A stalls after rendering; B takes the scope over and finishes. A must commit nothing."""
    env = Env(store); pub = Publisher(env); env.now = DUE
    a, b, out = env.worker(pub, owner='A'), env.worker(pub, owner='B'), {}
    def takeover():
        env.now = AFTER_LEASE
        out['b'] = b.tick(env.scope, {})
    env.before_fetch[2] = takeover          # A's recheck capture, after its render
    out['a'] = a.tick(env.scope, {})
    entries = env.entries()
    assert out['a'] == dict(status='lease_lost'), out
    assert out['b']['status'] == 'draft_ready', out
    assert list(entries) == [out['b']['operation_id']] and len(env.renders) == 1
    assert entries[out['b']['operation_id']]['fence'] == 2 and entries[out['b']['operation_id']]['status'] == 'pending'
    assert env.state()['receipts'][out['b']['operation_id']]['fence'] == 2
    assert env.record('lease:' + env.key)['fence'] == 2
    return out


def stale_worker_and_changed_content(store):
    """While A is stalled a late scan arrives and B observes it. A's rendered draft is for superseded content AND
    its lease is gone: nothing may reach the outbox."""
    env = Env(store); pub = Publisher(env); env.now = DUE
    a, b, out = env.worker(pub, owner='A'), env.worker(pub, owner='B'), {}
    def takeover():
        env.now = AFTER_LEASE
        env.rows.append(dict(id='2', scan_at='2026-11-10T21:38:00Z'))
        out['b'] = b.tick(env.scope, {})
    env.before_fetch[2] = takeover
    out['a'] = a.tick(env.scope, {})
    state = env.state()
    assert out['a'] == dict(status='lease_lost') and out['b']['status'] == 'waiting', out
    assert env.entries() == {}
    assert state['operation_id'] == out['b']['operation_id'] != env.renders[0]
    assert state['receipts'][env.renders[0]]['state'] == 'uncertain'   # A's claim stays for reconciliation
    assert state['due_at'].startswith('2026-11-10T22:41')              # a full hour from when the late scan was seen
    return out


def changed_between_render_and_enqueue(store):
    """Lease still held, but the source changed after rendering: the receipt is kept, the outbox gets nothing."""
    env = Env(store); env.now = DUE
    env.before_fetch[2] = lambda: env.rows.append(dict(id='2', scan_at='2026-11-10T21:34:00Z'))
    out = env.worker().tick(env.scope, {})
    state = env.state()
    assert out['status'] == 'superseded' and out['superseded_by'] == state['operation_id'], out
    assert env.entries() == {} and state['status'] == 'waiting'
    assert state['receipts'][out['operation_id']]['state'] == 'draft_ready'
    return out


def concurrent_ticks_render_once(store, workers=6):
    env = Env(store); env.now = DUE
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda _: env.worker().tick(env.scope, {}), range(workers)))
    statuses = [r['status'] for r in results]
    assert set(statuses) <= {'draft_ready', 'busy'} and 'draft_ready' in statuses, statuses
    assert len(env.renders) == 1 and len(env.entries()) == 1, (env.renders, env.entries())
    return statuses


def concurrent_claims_one_winner(store, contenders=12):
    with ThreadPoolExecutor(contenders) as pool:
        got = list(pool.map(lambda i: claim(store, 'scope-x', DUE, 300, 'w%d' % i), range(contenders)))
    winners = [g for g in got if g]
    assert len(winners) == 1 and winners[0]['fence'] == 1, got
    return winners[0]


def atomic_is_all_or_nothing(store):
    with store.atomic(['a', 'b']) as r:
        r['a']['n'] = 1; r['b']['n'] = 1
    try:
        with store.atomic(['a', 'b']) as r:
            r['a']['n'] = 2; r['b']['n'] = 2
            raise Crash()
    except Crash:
        pass
    with store.atomic(['a', 'b']) as r:
        assert r['a'] == {'n': 1} and r['b'] == {'n': 1}, r


def counter_is_serialized(store, threads=8, each=15):
    def bump(_):
        for _ in range(each):
            with store.atomic(['counter', 'other']) as r:
                r['counter']['n'] = int(r['counter'].get('n') or 0) + 1
    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(bump, range(threads)))
    with store.transaction('counter') as r:
        assert r['n'] == threads * each, r


def blobs_are_content_addressed(store):
    body = b'synthetic photo bytes' * 1000
    first, second = store.put_blob(body), store.put_blob(body)
    assert first == second == sf.digest(body) and store.get_blob(first) == body
    try:
        store.get_blob('0' * 64)
    except KeyError:
        return
    raise AssertionError('missing blob must not be invented')


ALL = [stale_worker_cannot_enqueue, stale_worker_and_changed_content, changed_between_render_and_enqueue,
       concurrent_ticks_render_once, concurrent_claims_one_winner, atomic_is_all_or_nothing, counter_is_serialized,
       blobs_are_content_addressed]
