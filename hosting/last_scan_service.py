"""Opt-in host-neutral scheduled worker. Live defaults are read-only Graph capture.
No scheduling installation or delivery transport is implicitly enabled.
"""
import base64
from copy import deepcopy
from hosting import source_first as sf, source_first_capture as capture
from hosting.last_scan_drafts import capture_adapter, observe, scope_key
from hosting.last_scan_store import LeaseLost, claim, fenced, release

def discover(config, *, fetch=capture.graph_fetch):
    """Terminal Graph discovery over explicitly configured sites, never job-name join.
    Lists with no verified assignment return issues; inactivity is never inferred
    from scan silence. Active-job flags must be supplied by authoritative registry.
    """
    scopes=[];issues=[];seen=set()
    for site in config['sites']:
        pages=[]
        lists=capture.collection('/sites/'+site+'/lists','lists',pages,fetch)
        for item in lists:
            key=(site,item['id'])
            assignment=config['assignments'].get('|'.join(key))
            if assignment is None:
                issues.append(dict(code='unmapped_list',site_id=site,list_id=item['id']))
                continue
            seen.add(key)
            if assignment.get('active') is False:continue
            if assignment.get('active') is not True or assignment.get('verified') is not True:
                issues.append(dict(code='unverified_active_assignment',site_id=site,list_id=item['id']))
                continue
            for day in assignment['business_dates']:
                for shift in assignment['recorded_shifts']:
                    scope=scope_key(dict(site_id=site,list_id=item['id'],job=assignment['job'],
                        client_id=assignment['client_id'],business_date=day,shift=shift,zone=assignment['zone']))
                    scopes.append(dict(scope=scope,settings=deepcopy(assignment)))
    for key in config['assignments']:
        if tuple(key.split('|')) not in seen:
            issues.append(dict(code='configured_source_not_discovered',source=key))
    return scopes,issues

def source_adapter(scope,settings,*,capture_fn=capture.capture):
    if not settings.get('timestamp_semantics_verified'):
        raise ValueError('Actual scan timestamp meaning must be verified, not inferred from field name.')
    return capture_adapter(scope,scan_field=settings['scan_field'],shift_field=settings['shift_field'],
        capture_fn=capture_fn,not_before=settings.get('not_before',{}).get(scope['business_date']+'|'+scope['shift']))

BINDING=('client_id','site_id','list_id','job')

def binding(value):
    """The exact client / site / list / job a renderer is allowed to serve. No normalising, no partial match."""
    out={k:value.get(k) for k in BINDING}
    if any(not isinstance(v,str) or not v.strip() or v!=v.strip() for v in out.values()):
        raise ValueError('Exact client, site, list and job are required.')
    return out

class RendererRouter:
    """Exactly one publisher per client/site/list/job. There is no default publisher: an unrouted scope is refused.

    Every publisher carries its own ``binding`` and a ``render_version()`` covering its mapping, header and template,
    so that configuration is part of each operation ID and a receipt is never reused after it changes."""
    def __init__(self,publishers):
        self._by={}
        for p in publishers:
            key=sf.digest(binding(getattr(p,'binding',None) or {}))
            if key in self._by:raise ValueError('Two publishers claim the same client/site/list/job.')
            if not callable(p) or not callable(getattr(p,'render_version',None)):
                raise TypeError('A publisher must be callable and report its render_version().')
            self._by[key]=p
    def for_scope(self,scope):
        found=self._by.get(sf.digest(binding(scope)))
        if found is None:raise LookupError('No verified renderer is configured for this exact client/site/list/job.')
        return found

