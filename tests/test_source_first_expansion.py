"""Synthetic bounded native expansion. No external or live-source reads."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from openpyxl import load_workbook
sys.path.insert(0,str(Path(__file__).parent))
from source_first_fixture import capture,MAP,USER
from hosting import source_first as sf,source_first_workbook as wb,source_first_layout as lx,source_first_template as tx


def fixture(count=621,long=False,photos=0,long_identifiers=False):
    base=capture()['records'][0];records=[]
    for i in range(count):
        r=deepcopy(base);r['id']=str(i+1).zfill(5);r['version']='v'+str(i+1)
        r['raw'].update(Shift=('1','2nd',3)[i%3],Part=('0001','1','0002')[i%3],Identifier=str(100000+i).zfill(12),Inspected=10,Rejected=2 if i%5 else 0,Certified=8 if i%5 else 10,Comments=None if i%7==0 else '' if i%7==1 else 'Recorded comment')
        if long and i==5:r['raw']['Comments']='Preserve spaces  and newline\n'+('A very long exact comment. '*90)+'\nEND'
        if i==6:r['raw']['Inspected']='10';r['raw']['Rejected']='2';r['raw']['Certified']='8'
        records.append(r)
    if long_identifiers:records[1]['raw'].update(Identifier='ID'+'x'*100,Part='PART'+'y'*100)
    cap=capture(records);blobs={};choices={}
    if photos:
        from PIL import Image
        out=BytesIO();Image.new('RGB',(50,30),(20,40,180)).save(out,format='PNG');blob=out.getvalue();sha=sf.digest(blob);blobs[sha]=blob
        selected=[r for r in records if r['raw']['Rejected'] in (2,'2')][:photos]
        cap['photos']=[dict(record_id=r['id'],record_hash=sf.digest(r),field='Photo',status='supported',sha256=sha)for r in selected]
        choices={r['id']:sha for r in selected[:11]}
        cap=sf.seal({k:v for k,v in cap.items()if k not in ('snapshot_hash','envelope_hash')})
    return cap,blobs,choices


def build(count=621,long=False,photos=0,long_identifiers=False):
    cap,blobs,choices=fixture(count,long,photos,long_identifiers);rows,issues=sf.reviewed_rows(cap,MAP,[])
    assert not any(i['blocking'] for i in issues),issues
    head=wb.header({'photo_choices':choices},cap);c=wb.contract();binding=sf.binding(cap,MAP,[],head,c['clean_sha256'],sf.digest(c),wb.generator_hash())
    raw,verification=wb.build(rows,head,cap,binding,blobs)
    return raw,verification,rows,cap


class Expansion(unittest.TestCase):
    def test_621_rows_mixed_parts_all_shifts_ties_and_full_reject_trace(self):
        raw,v,rows,cap=build();book=load_workbook(BytesIO(raw),data_only=True)
        self.assertEqual(v['record_count'],621);self.assertTrue(v['typed_source_ledger_verified'])
        self.assertEqual(book['Summary']['G24'].value,6210)
        self.assertEqual([book['Summary'][f'B{i}'].value for i in range(29,32)],['0001','0002','1'])
        self.assertEqual([book['Summary'][f'J{i}'].value for i in range(29,32)],[2070]*3)
        rejected=sorted((r for r in rows if r['resolved']['rejected']),key=lambda r:(-r['resolved']['rejected'],r['id']))
        self.assertEqual(v['complete_rejected_record_count'],len(rejected))
        self.assertEqual([book['Rejects & Photos'][f'I{i}'].value for i in range(8,8+len(rejected))],[r['id']for r in rejected])
        self.assertEqual(book['Summary']['F64'].value,26)
        self.assertEqual(book['Rejects & Photos'][f'F{8+len(rejected)}'].value,len(rejected)*2)
        self.assertEqual(len(set((p['sheet'],p['row']) for p in v['row_provenance'])),621)
        self.assertGreater(v['layout_expansion']['rank_start'],len(rows)+1)
        for name in sf.SHIFT_NAMES:self.assertEqual(book[name].page_setup.fitToHeight,0)
        self.assertEqual(book['_Source'].sheet_state,'hidden')
    def test_long_comments_complete_in_printable_continuation(self):
        raw,v,rows,cap=build(30,long=True);book=load_workbook(BytesIO(raw),data_only=True)
        source=next(r for r in rows if r['id']=='00006')['resolved']['comments']
        text=''.join(book['Source Text'][f'D{i}'].value or '' for i in range(5,book['Source Text'].max_row+1))
        self.assertEqual(text,source);self.assertIn('Source Text',book.sheetnames)
        self.assertGreater(v['printable_text_segments'],1)
        where=next(p for p in v['row_provenance'] if p['record_id']=='00006')
        self.assertTrue(book[where['sheet']]['L'+str(where['row'])].alignment.wrap_text)
    def test_long_identifier_and_part_reference_caches_match_display(self):
        raw,v,rows,cap=build(3,long_identifiers=True);book=load_workbook(BytesIO(raw),data_only=True)
        self.assertTrue(v['source_reference_cache_values_checked'])
        self.assertEqual(book['_Calc']['B3'].value,book['2nd Shift']['E16'].value)
        self.assertEqual(book['_Calc']['G3'].value,book['2nd Shift']['D16'].value)
        self.assertEqual(book['Summary']['C51'].value,book['_Calc']['B3'].value)
        for field,value in [('identifier','ID'+'x'*100),('part','PART'+'y'*100)]:
            col=5+sf.FIELDS.index(field)*4
            self.assertEqual(book['_Source'].cell(3,col+1).value,value)
            self.assertEqual(book['_Source'].cell(3,col+3).value,value)
        self.assertEqual(book['_Calc']['J3'].value,3)
        self.assertEqual(book['Summary']['J31'].value,10)
        self.assertEqual(v['part_totals']['PART'+'y'*100],{'inspected':10,'rejected':2,'certified':8})
    def test_verifier_rejects_inconsistent_direct_link_cache(self):
        original_verify=wb.verify
        def corrupt(raw,*args):
            args=list(args);expected=deepcopy(args[4]);expected[('_Calc','B3')]['value']='inconsistent cache';args[4]=expected
            package=tx.read_package(raw);part=tx.sheets(package)['_Calc'];root=ET.fromstring(package[part])
            tx.write(root,'B3','inconsistent cache',expected[('_Calc','B3')]['formula']);package[part]=tx.xml(root)
            return original_verify(tx.packed(package),*args)
        with patch.object(wb,'verify',side_effect=corrupt):
            with self.assertRaisesRegex(sf.ReviewError,'source-reference cache'):
                build(3,long_identifiers=True)
    def test_original_and_overlay_typed_fields_are_separate(self):
        cap,_,_=fixture(3);o=sf.proposal(cap,'00001','comments',{'type':'text','value':'=HYPERLINK("https://invalid.test","inert")'},'Reviewed typed overlay evidence',USER);o['original']=sf.typed(cap['records'][0]['raw'],'Comments')
        rows,issues=sf.reviewed_rows(cap,MAP,[o]);head=wb.header({},cap);c=wb.contract();binding=sf.binding(cap,MAP,[o],head,c['clean_sha256'],sf.digest(c),wb.generator_hash())
        raw,v=wb.build(rows,head,cap,binding,{});book=load_workbook(BytesIO(raw),data_only=False)
        fi=sf.FIELDS.index('comments');self.assertEqual(book['_Source'].cell(2,5+fi*4).value,'null');self.assertEqual(book['_Source'].cell(2,7+fi*4).value,'text')
        self.assertEqual(book['_Source'].cell(2,8+fi*4).data_type,'s')
    def test_more_than_eleven_photos_are_explicit_representatives(self):
        raw,v,rows,cap=build(30,photos=12);book=load_workbook(BytesIO(raw),data_only=True)
        self.assertEqual(len(v['photo_provenance']),11)
        self.assertIn('12 supported captured',book['Summary']['B44'].value)
        self.assertIn('1 supported not selected',book['Summary']['B44'].value)
        self.assertEqual({x['record_id'] for x in v['photo_provenance']},set(p['record_id']for p in cap['photos'][:11]))
        with self.assertRaises(sf.ReviewError):wb.header({'photo_choices':{p['record_id']:p['sha256']for p in cap['photos']}},cap)
    def test_valid_outlier_part_is_not_normalized_by_cohort(self):
        cap,_,_=fixture(27)
        for i,r in enumerate(cap['records']):r['raw']['Part']='00356870-V-LH' if i==14 else '00000001 AA'
        cap=capture(cap['records']);rows,issues=sf.reviewed_rows(cap,MAP,[]);self.assertFalse(any(x['blocking'] for x in issues))
        head=wb.header({},cap);c=wb.contract();bind=sf.binding(cap,MAP,[],head,c['clean_sha256'],sf.digest(c),wb.generator_hash());raw,v=wb.build(rows,head,cap,bind,{})
        book=load_workbook(BytesIO(raw),data_only=True);where=next(p for p in v['row_provenance']if p['record_id']=='00015')
        self.assertEqual(book[where['sheet']]['D'+str(where['row'])].value,'00356870-V-LH')
        self.assertEqual(book[where['sheet']]['D'+str(where['row'])].number_format,'@')
        self.assertIn('00356870-V-LH',[book['Summary'][f'B{i}'].value for i in (29,30)])
        self.assertEqual(rows[14]['original_values']['part'],rows[14]['values']['part'])
    def test_stale_photo_source_hash_is_held(self):
        cap,blobs,choices=fixture(30,photos=12);cap['photos'][0]['record_hash']='wrong'
        cap=sf.seal({k:v for k,v in cap.items()if k not in ('snapshot_hash','envelope_hash')});rows,_=sf.reviewed_rows(cap,MAP,[])
        findings=wb.scope_findings(rows,wb.header({'photo_choices':choices},cap),cap,wb.contract())
        self.assertTrue(any(x['code']=='photo_changed' and x['blocking'] for x in findings))
    def test_layout_boundaries_all_three_shifts(self):
        for count in (1,5,13,14,100,500,2000):
            cap,_,_=fixture(count);rows,_=sf.reviewed_rows(cap,MAP,[]);plan=lx.plan(rows,wb.contract())
            self.assertGreater(plan['rank_start'],len(rows)+1)
            self.assertEqual(sum(max(wb.contract()['shift_details'][str(i+1)]['capacity'],sum(r['resolved']['shift']==name for r in rows)) for i,name in enumerate(sf.SHIFT_NAMES)),sum(wb.contract()['shift_details'][str(i+1)]['capacity']+plan['shifts'][name] for i,name in enumerate(sf.SHIFT_NAMES)))
    def test_excessive_text_is_held_not_truncated(self):
        cap,_,_=fixture(3);cap['records'][0]['raw']['Comments']='x'*32768;cap=capture(cap['records']);rows,_=sf.reviewed_rows(cap,MAP,[])
        self.assertTrue(any(x['blocking']for x in wb.scope_findings(rows,wb.header({},cap),cap,wb.contract())))
    def test_pathological_but_cell_supported_text_is_held(self):
        cap,_,_=fixture(80)
        for r in cap['records']:r['raw']['Comments']='x'*7900
        cap=capture(cap['records']);rows,_=sf.reviewed_rows(cap,MAP,[])
        self.assertTrue(any(x['code']=='text_capacity' and x['blocking'] for x in wb.scope_findings(rows,wb.header({},cap),cap,wb.contract())))
    def test_null_absent_empty_and_zero_remain_distinct_after_expansion(self):
        cap,_,_=fixture(30)
        cap['records'][0]['raw'].pop('Comments',None);cap['records'][1]['raw']['Comments']=None;cap['records'][2]['raw']['Comments']='';cap['records'][3]['raw']['Comments']=0
        cap=capture(cap['records']);rows,_=sf.reviewed_rows(cap,MAP,[]);head=wb.header({},cap);c=wb.contract();bind=sf.binding(cap,MAP,[],head,c['clean_sha256'],sf.digest(c),wb.generator_hash());raw,v=wb.build(rows,head,cap,bind,{})
        b=load_workbook(BytesIO(raw),data_only=True);col=5+sf.FIELDS.index('comments')*4
        self.assertEqual([b['_Source'].cell(i,col).value for i in range(2,6)],['absent','null','text','number'])
        self.assertEqual(b['_Source'].cell(5,col+1).value,0)
    def test_text_segments_preserve_wide_glyphs_and_many_newlines(self):
        value=('中\t\n'*700)+'END';chunks=lx.text_segments(value)
        self.assertEqual(''.join(chunks),value);self.assertTrue(all(x.count('\n')<=5 for x in chunks))
    def test_source_ledger_has_no_other_day_or_unmapped_fields(self):
        cap,_,_=fixture(3);cap['records'][0]['raw']['UnrelatedSecret']='MUST_NOT_APPEAR';cap=capture(cap['records']);rows,_=sf.reviewed_rows(cap,MAP,[]);head=wb.header({},cap);c=wb.contract();bind=sf.binding(cap,MAP,[],head,c['clean_sha256'],sf.digest(c),wb.generator_hash());raw,v=wb.build(rows,head,cap,bind,{})
        self.assertNotIn(b'MUST_NOT_APPEAR',b''.join(tx.read_package(raw).values()))

if __name__=='__main__':unittest.main()
