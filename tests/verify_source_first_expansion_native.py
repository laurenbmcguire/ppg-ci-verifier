"""Prepare/check native evidence in separate processes to bound peak memory.

python tests/verify_source_first_expansion_native.py prepare <output>
soffice --headless --convert-to xlsx --outdir <output>/native <output>/native-input/*.xlsx
soffice --headless --convert-to pdf --outdir <output>/pdf <output>/large.xlsx <output>/small.xlsx <output>/long_identifiers.xlsx
python tests/verify_source_first_expansion_native.py check <output>
"""
from pathlib import Path
import json
import re
import sys
from xml.etree import ElementTree as ET
from openpyxl import load_workbook
import fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_source_first_expansion import build,fixture
from hosting import source_first as sf,source_first_template as tx

mode=sys.argv[1];out=Path(sys.argv[2] if len(sys.argv)>2 else 'verification/expansion')
out.mkdir(parents=True,exist_ok=True)
for sub in ('native','native-input','pdf'):(out/sub).mkdir(exist_ok=True)
cases=[('large',621,True,12,False),('small',3,False,0,False),('long_identifiers',3,False,0,True)]
if mode=='prepare':
    for name,n,long,photos,long_ids in cases:
        raw,v,rows,cap=build(n,long,photos,long_ids);(out/(name+'.xlsx')).write_bytes(raw)
        (out/(name+'-verification.json')).write_text(json.dumps(v,indent=2))
    # Only a disposable quantity-change test copy is edited, never the artifact.
    p=tx.read_package((out/'large.xlsx').read_bytes());part=tx.sheets(p)['1st Shift'];root=ET.fromstring(p[part])
    for address,value in [('J16',9),('K16',1)]:tx.write(root,address,value)
    p[part]=tx.xml(root);(out/'changed.xlsx').write_bytes(tx.packed(p))
    # Missing caches force native evaluation; a matching saved cache alone is
    # not evidence that LibreOffice actually recalculated the formula.
    for name in ('large','small','long_identifiers','changed'):
        package=tx.read_package((out/(name+'.xlsx')).read_bytes())
        for sheet_part in tx.sheets(package).values():
            sheet=ET.fromstring(package[sheet_part])
            for cell in sheet.findall('.//m:sheetData/m:row/m:c',tx.NS):
                if cell.find('m:f',tx.NS) is not None:
                    for cache in cell.findall('m:v',tx.NS):cell.remove(cache)
            package[sheet_part]=tx.xml(sheet)
        (out/'native-input'/(name+'.xlsx')).write_bytes(tx.packed(package))
    print('Prepared synthetic artifacts and uncached native-evaluation copies.');sys.exit(0)
