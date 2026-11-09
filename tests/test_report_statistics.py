"""Synthetic evidence-only regressions for the shared Overview/KPI contract."""
import copy
from datetime import date
from unittest.mock import patch

import pytest
from hosting import kpis, overview, report_statistics as stats

END = date(2026, 11, 8)


def report(key='a', job='TEST-1000', day='2026-11-06', at='2026-11-07T10:00:00+00:00',
           issues=2, high=1, rejected=10, inspected=100, certified=90, **extra):
    r = dict(file_hash=key, filename=f'{job} {day}.xlsx', report_identity=dict(project_number=job, report_date=day),
             received_at=at, record_count=2,
             totals={f: dict(total=v, known_sum=v) for f,v in
                     (('inspected', inspected), ('rejected', rejected), ('certified', certified))},
             fake_review=dict(issues=[dict(code='missing_cell', severity='high' if n < high else 'medium')
                                      for n in range(issues)],
                              status='hold' if high else 'check' if issues else 'clear',
                              approval_current=False, coverage=dict(truncated=False)))
    r.update(extra)
    return r


@pytest.fixture(autouse=True)
def reviews():
    with patch.object(stats, 'summarize', side_effect=lambda r: r['fake_review']):
        yield


def both(rows, **kw):
    client_of = lambda j: 'test'
    o = overview.build(rows, 7, END, client_of)
    k = kpis.build(rows, 7, END, [dict(key='test',client='Test')], client_of, **kw)
    return o, k


def test_same_all_shared_totals_and_categories():
    rows = [report('old'), report('new', at='2026-11-07T11:00:00+00:00', issues=1),
            report('other', job='TEST-2000', rejected=None)]
    o, k = both(rows)
    assert o['totals'] == k['totals']
    assert [(x['reason'],x['count']) for x in o['categories']] == [(x['kind'],x['count']) for x in k['categories']]
    assert o['totals']['caught'] == 4
    assert o['totals']['current_issues'] == 3
    assert o['totals']['rejected'] is None and o['totals']['rate'] is None
    assert o['totals']['rejected_known'] == 10
    assert o['totals']['rejected_missing_reports'] == 1


def test_categories_use_same_deduplicated_job_days_at_every_level():
    rows = [report('old'), report('new',at='2026-11-07T11:00:00+00:00',issues=8)]
    for kwargs in ({},dict(client='test'),dict(client='test',job='TEST-1000'),dict(job='TEST-1000')):
        _,k = both(rows,**kwargs)
        assert sum(x['count'] for x in k['categories']) == k['totals']['caught'] == 2
    _,k = both(rows,client='wrong',job='TEST-1000')
    assert not k['categories'] and k['totals']['reports'] == 0


def test_duplicate_input_version_is_not_double_counted():
    r=report()
    _,k=both([r,copy.deepcopy(r)])
    assert k['totals']['uploads']==1
    assert k['statistics']['coverage']['duplicate_versions']==1


def test_missing_and_partial_are_not_zero():
    r=report(rejected=None)
    r['totals']['rejected']['known_sum']=3
    r['totals']['rejected']['missing_count']=1
    _,k=both([r])
    t=k['totals']
    assert (t['rejected'],t['rejected_known'],t['rejected_missing_reports'],t['rate'])==(None,3,1,None)
    assert k['trend'][-3]['rejected'] is None


def test_known_zero_has_zero_rate_but_empty_and_missing_do_not():
    _,zero=both([report(rejected=0,certified=100)])
    _,empty=both([])
    assert zero['totals']['rejected']==0 and zero['totals']['rate']==0
    assert empty['totals']['reports']==0 and empty['totals']['rejected'] is None and empty['totals']['rate'] is None


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,True,'invalid',10**16])
def test_invalid_quantities_fail_closed(value):
    _,k=both([report(rejected=value)])
    assert k['totals']['rejected'] is None and k['totals']['rate'] is None


def test_row_counts_never_added_to_parts():
    r=report()
    r['totals']['rejected']['unit']='rows'
    r['totals']['certified']['unit']='rows'
    _,k=both([r,report('b',job='TEST-2000')])
    assert k['totals']['rejected'] is None
    assert k['totals']['rejected_known']==10
    assert k['totals']['rejected_incompatible_reports']==1
    assert k['totals']['rate'] is None