class ScheduledWorker:
    """One scope per tick, under a fenced lease, on a store with ``atomic(keys)`` (hosting.last_scan_store).

    Requires a host calling tick periodically; never installs a cron. Draft only: nothing is approved or sent.

    What is committed together, in ONE database transaction that also re-reads the lease:
      * observing a capture: timer state, the render claim for a due operation, and superseding older outbox entries;
      * finishing a draft: the pre-outbox recheck of the source, the check that the rendered operation is still the
        current one, the render receipt, and the outbox entry.
    A worker whose lease was taken over or expired cannot commit either step (LeaseLost): it writes nothing.

    A render whose outcome is unknown is never repeated blindly. It is looked up by its operation ID; only a
    publisher that declares its lookup authoritative may render the same operation again.
    Photo bytes and the captured snapshot are stored as content-addressed blobs, not in the state row.
    """
    def __init__(self,transaction,fetch,render,*,clock=sf.now,enabled=False,lease_seconds=300,owner=None):
        self.store=getattr(transaction,'__self__',transaction)
        if not hasattr(self.store,'atomic') or not hasattr(self.store,'put_blob'):
            raise TypeError('A store with atomic multi-record transactions is required (hosting.last_scan_store).')
        self.transaction=self.store.transaction;self.fetch=fetch;self.render=render
        self.clock=clock;self.enabled=enabled;self.lease_seconds=lease_seconds;self.owner=owner

    # ---- one scope
    def tick(self,scope,settings):
        if not self.enabled:return dict(status='disabled')
        s=scope_key(scope);key=sf.digest(s)
        bound=getattr(self.render,'binding',None)
        if bound is not None and bound!=binding(s):   # before any lease, read or render
            return dict(status='error',error_type='RendererBindingMismatch')
        held=claim(self.store,key,self.clock(),self.lease_seconds,self.owner)
        if not held:return dict(status='busy')
        try:
            return self._run(s,key,settings,held)
        except LeaseLost:
            return dict(status='lease_lost')     # nothing of ours was committed; the current holder carries on
        except Exception as error:
            self._health(key,error)
            return dict(status='error',error_type=type(error).__name__)
        finally:
            try:release(self.store,key,held,self.clock())
            except Exception:pass                # the lease simply runs out

    def _capture(self,s,settings):
        """One source capture -> (slim snapshot for state, full snapshot for rendering, blob address of the slim one)."""
        full=dict(self.fetch(s,settings))
        if hasattr(self.render,'render_version'):full['render_version']=self.render.render_version()
        slim={k:v for k,v in full.items() if k!='photo_blobs'}
        refs={}
        for name,encoded in sorted((full.get('photo_blobs') or {}).items()):
            refs[name]=self.store.put_blob(base64.b64decode(encoded,validate=True))
        slim['photo_blob_refs']=refs
        return slim,full,self.store.put_blob(sf.canonical(slim))

    @staticmethod
    def _stub(snapshot,ref):
        return dict(captured_at=snapshot['captured_at'],ref=ref,rows=len(snapshot.get('rows') or []),
                    not_before=snapshot.get('not_before'),render_version=snapshot.get('render_version'))

    @staticmethod
    def _supersede_in(box,current,now=None):
        for op,entry in (box.get('entries') or {}).items():
            if op!=current and entry['status'] in ('pending','sent','uncertain'):
                entry['status']='superseded';entry['superseded_by']=current
                if now:entry['superseded_at']=now

    def _run(self,s,key,settings,held):
        state_key,box_key=key,'outbox:'+key
        slim,full,ref=self._capture(s,settings)
        with fenced(self.store,key,held,self.clock,[state_key,box_key]) as records:
            now=self.clock()
            current=observe(records[state_key].get('state'),s,slim,now)
            op=current['operation_id'];status=current['status']
            if status=='due':
                current['receipts'][op]=dict(state='uncertain',claimed_at=now,fence=held['fence'])
                current['status']='uncertain'    # a crash after this point is reconcile-only
            current['snapshot']=self._stub(slim,ref)
            records[state_key]['state']=current
            self._supersede_in(records[box_key],op,now)
            public={k:v for k,v in current.items() if k not in ('receipts','snapshot')}
            if status=='draft_ready':            # same content as a finished draft: exact retry, only make sure it is queued
                receipt=deepcopy(current['receipts'][op]['receipt'])
                self._queue(records[box_key],op,receipt,now,held['fence'])
                return dict(status='draft_ready',operation_id=op,receipt=receipt)
        if status in ('waiting','no_scans'):
            return public
        if status=='uncertain':
            receipt=self._reconcile(op,full)
            if receipt is None:
                return dict(status='uncertain',operation_id=op,reconcile='render_outcome_unknown')
        else:
            try:
                receipt=self._checked(self.render(full,op),op)
            except Exception as error:
                self._health(key,error,op)
                return dict(status='uncertain',operation_id=op,error_type=type(error).__name__)
        return self._finish(s,key,settings,held,op,receipt)

    def _checked(self,receipt,op):
        if not isinstance(receipt,dict) or receipt.get('operation_id')!=op or not receipt.get('workbook_sha256') or not receipt.get('checklist_sha256'):
            raise ValueError('Renderer must return matching workbook and checklist receipt.')
        bound=getattr(self.render,'binding',None)
        if bound is not None and receipt.get('binding')!=bound:
            raise ValueError('Receipt belongs to another client/site/list/job.')
        if receipt.get('render_version') is not None and hasattr(self.render,'render_version') and receipt['render_version']!=self.render.render_version():
            raise ValueError('Receipt was made with another mapping/header/template.')
        return receipt

    def _reconcile(self,op,full):
        """The earlier render of this operation has no recorded outcome. Ask the publisher; never just run it again."""
        lookup=getattr(self.render,'lookup',None)
        if lookup is None:return None
        found=lookup(op)                          # raises on an interrupted or altered artifact: stays uncertain
        if found:return self._checked(found,op)
        if getattr(self.render,'lookup_is_authoritative',False) is True:
            return self._checked(self.render(full,op),op)   # the publisher vouches that nothing was written
        return None

    def _finish(self,s,key,settings,held,op,receipt):
        """Recheck the source, then commit receipt + outbox only if this operation is still current and the lease is ours."""
        state_key,box_key=key,'outbox:'+key
        slim,_,ref=self._capture(s,settings)      # edits, deletions and late uploads supersede a rendered draft
        with fenced(self.store,key,held,self.clock,[state_key,box_key]) as records:
            now=self.clock()
            checked=observe(records[state_key].get('state'),s,slim,now)
            current=checked['operation_id']
            checked['receipts'][op]=dict(state='draft_ready',receipt=receipt,finished_at=now,fence=held['fence'])
            checked['snapshot']=self._stub(slim,ref)
            ready=current==op and checked['status'] in ('uncertain','draft_ready','due')   # never before the timer
            if ready:checked['status']='draft_ready'
            records[state_key]['state']=checked
            box=records[box_key]
            self._supersede_in(box,current,now)
            if current!=op:
                return dict(status='superseded',operation_id=op,superseded_by=current)
            if not ready:
                return {k:v for k,v in checked.items() if k not in ('receipts','snapshot')}
            self._queue(box,op,receipt,now,held['fence'])
            return dict(status='draft_ready',operation_id=op,receipt=receipt)

    @staticmethod
    def _queue(box,op,receipt,now=None,fence=None):
        """One internal outbox entry per operation. Content that returns to an earlier version makes that
        operation current again: its entry goes back to pending only if no delivery was ever attempted."""
        entries=box.setdefault('entries',{})
        entry=entries.get(op)
        if entry is None:
            entries[op]=dict(status='pending',audience='Lauren',receipt=receipt,enqueued_at=now,fence=fence)
        elif entry['status']=='superseded' and not entry.get('delivery_status'):
            entry.update(status='pending',requeued_at=now)
            entry.pop('superseded_by',None);entry.pop('superseded_at',None)

    def _health(self,key,error,op=None):
        try:
            with self.transaction('health:'+key) as health:
                health.update(status='error',error_type=type(error).__name__,at=self.clock(),operation_id=op)
        except Exception:pass

    # ---- outbox helpers (single record; used by tests and by reconciliation tooling)
    def _supersede(self,key,current):
        with self.transaction('outbox:'+key) as box:
            self._supersede_in(box,current)

    def _enqueue(self,key,op,receipt):
        with self.transaction('outbox:'+key) as box:
            self._supersede_in(box,op)
            self._queue(box,op,receipt)

    def dispatch_test(self,scope,op,sender):
        """Test transport only. No network sender is supplied in this module.

        ``delivery_status`` is recorded apart from ``status``: once an attempt starts it is 'uncertain' until a
        matching acknowledgment arrives, and it stays 'uncertain' (for reconciliation with the provider by the
        operation ID) when the sender fails, also when the entry was superseded while the attempt was in flight.
        """
        key=sf.digest(scope_key(scope))
        with self.transaction('outbox:'+key) as box:
            entry=box.get('entries',{}).get(op)
            if not entry or entry['status']!='pending':return dict(status='not_pending')
            entry.update(status='uncertain',delivery_status='uncertain',delivery_attempted_at=self.clock())
            payload=deepcopy(entry)
        try:
            ack=sender(idempotency_key=op,audience='Lauren',payload=payload)
            if not isinstance(ack,dict) or ack.get('operation_id')!=op:
                raise ValueError('Matching delivery acknowledgment required.')
        except Exception as error:
            with self.transaction('outbox:'+key) as box:
                entry=box['entries'][op]
                entry.update(delivery_status='uncertain',delivery_error_type=type(error).__name__)
                superseded=entry['status']!='uncertain'
            return dict(status='superseded' if superseded else 'uncertain',delivery_status='uncertain')
        with self.transaction('outbox:'+key) as box:
            entry=box['entries'][op]
            entry.update(ack=ack,delivery_status='sent')
            if entry['status']!='uncertain':
                return dict(status='superseded',delivery_status='sent')
            entry['status']='sent'
        return dict(status='sent')
