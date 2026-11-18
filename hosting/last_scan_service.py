"""Opt-in host-neutral scheduled worker. Live defaults are read-only Graph capture.
No scheduling installation or delivery transport is implicitly enabled.
"""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
from hosting import source_first as sf, source_first_capture as capture
from hosting.last_scan_drafts import Runner, capture_adapter, scope_key, instant

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

class ScheduledWorker:
    """Durable polling lease + Lauren-only testable outbox, using transaction store.
    Requires host calling tick periodically; never installs a cron.
    Delivery sender must reconcile idempotency_key with provider before retry.
    """
    def __init__(self,transaction,fetch,render,*,clock=sf.now,enabled=False,lease_seconds=300):
        self.transaction=transaction;self.fetch=fetch;self.render=render
        self.clock=clock;self.enabled=enabled;self.lease_seconds=lease_seconds

    def tick(self,scope,settings):
        if not self.enabled:return dict(status='disabled')
        key=sf.digest(scope_key(scope));token=str(uuid4());now=self.clock()
        with self.transaction('lease:'+key) as lease:
            if lease.get('expires') and instant(lease['expires'])>instant(now):
                return dict(status='busy')
            lease.update(token=token,expires=(instant(now)+timedelta(seconds=self.lease_seconds)).isoformat())
        def owned():
            with self.transaction('lease:'+key) as lease:
                if lease.get('token')!=token or instant(lease['expires'])<=instant(self.clock()):
                    raise RuntimeError('Lease lost; no dispatch.')
        try:
            snap=self.fetch(scope,settings);owned()
            def render(snapshot,op):
                owned()
                result=self.render(snapshot,op)
                owned()
                return result
            if hasattr(self.render,'render_version'):render.render_version=self.render.render_version
            result=Runner(self.transaction,lambda s:snap,render,enabled=True).poll(scope,self.clock())
            if result['status']=='draft_ready':
                # Refresh through the same timer before enqueue. Edits, deletion
                # and late uploads supersede a rendered draft; never send it as current.
                fresh=self.fetch(scope,settings);owned()
                checked=Runner(self.transaction,lambda s:fresh,render,enabled=True).poll(scope,self.clock())
                if checked['operation_id']!=result['operation_id']:
                    self._supersede(key,checked['operation_id'])
                    return dict(status='superseded',operation_id=result['operation_id'])
                with self.transaction(key) as record:
                    receipt=record['state']['receipts'][result['operation_id']]['receipt']
                self._enqueue(key,result['operation_id'],receipt)
            else:
                self._supersede(key,result.get('operation_id'))
            return result
        except Exception as error:
            with self.transaction('health:'+key) as health:
                health.update(status='error',error_type=type(error).__name__,at=self.clock())
            return dict(status='error',error_type=type(error).__name__)
        finally:
            with self.transaction('lease:'+key) as lease:
                if lease.get('token')==token:lease['expires']=self.clock()

    def _supersede(self,key,current):
        with self.transaction('outbox:'+key) as box:
            for op,entry in box.get('entries',{}).items():
                if op!=current and entry['status'] in ('pending','sent','uncertain'):
                    entry['status']='superseded'
                    entry['superseded_by']=current

    def _enqueue(self,key,op,receipt):
        with self.transaction('outbox:'+key) as box:
            entries=box.setdefault('entries',{})
            for prior,entry in entries.items():
                if prior!=op and entry['status'] in ('pending','sent','uncertain'):
                    entry.update(status='superseded',superseded_by=op)
            entries.setdefault(op,dict(status='pending',audience='Lauren',receipt=receipt))

    def dispatch_test(self,scope,op,sender):
        """Test transport only. No network sender is supplied in this module.
        Sender accepts op idempotency key; uncertain outcome is reconcile-only.
        """
        key=sf.digest(scope_key(scope))
        with self.transaction('outbox:'+key) as box:
            entry=box.get('entries',{}).get(op)
            if not entry or entry['status']!='pending':return dict(status='not_pending')
            entry['status']='uncertain';payload=deepcopy(entry)
        try:
            ack=sender(idempotency_key=op,audience='Lauren',payload=payload)
            if not isinstance(ack,dict) or ack.get('operation_id')!=op:
                raise ValueError('Matching delivery acknowledgment required.')
        except Exception:return dict(status='uncertain')
        with self.transaction('outbox:'+key) as box:
            entry=box['entries'][op]
            if entry['status']!='uncertain':
                entry['ack']=ack
                entry['delivery_status']='sent'
                return dict(status='superseded',delivery_status='sent')
            entry.update(status='sent',ack=ack)
        return dict(status='sent')