def test_impossible_reject_ratio_not_shown():
    _,k=both([report(rejected=101),report('b',job='TEST-2000',rejected=0)])
    assert k['totals']['rate'] is None and k['totals']['rate_invalid_reports']==1


def test_inconsistent_total_and_known_sum_not_complete():
    r=report()
    r['totals']['rejected']['known_sum']=9
    _,k=both([r])
    assert k['totals']['rejected'] is None


def test_correction_record_and_disappearing_findings_do_not_prove_prevention():
    a=report()
    b=report('b',issues=0,high=0,at='2026-11-07T11:00:00+00:00',
             fixed_from=dict(file_hash='a',filename=a['filename'],changes=[dict(cell='A1')]))
    _,k=both([a,b])
    t=k['totals']
    assert (t['corrected'],t['changes'],t['caught'],t['current_issues'])==(1,1,2,0)
    assert t['stopped'] is None and t['reached'] is None
    assert not t['client_outcomes_known']


def test_recorded_send_is_not_client_receipt_or_at_send_findings():
    _,k=both([report(sent=[dict(sent=True,at='2026-11-07T15:00:00+00:00')])])
    t=k['totals']
    assert (t['sent'],t['on_time'],t['on_time_pct'],t['turnaround'])==(1,1,100,5)
    assert t['reached'] is None and t['stopped'] is None
    assert t['turnaround_reports']==1


@pytest.mark.parametrize('send',[dict(sent=False,at='2026-11-07T12:00:00+00:00'),dict(mode='draft'),dict(sent='true')])
def test_drafts_failed_and_nonboolean_send_not_success(send):
    _,k=both([report(sent=[send])])
    assert k['totals']['sent']==0 and k['totals']['on_time_pct'] is None


@pytest.mark.parametrize('at',[None,'bad','2026-11-07T12:00:00','2026-11-06T10:00:00+00:00'])
def test_invalid_ambiguous_or_before_upload_send_timing_unknown(at):
    _,k=both([report(sent=[dict(sent=True,at=at)])])
    assert k['totals']['sent']==1
    assert k['totals']['on_time_pct'] is None and k['totals']['turnaround'] is None


def test_first_recorded_send_chronological_not_list_order_and_chicago_deadline():
    sends=[dict(sent=True,at='2026-11-08T06:01:00+00:00'),dict(sent=True,at='2026-11-08T05:59:00+00:00')]
    _,k=both([report(sent=sends)])
    assert k['totals']['on_time_pct']==100
    assert k['totals']['turnaround']==20.0
    _,late=both([report(sent=sends[:1])])
    assert late['totals']['on_time_pct']==0


def test_stale_approval_never_beats_new_version():
    old=report('old',approval=dict(by='Reviewer'))
    new=report('new',at='2026-11-07T11:00:00+00:00',inspected=120)
    _,k=both([old,new])
    assert k['records'][0]['selected_file_hash']=='new'
    old['fake_review']['approval_current']=True
    _,k=both([old,new])
    assert k['records'][0]['selected_file_hash']=='old'


def test_timestamp_offsets_used_for_version_order():
    a=report('a',at='2026-11-07T09:00:00-06:00')
    b=report('b',at='2026-11-07T14:00:00+00:00')
    _,k=both([a,b])
    assert k['records'][0]['first_file_hash']=='b'
    assert k['records'][0]['selected_file_hash']=='a'


def test_truncated_findings_disclose_listing_coverage():
    r=report()
    r['fake_review']['coverage']['truncated']=True
    _,k=both([r])
    assert k['totals']['caught']==2
    assert not k['totals']['caught_complete'] and not k['totals']['current_issues_complete']


def test_missing_identity_outside_and_bad_dates_disclosed():
    rows=[report('bad',day='bad'),report('missing',job=''),report('old',day='2026-10-01'),report()]
    _,k=both(rows)
    assert k['totals']['reports']==1
    assert k['statistics']['coverage']['excluded_identity']==2
    assert k['statistics']['coverage']['outside_period']==1


def assignment(site='s1',status='manual',**kw):
    return dict(status=status,job='ASSIGNED-2000',business_date='2026-11-05',client_key='assigned',
                site_id=site,list_id='list',**kw)