if mode!='check':raise SystemExit('Use prepare or check.')
result={}
for name,n,long,photos,long_ids in cases:
    v=json.loads((out/(name+'-verification.json')).read_text());cap,_,_=fixture(n,long,photos,long_ids)
    original=load_workbook(out/(name+'.xlsx'),data_only=True);formula=load_workbook(out/(name+'.xlsx'),data_only=False);native=load_workbook(out/'native'/(name+'.xlsx'),data_only=True)
    count=0;diffs=[];errors=[]
    for sheet in formula:
        for row in sheet:
            for cell in row:
                actual=native[sheet.title][cell.coordinate]
                if actual.data_type=='e':errors.append([sheet.title,cell.coordinate,actual.value])
                if cell.data_type!='f':continue
                count+=1;a=original[sheet.title][cell.coordinate].value;b=actual.value
                if a!=b and not(a in (None,'') and b in (None,'')) and not(isinstance(a,(int,float))and isinstance(b,(int,float))and abs(a-b)<1e-9):diffs.append([sheet.title,cell.coordinate,a,b])
    ledger_differences=[];empty_text_cells=0
    for row in original['_Source']:
        for cell in row:
            actual=native['_Source'][cell.coordinate]
            # openpyxl reads an empty inline string as None; LibreOffice may
            # save it as an explicit empty string. The adjacent exact type cell
            # distinguishes this from null/absent and must remain unchanged.
            empty_text=cell.column>=6 and (cell.column-6)%4 in (0,2) and original['_Source'].cell(cell.row,cell.column-1).value=='text' and cell.value in (None,'') and actual.value in (None,'')
            if empty_text:empty_text_cells+=1
            elif actual.value!=cell.value or isinstance(cell.value,str) and actual.data_type!='s':ledger_differences.append(cell.coordinate)
    pdf=fitz.open(out/'pdf'/(name+'.pdf'));pdftext='\n'.join(page.get_text()for page in pdf);(out/(name+'-pdf-text.txt')).write_text(pdftext)
    rejected={r['id']for r in cap['records'] if sf.quantity(sf.typed(r['raw'],'Rejected'))};ids=set(re.findall(r'\b\d{5}\b',pdftext));missing=sorted(rejected-ids)
    pages=[]
    for i,page in enumerate(pdf):
        text=page.get_text()
        kind='source_text' if 'Complete text' in text else 'shift' if 'INSPECTION RECORDS' in text else 'photo' if 'Original source photo' in text else 'traceability' if 'ALL REJECTED RECORDS' in text else 'summary'
        pages.append({'page':i+1,'section':kind})
    render_pages=sorted(set([0,1,2,len(pdf)//2,len(pdf)-6,len(pdf)-2,len(pdf)-1]))
    for i in render_pages:
        if 0<=i<len(pdf):pdf[i].get_pixmap(matrix=fitz.Matrix(1,1)).save(str(out/(name+f'-final-page-{i+1}.png')))
    result[name]=dict(workbook_sha256=sf.digest((out/(name+'.xlsx')).read_bytes()),uncached_native_input_sha256=sf.digest((out/'native-input'/(name+'.xlsx')).read_bytes()),native_recalculated_sha256=sf.digest((out/'native'/(name+'.xlsx')).read_bytes()),primary_source_ids_exact_once=len({p['record_id'] for p in v['row_provenance']})==len(cap['records']) and len(v['row_provenance'])==len(cap['records']),ledger_record_count=original['_Source'].max_row-1,full_rejected_record_count=len(rejected),shown_top13_rejected_subtotal=original['Summary']['F'+str(62+v['layout_expansion']['part_extra'])].value,full_rejected_quantity=original['Summary']['K24'].value,formula_cells=count,differences=diffs,formula_errors=errors,typed_ledger_differences=ledger_differences,empty_text_cells_compared_with_explicit_type=empty_text_cells,pdf_pages=len(pdf),page_sections=pages,rejected_record_ids_missing_in_pdf=missing,output_budgets=v['output_budgets'])
    if long_ids:
        observed={label:{'before_native':original[sheet][cell].value,'after_native':native[sheet][cell].value} for label,sheet,cell in [('calc_identifier','_Calc','B3'),('calc_part','_Calc','G3'),('display_identifier','2nd Shift','E16'),('display_part','2nd Shift','D16'),('ranked_identifier','Summary','C51'),('part_inspected_total','Summary','J31')]}
        result[name]['continuation_reference_values']=observed
        assert observed['calc_identifier']==observed['display_identifier']==observed['ranked_identifier']
        assert observed['calc_part']==observed['display_part']
        assert observed['part_inspected_total']=={'before_native':10,'after_native':10}
        for field,value in [('identifier','ID'+'x'*100),('part','PART'+'y'*100)]:
            col=5+sf.FIELDS.index(field)*4
            assert native['_Source'].cell(3,col+1).value==native['_Source'].cell(3,col+3).value==value
changed=load_workbook(out/'native'/'changed.xlsx',data_only=True)
result['quantity_change']={'top_identifier':changed['Summary']['C51'].value,'rejected_total':changed['Summary']['K24'].value,'inspected_total':changed['Summary']['G24'].value,'part_total':changed['Summary']['N29'].value}
(out/'native-final-verification.json').write_text(json.dumps(result,indent=2))
assert result['quantity_change']==dict(top_identifier='000000100000',rejected_total=1001,inspected_total=6210,part_total=339),result['quantity_change']
assert all(not r['differences'] and not r['formula_errors'] and not r['typed_ledger_differences'] and not r['rejected_record_ids_missing_in_pdf'] for k,r in result.items() if k!='quantity_change'),result
print(json.dumps({k:{a:b for a,b in v.items()if a!='page_sections'}for k,v in result.items()},indent=2))
