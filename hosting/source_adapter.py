"""Read-only conversion of a hash-checked private source envelope to report schema 2.

This does not fetch SharePoint, infer missing quantities, or assert a complete
shift. Original source metadata stays bound by the envelope hash.
"""
import copy
import re
from hosting.report_workflow import WorkflowError,parse,fingerprint,validate_manifest


def convert_private_envelope(value):
    envelope=parse(value)
    if envelope.get('schema_version')!='private-sharepoint-envelope/1':
        raise WorkflowError('Unsupported private source envelope schema.')
    claimed=envelope.get('manifest_sha256')
    raw={key:value for key,value in envelope.items() if key!='manifest_sha256'}
    if not isinstance(claimed,str) or fingerprint(raw)!=claimed:
        raise WorkflowError('Source envelope hash mismatch; no report was imported.')
    source=copy.deepcopy(envelope.get('source_snapshot'))
    outer=envelope.get('scope')
    if not isinstance(source,dict) or not isinstance(outer,dict):raise WorkflowError('Source envelope scope is missing.')
    target=source.get('scope')
    if not isinstance(target,dict):raise WorkflowError('Verifier source scope is missing.')
    daily=outer.get('report_granularity')=='daily'
    if daily and ('shift_key' in outer or 'shift_id' in outer or target.get('report_granularity')!='daily' or 'shift_id' in target):
        raise WorkflowError('Daily envelopes must not pretend to identify a physical shift.')
    if not daily and ('report_granularity' in outer or 'report_granularity' in target):
        raise WorkflowError('Envelope and source granularity disagree.')
    mapping=(('site_id','site_id'),('list_id','list_id'),('business_date','report_date'))+((('report_granularity','report_granularity'),) if daily else (('shift_key','shift_id'),))
    for old,new in mapping:
        if outer.get(old)!=target.get(new):raise WorkflowError('Envelope and source scope disagree.')
    # Historical physical envelopes omit these identities. When supplied at
    # either outer level they must never contradict the exact target identity.
    for container in (outer,envelope):
        for key in ('job_id','client_id'):
            if key in container and container[key]!=target.get(key):
                raise WorkflowError('Envelope and source '+key+' disagree.')
    for key in ('report_date','shift_id'):
        if key in outer and outer[key]!=target.get(key):raise WorkflowError('Envelope and source '+key+' disagree.')
    fields=envelope.get('field_definitions')
    if source.get('field_definitions_hash')!=fingerprint(fields):raise WorkflowError('Envelope field mapping hash mismatch.')
    if not isinstance(envelope.get('source_revision'),str) or not re.fullmatch('[0-9a-f]{64}',envelope['source_revision']) or envelope['source_revision']!=source.get('source_revision'):raise WorkflowError('Envelope and snapshot source revisions disagree.')
    source['envelope_hash']=claimed
    blockers=source.get('evidence_blockers',[])
    if not isinstance(blockers,list) or not all(isinstance(v,str) for v in blockers):raise WorkflowError('Invalid nested source evidence blockers.')
    blockers=list(blockers)
    for section in ('eligibility','discovery'):
        details=envelope.get(section,{})
        if not isinstance(details,dict):raise WorkflowError('Declared source evidence must be an object.')
        if section in envelope:
            if section=='eligibility' and details.get('candidate_eligible') is not True: blockers.append('source_candidate_eligibility_unconfirmed')
            if section=='discovery':
                for key in ('discovered','accessible','mapped'):
                    if details.get(key) is not True: blockers.append('source_discovery_'+key+'_unconfirmed')
            values=details.get('blockers',[])
            if not isinstance(values,list) or not all(isinstance(v,str) for v in values): raise WorkflowError('Invalid source evidence blocker list.')
            blockers.extend(values)
    source['evidence_blockers']=sorted(set(blockers))
    manifest=dict(schema_version=2,synthetic=False,report_id=envelope.get('report_id'),version=envelope.get('version'),
        scope=copy.deepcopy(target),fields=copy.deepcopy(fields),report=copy.deepcopy(envelope.get('report')),
        source_snapshot=source,generated_at=envelope.get('generated_from_snapshot_at'))
    return validate_manifest(manifest)