def test_exact_assignment_controls_source_client_job_and_day():
    r=report(_statistics_assignment=assignment())
    _,k=both([r],client='assigned',job='ASSIGNED-2000')
    rec=k['records'][0]
    assert (rec['job'],rec['day'],rec['client'])==('ASSIGNED-2000','2026-11-05','assigned')


def test_same_job_on_two_sources_never_collapsed():
    rows=[report('a',_statistics_assignment=assignment('s1')),
          report('b',_statistics_assignment=assignment('s2'))]
    o,k=both(rows)
    assert k['totals']['reports']==2 and k['totals']['jobs']==2
    assert len(o['jobs'])==2 and o['totals']==k['totals']


@pytest.mark.parametrize('status',['ambiguous','unmatched','missing'])
def test_unresolved_assignment_not_guessed(status):
    _,k=both([report(_statistics_assignment=assignment(status=status))])
    assert k['totals']['reports']==0
    assert k['statistics']['coverage']['excluded_assignment']==1


def test_every_additive_metric_reconciles_across_clients_jobs_and_dates():
    rows=[report('a',_statistics_assignment=assignment('s1')),
          report('b',_statistics_assignment=dict(assignment('s2'),client_key='other'))]
    _,k=both(rows)
    for key in ('reports','uploads','caught','must_fix','current_issues','current_must_fix','sent','corrected','changes','hold'):
        assert k['totals'][key]==sum(x[key] for x in k['clients'])
        assert k['totals'][key]==sum(x[key] for x in k['trend'])
    assert sum(x['count'] for x in k['categories'])==k['totals']['caught']


def test_tied_uploads_deterministic_and_no_input_mutation():
    rows=[report('z'),report('a')]
    saved=copy.deepcopy(rows)
    _,a=both(rows)
    _,b=both(list(reversed(rows)))
    assert a['records']==b['records'] and rows==saved


def test_overview_365_key_and_scope_supported():
    assert overview.parse_key('overview|365|2026-11-08|dashboard')==(365,END,'dashboard')


def test_assignment_metadata_not_passed_into_approval_fingerprint():
    row=report(_statistics_assignment=assignment())
    def review(r):
        assert '_statistics_assignment' not in r
        return r['fake_review']
    with patch.object(stats,'summarize',side_effect=review):
        both([row])


def test_source_labels_do_not_split_same_exact_ids():
    a=report('a',_statistics_assignment=dict(assignment(),site='Old label'))
    b=report('b',_statistics_assignment=dict(assignment(),site='New label'))
    _,k=both([a,b])
    assert k['totals']['reports']==1
    assert k['statistics']['coverage']['competing_original_groups']==1


def test_missing_original_and_uncertain_upload_order_disclosed():
    _,k=both([report(fixed_from=dict(file_hash='not-saved'),at=None)])
    assert k['statistics']['coverage']['first_original_missing_reports']==1
    assert k['statistics']['coverage']['first_order_unknown_reports']==1
    assert not k['records'][0]['first_order_known']
    assert not k['totals']['caught_complete']
    _,dated=both([report(fixed_from=dict(file_hash='not-saved'))])
    assert dated['totals']['caught']==2
    assert not dated['totals']['caught_complete']


def test_artifacts_and_recommendations_keep_unknowns_and_evidence_limits():
    from hosting import dashboard,recommend
    o,_=both([report(rejected=None)])
    for html in (dashboard.build_overview_page(o),dashboard.build_overview_slides(o)):
        assert 'Unknown' in html and 'Known subtotal' in html
        assert 'before the client' not in html and 'before any client' not in html
        assert 'Every one found and fixed' not in html
        assert 'First-original findings (current review)' in html
        assert 'Client exposure' in html
    facts=recommend.overview_facts(o)
    assert facts['totals']['rate'] is None and facts['statistics']['definitions']['client_outcomes']
    messages=' '.join(str(x) for x in recommend.overview_rules(o))
    assert 'Overall reject rate is unknown' in messages
    assert 'every report was checked and sent' not in messages


