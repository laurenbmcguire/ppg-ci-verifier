import io, unittest
from datetime import datetime
from unittest.mock import patch
from openpyxl import load_workbook
from hosting import builder, sharepoint, shifts, public_api

class SafeBuilder(unittest.TestCase):
    def capture(self):
        return ('SL',{'id':'lid','displayName':'Data','_capture':{'site_id':'sid','list_id':'lid','columns':[]}}, {'Shift':'Shift'},[
            {'id':'1','fields':{'Date':'2026-11-07','Date0':'2026-11-07T21:00:00Z','Shift':'1','Identifier':'A','Qty':10,'NOK':None}},
            {'id':'2','fields':{'Date':'2026-11-07','Date0':'2026-11-07T21:00:00Z','Shift':'1','Identifier':'','Qty':None,'NOK':None}}])
    def rows(self,data=None):
        with patch.object(sharepoint,'configured',return_value=True),patch.object(sharepoint,'_list_items',return_value=data or self.capture()),patch.object(builder,'column_roles',return_value={'Identifier':'identifier','Qty':'inspected','NOK':'rejected'}):
            return builder.day_rows('2207','2026-11-07',shifts.DEFAULT,site_id='sid',list_id='lid')
    def test_capture_identity_must_match_selected_source(self):
        captured=self.capture();captured[1]['_capture']['list_id']='other-list'
        with self.assertRaisesRegex(builder.BuildError,'capture identity'):
            self.rows(captured)
    def test_missing_counts_never_invented(self):
        r=builder._row({'VIN':'A','Condition':'NOK'},{'VIN':'identifier','Condition':'condition'})
        self.assertIsNone(r['inspected']);self.assertIsNone(r['rejected']);self.assertIsNone(r['certified'])
    def test_explicit_zero_preserved(self):
        r=builder._row({'Qty':10,'NOK':0},{'Qty':'inspected','NOK':'rejected'})
        self.assertEqual(r['rejected'],0);self.assertIsNone(r['certified']);self.assertEqual(r['condition'],'')
    def test_every_record_and_recorded_shift_preserved(self):
        rows,info=self.rows();self.assertEqual(len(rows),2);self.assertEqual([r['shift'] for r in rows],['1','1'])
        self.assertEqual(rows[1]['source'],{'site_id':'sid','list_id':'lid','item':'2'});self.assertEqual(info['scans'],2)
    def test_missing_shift_holds_instead_of_guessing(self):
        c=self.capture();c[3][0]['fields']['Shift']=None
        with self.assertRaises(builder.BuildError):self.rows(c)
    def test_blank_business_date_is_counted_and_never_dated_from_entry_time(self):
        # SharePoint is shown as it is: a row with a blank Date goes in no day's report, and is counted and named.
        c=self.capture();del c[3][0]['fields']['Date']
        rows,info=self.rows(c)
        self.assertEqual([r['source']['item'] for r in rows],['2']);self.assertEqual(info['undated'],{'count':1,'items':['1']})
        for blank in (None,''):
            c=self.capture();c[3][0]['fields']['Date']=blank
            self.assertEqual(self.rows(c)[1]['undated']['count'],1)
    def test_a_date_that_is_present_but_cannot_be_confirmed_still_holds(self):
        c=self.capture();c[3][0]['fields']['Date']='not a date'
        with self.assertRaises(builder.BuildError):self.rows(c)
    def test_unbound_builder_refused(self):
        with patch.object(sharepoint,'configured',return_value=True):
            with self.assertRaises(builder.BuildError):builder.day_rows('2207','2026-11-07',shifts.DEFAULT)
    def test_inactivity_never_means_complete(self):
        self.assertEqual(builder.readiness('2020-01-01T10:00:00')['state'],'provisional')
        self.assertFalse(builder.readiness('2020-01-01T10:00:00')['shift_end_confirmed'])
    def test_actual_workbook_preserves_blanks_and_omits_empty_shift_tabs(self):
        rows,_=self.rows();raw=builder.build_xlsx('2207','2026-11-07',rows,shifts.DEFAULT)
        w=load_workbook(io.BytesIO(raw));self.assertEqual(w.sheetnames,['Summary','1st Shift','Rejects'])
        # The list has a reject column the crew left empty: nothing was rejected on that row, so it is 0, not unknown.
        self.assertEqual(w['1st Shift']['J16'].value,0);self.assertIsNone(w['1st Shift']['K16'].value)       # certified was not recorded and stays blank
    def test_reject_quantity_from_sharepoint_is_never_unknown_when_the_list_has_the_column(self):
        roles={'Qty':'inspected','NOK':'rejected'}
        for value in (None,''):self.assertEqual(builder._row({'Qty':10,'NOK':value},roles)['rejected'],0)
        self.assertEqual(builder._row({'Qty':10},roles)['rejected'],0)                                       # the field is absent on the row: still the list's own column
        self.assertEqual(builder._row({'Qty':10,'NOK':3},roles)['rejected'],3);self.assertEqual(builder._row({'Qty':10,'NOK':0},roles)['rejected'],0)
        self.assertIsNone(builder._row({'Qty':10,'NOK':'a few'},roles)['rejected'])                          # text is not turned into a number
        self.assertIsNone(builder._row({'Qty':10},{'Qty':'inspected'})['rejected'])                          # a list with no reject column gives no quantity
        self.assertIsNone(builder._row({'NOK':None},roles)['inspected'])                                     # only the reject quantity has this rule
    def test_rate_caches_positive_zero_and_unknown_cases(self):
        for inspected,rejected,certified,want in [(10,2,8,0.2),(10,0,10,0),(0,0,0,None),(10,None,None,None),(None,2,None,None)]:
            with self.subTest(inspected=inspected,rejected=rejected):
                rows,_=self.rows();rows=rows[:1];rows[0].update(inspected=inspected,rejected=rejected,certified=certified)
                raw=builder.build_xlsx('2207','2026-11-07',rows,shifts.DEFAULT)
                w=load_workbook(io.BytesIO(raw),data_only=True)
                for cell in ('M21','M22'):
                    value=w['Summary'][cell].value
                    if want is None:self.assertIn(value,(None,''))
                    else:self.assertAlmostEqual(value,want)
    def test_nexus_missing_quantities_not_zero(self):
        out=builder.nexus_split([dict(time=datetime(2026,11,7,8),inspected=10,rejected=None,certified=None)])
        self.assertIsNone(out['total']['rejected']);self.assertIsNone(out['total']['certified'])
    def test_ambiguous_verified_registry_requires_source_selection(self):
        class Inbox:
            def setting(self,*a):return []
        with patch.object(sharepoint,'assignment_identity',side_effect=[{'site_id':'sl','list_id':'one'},{'site_id':'hmma','list_id':'two'}]):
            with self.assertRaises(public_api.ApiError):public_api._build_binding(Inbox(),'2207')
    def test_verified_registry_source_selection_bound(self):
        class Inbox:
            def setting(self,*a):return []
        with patch.object(sharepoint,'assignment_identity',return_value={'site_id':'sid','list_id':'32a42ee0-4592-4d73-b893-1b98970b3d4b'}) as resolve:
            out=public_api._build_binding(Inbox(),'2207',{'site':'SLAlabama_AlexanderCity','list_id':'32a42ee0-4592-4d73-b893-1b98970b3d4b'})
        self.assertEqual(out['site_id'],'sid');self.assertEqual(resolve.call_args.args[0]['site'],'SLAlabama_AlexanderCity')
    def test_mcp_build_and_nexus_forward_selected_source(self):
        from hosting import mcp_server
        source={'site':'SLAlabama_AlexanderCity','list_id':'32a42ee0-4592-4d73-b893-1b98970b3d4b'}
        for name,operation in [('build_report','op_build'),('nexus_numbers','op_nexus')]:
            with self.subTest(name=name),patch.object(public_api,operation,return_value={'ok':True}) as call:
                result=mcp_server._call(None,None,name,{'job':'2207','day':'2026-11-07','source':source})
                self.assertTrue(result['ok']);self.assertEqual(call.call_args.kwargs['source'],source)
            tool=next(x for x in mcp_server.TOOLS if x['name']==name)
            self.assertIn('source',tool['inputSchema']['properties'])
    def test_build_source_selection_cannot_authorize_unknown_list(self):
        class Inbox:
            def setting(self,*a):return []
        with self.assertRaises(public_api.ApiError):
            public_api._build_binding(Inbox(),'2207',{'site':'SLAlabama_AlexanderCity','list_id':'unknown'})
    def test_conflicting_site_selectors_rejected(self):
        class Inbox:
            def setting(self,*a):return []
        source={'site':'SLAlabama_AlexanderCity','site_id':'wrong','list_id':'32a42ee0-4592-4d73-b893-1b98970b3d4b'}
        with patch.object(sharepoint,'assignment_identity',return_value={'site_id':'correct','list_id':source['list_id']}):
            with self.assertRaises(public_api.ApiError):public_api._build_binding(Inbox(),'2207',source)
    def test_site_id_only_selector_resolves_registry_name(self):
        class Inbox:
            def setting(self,*a):return []
        source={'site_id':'correct','list_id':'32a42ee0-4592-4d73-b893-1b98970b3d4b'}
        with patch.object(sharepoint,'assignment_identity',return_value=source) as resolve:
            self.assertEqual(public_api._build_binding(Inbox(),'2207',source),source)
        self.assertEqual(resolve.call_args.args[0]['site'],'SLAlabama_AlexanderCity')
    def test_consistent_site_and_id_selectors_succeed(self):
        class Inbox:
            def setting(self,*a):return []
        source={'site':'SLAlabama_AlexanderCity','site_id':'correct','list_id':'32a42ee0-4592-4d73-b893-1b98970b3d4b'}
        binding={'site_id':'correct','list_id':source['list_id']}
        with patch.object(sharepoint,'assignment_identity',return_value=binding):
            self.assertEqual(public_api._build_binding(Inbox(),'2207',source),binding)
if __name__=='__main__':unittest.main()