def test_decimal_sums_are_exact_json_numbers_and_same_in_both_views():
    import json
    rows=[report('a',inspected='0.1',rejected='0.01',certified='0.09'),
          report('b',job='TEST-2000',inspected='0.2',rejected='0.02',certified='0.18')]
    o,k=both(rows)
    assert o['totals']==k['totals']
    assert k['totals']['inspected']==0.3 and k['totals']['rejected']==0.03
    assert k['totals']['certified']==0.27 and k['totals']['rate']==10
    encoded=json.dumps(k,allow_nan=False)
    assert '0.30000000000000004' not in encoded and 'Decimal' not in encoded
    from hosting import dashboard
    assert dashboard._ov_number(k['totals']['inspected'])=='0.3'
    assert 'Inspected: 0.3' in dashboard.build_overview_page(o)


def test_aggregate_supported_range_is_checked_not_only_inputs():
    rows=[report('a',inspected=10**15),report('b',job='TEST-2000',inspected=1)]
    _,k=both(rows)
    assert k['totals']['inspected'] is None
    assert k['totals']['inspected_known'] is None
    assert k['totals']['inspected_range_exceeded']
    assert k['totals']['rate'] is None


@pytest.mark.parametrize('changed',[dict(needs_person=True),dict(site_id=None),dict(list_id=None)])
def test_manual_or_label_only_scope_cannot_bypass_exact_identity(changed):
    a=dict(assignment(),**changed)
    _,k=both([report(_statistics_assignment=a)])
    assert k['totals']['reports']==0
    assert k['statistics']['coverage']['excluded_assignment']==1


def test_generic_unit_is_not_assumed_to_mean_parts():
    r=report()
    r['totals']['rejected']['unit']='units'
    _,k=both([r])
    assert k['totals']['rejected'] is None and k['totals']['rejected_incompatible_reports']==1


def test_source_labels_disambiguate_same_job():
    a=report('a',_statistics_assignment=dict(assignment('s1'),site='Plant East',client='Synthetic Client'))
    b=report('b',_statistics_assignment=dict(assignment('s2'),site='Plant West',client='Synthetic Client'))
    o,k=both([a,b],client='assigned')
    assert len({r['source_label'] for r in o['jobs']})==2
    assert len({r['source_label'] for r in k['jobs']})==2


def test_scoped_coverage_explicitly_describes_authorized_population():
    rows=[report('a',_statistics_assignment=assignment()),
          report('b',_statistics_assignment=dict(assignment(),status='ambiguous'))]
    _,k=both(rows,client='assigned')
    assert k['totals']['reports']==1
    assert k['statistics']['coverage']['excluded_assignment']==1
    assert 'before client/job filters' in k['statistics']['coverage_scope']
    assert 'not denominators' in k['statistics']['definitions']['coverage']


def test_conflicting_client_assignments_do_not_double_count_one_source_day():
    rows=[report('a',_statistics_assignment=assignment()),
          report('b',_statistics_assignment=dict(assignment(),client_key='different'))]
    _,k=both(rows)
    assert k['totals']['reports']==0
    assert k['statistics']['coverage']['conflicting_client_groups']==1


def test_child_only_cannot_measure_turnaround_from_missing_original():
    child=report(fixed_from=dict(file_hash='missing-original'),sent=[dict(sent=True,at='2026-11-07T15:00:00+00:00')])
    _,k=both([child])
    assert k['totals']['turnaround'] is None and k['totals']['turnaround_reports']==0
    assert k['totals']['on_time_pct']==100
    child['sent'][0]['at']='2026-11-07T09:00:00+00:00'
    _,bad=both([child])
    assert bad['totals']['on_time_pct'] is None


def test_unknown_order_of_originals_makes_first_original_metric_incomplete():
    _,k=both([report('missing-time',at=None),report('known-time')])
    assert k['totals']['caught']==2
    assert not k['totals']['caught_complete']
    assert k['statistics']['coverage']['first_order_unknown_reports']==1


def test_on_time_recommendation_names_recorded_send_denominator():
    from hosting import recommend
    rows=[report('late',sent=[dict(sent=True,at='2026-11-08T07:00:00+00:00')]),report('unsent',job='TEST-2000')]
    o,_=both(rows)
    recs=recommend.overview_rules(o)
    message=next(str(r) for r in recs if 'recorded sends with known timing were on time' in str(r))
    assert '0 of 1 recorded sends with known timing' in message
    assert 'of reports went out' not in message

