"""Bounded native DAILY expansion. No capture, auth, storage or release actions."""
from collections import Counter
from copy import deepcopy
import math
import unicodedata
import re
from xml.etree import ElementTree as ET
from hosting import source_first as sf, source_first_template as tx

MAX_TEXT_ROWS = 1200
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_WORKBOOK_BYTES = sf.MAX_RESPONSE_BYTES
MAX_ESTIMATED_PAGES = 300
MAX_CELL_TEXT = 32767
TEXT_CHUNK = 300


def letters(n):
    out=''
    while n:
        n,r=divmod(n-1,26);out=chr(65+r)+out
    return out


def moved(address,at,count):
    return re.sub(r'(\$?[A-Z]{1,3}\$?)([1-9][0-9]*)',lambda m:m[1]+str(int(m[2])+(count if int(m[2])>=at else 0)),address)


def shift_rows(package,roots,name,at,count,donor):
    if not count:return
    root=roots[name];data=root.find('m:sheetData',tx.NS)
    prototype=deepcopy(next(n for n in data if int(n.get('r'))==donor))
    merged=root.find('m:mergeCells',tx.NS)
    copies=[]
    if merged is not None:
        for n in merged:
            refs=n.get('ref').split(':')
            if len(refs)==2 and all(tx.coord(x)[0]==donor for x in refs):copies.append(n.get('ref'))
            n.set('ref',moved(n.get('ref'),at,count))
        for r in range(at,at+count):
            for ref in copies:ET.SubElement(merged,'{'+tx.M+'}mergeCell',{'ref':moved(ref,donor,r-donor)})
        merged.set('count',str(len(merged)))
    for n in data:
        rr=int(n.get('r'))
        if rr>=at:
            n.set('r',str(rr+count))
            for cell in n:cell.set('r',moved(cell.get('r'),at,count))
    for rr in range(at,at+count):
        n=deepcopy(prototype);n.set('r',str(rr))
        for cell in n:
            cell.set('r',moved(cell.get('r'),donor,rr-donor));tx.clear(cell)
        data.append(n)
    data[:]=sorted(data,key=lambda n:int(n.get('r')))
    for n in root.iter():
        if n.tag in ('{'+tx.M+'}mergeCell','{'+tx.M+'}c'):continue
        for key in ('sqref','ref','activeCell','topLeftCell'):
            if n.get(key):
                old=n.get(key);new=moved(old,at,count)
                if key=='sqref':
                    expanded=[]
                    for area in new.split():
                        ends=area.split(':')
                        if len(ends)==2 and tx.coord(ends[-1].replace('$',''))[0]==at-1:
                            area=ends[0]+':'+moved(ends[-1],at-1,count)
                        expanded.append(area)
                    new=' '.join(expanded)
                n.set(key,new)
        if n.tag=='{'+tx.M+'}brk' and int(n.get('id'))>=at:n.set('id',str(int(n.get('id'))+count))
    wb=ET.fromstring(package['xl/workbook.xml'])
    for n in wb.findall('m:definedNames/m:definedName',tx.NS):
        if (n.text or '').startswith("'"+name+"'!"):n.text=moved(n.text,at,count)
    package['xl/workbook.xml']=tx.xml(wb)
    part=tx.sheets(package)[name]
    drawing=root.find('m:drawing',tx.NS)
    if drawing is not None:
        dp=tx.relationships(package,part)[drawing.get('{'+tx.R+'}id')];dr=ET.fromstring(package[dp])
        for n in dr.findall('.//xdr:from/xdr:row',tx.NS)+dr.findall('.//xdr:to/xdr:row',tx.NS):
            if int(n.text)>=at-1:n.text=str(int(n.text)+count)
        package[dp]=tx.xml(dr)
    setup=root.find('m:pageSetup',tx.NS)
    if setup is not None:setup.set('fitToHeight','0')


def add_sheet(package,roots,name,hidden=False):
    wb=ET.fromstring(package['xl/workbook.xml']);sheets=wb.find('m:sheets',tx.NS)
    sid=max(int(n.get('sheetId')) for n in sheets)+1;rid='rIdExpansion'+str(sid)
    part=f'xl/worksheets/expansion{sid}.xml'
    attrs={'name':name,'sheetId':str(sid),'{'+tx.R+'}id':rid}
    if hidden:attrs['state']='hidden'
    ET.SubElement(sheets,'{'+tx.M+'}sheet',attrs);package['xl/workbook.xml']=tx.xml(wb)
    rels=ET.fromstring(package['xl/_rels/workbook.xml.rels'])
    ET.SubElement(rels,'{'+tx.REL+'}Relationship',{'Id':rid,'Type':tx.R+'/worksheet','Target':part[3:]})
    package['xl/_rels/workbook.xml.rels']=tx.xml(rels)
    ct=ET.fromstring(package['[Content_Types].xml'])
    ET.SubElement(ct,'{http://schemas.openxmlformats.org/package/2006/content-types}Override',{'PartName':'/'+part,'ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
    package['[Content_Types].xml']=tx.xml(ct)
    root=ET.Element('{'+tx.M+'}worksheet');ET.SubElement(root,'{'+tx.M+'}sheetData');roots[name]=root;package[part]=tx.xml(root)
    return root


def set_print(package,name,area,titles=None):
    wb=ET.fromstring(package['xl/workbook.xml']);names=wb.find('m:definedNames',tx.NS)
    if names is None:names=ET.SubElement(wb,'{'+tx.M+'}definedNames')
    sid=list(tx.sheets(package)).index(name)
    for key,text in [('_xlnm.Print_Area',area),('_xlnm.Print_Titles',titles)]:
        if text is None:continue
        n=next((n for n in names if n.get('name')==key and n.get('localSheetId')==str(sid)),None)
        if n is None:n=ET.SubElement(names,'{'+tx.M+'}definedName',{'name':key,'localSheetId':str(sid)})
        n.text="'"+name+"'!"+text
    package['xl/workbook.xml']=tx.xml(wb)


def breaks(root,rows):
    old=root.find('m:rowBreaks',tx.NS)
    if old is not None:root.remove(old)
    n=ET.Element('{'+tx.M+'}rowBreaks',{'count':str(len(rows)),'manualBreakCount':str(len(rows))})
    for row in rows:ET.SubElement(n,'{'+tx.M+'}brk',{'id':str(row-1),'min':'0','max':'16383','man':'1'})
    # rowBreaks precede drawings in the native worksheet schema.
    i=next((i for i,x in enumerate(root) if x.tag in ('{'+tx.M+'}drawing','{'+tx.M+'}legacyDrawing')),len(root))
    root.insert(i,n)


def plan(rows,contract):
    counts=Counter(r['resolved']['shift'] for r in rows)
    return dict(shifts={s['sheet']:max(0,counts[s['sheet']]-s['capacity']) for s in contract['shift_details'].values()},
                parts=sorted({r['resolved']['part'] for r in rows}),reject_extra=max(0,sum(bool(r['resolved']['rejected']) for r in rows)-13),
                rank_start=max(30,len(rows)+4))


def expand(package,roots,contract,rows):
    c=deepcopy(contract);p=plan(rows,c);p['part_extra']=max(0,len(p['parts'])-1)
    for spec in c['shift_details'].values():
        n=p['shifts'][spec['sheet']]
        shift_rows(package,roots,spec['sheet'],spec['last_row']+1,n,spec['last_row'])
        spec['last_row']+=n;spec['total_row']+=n;spec['capacity']+=n
        for key in ('billing_name','billing_hours','billing_total'):spec[key]=moved(spec[key],16,n)
    shift_rows(package,roots,'Summary',30,p['part_extra'],29)
    merges=roots['Summary'].find('m:mergeCells',tx.NS)
    for merge in merges:
        if merge.get('ref')==f"C{62+p['part_extra']}:D{62+p['part_extra']}":merge.set('ref',f"B{62+p['part_extra']}:D{62+p['part_extra']}")
    shift_rows(package,roots,'Rejects & Photos',21,p['reject_extra'],20)
    if p['reject_extra']:
        set_print(package,'Rejects & Photos',f'$A$1:$I${90+p["reject_extra"]}','$6:$7')
        breaks(roots['Rejects & Photos'],[24+p['reject_extra'],41+p['reject_extra'],58+p['reject_extra'],75+p['reject_extra']])
    add_sheet(package,roots,'_Source',hidden=True)
    return c,p


def text_segments(value):
    # Bound characters, wide glyphs, tabs and explicit line breaks separately.
    # Concatenation is exactly equal to the source, including all whitespace.
    result=[];start=0;weight=0;lines=0
    for i,ch in enumerate(value):
        unit=4 if ch=='\t' else 2 if unicodedata.east_asian_width(ch) in ('W','F') else 1
        if weight+unit>TEXT_CHUNK or (ch=='\n' and lines>=4):
            result.append(value[start:i]);start=i;weight=0;lines=0
        weight+=unit;lines+=ch=='\n'
    result.append(value[start:])
    return result


def printed_lines(value,width=12):
    return sum(max(1,math.ceil(sum(4 if c=='\t' else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in line)/width)) for line in str(value).split('\n'))


def needs_continuation(field,value):
    return isinstance(value,str) and printed_lines(value)>8


def findings(rows):
    out=[];n=0
    for row in rows:
        for identity in (row['id'],row.get('source_version','')):
            if len(identity)>(40 if identity==row['id'] else 300) or any(ord(c)<32 for c in identity):out.append(dict(code='identity_capacity',text='Source ID or version exceeds the verified printable identity limits. Nothing was truncated.',blocking=True))
        for values in (row['values'],row.get('original_values',{})):
            for v in values.values():
                x=v.get('value')
                if isinstance(x,str) and (len(x)>MAX_CELL_TEXT or any(ord(c)<32 and c not in '\n\r\t' for c in x)):
                    out.append(dict(code='text_capacity',text='A source field exceeds the verified spreadsheet text limits. Nothing will be truncated.',blocking=True))
        for field,value in row['resolved'].items():
            if needs_continuation(field,value):n+=len(text_segments(value))
    if n>MAX_TEXT_ROWS:out.append(dict(code='text_capacity',text='Complete printable source text exceeds the verified continuation-row limit. Nothing will be truncated.',blocking=True))
    return out


def make_text_sheet(package,roots,entries,put):
    if not entries:return
    root=add_sheet(package,roots,'Source Text')
    data=root.find('m:sheetData',tx.NS)
    pr=ET.Element('{'+tx.M+'}sheetPr');ET.SubElement(pr,'{'+tx.M+'}pageSetUpPr',{'fitToPage':'1'});root.insert(0,pr)
    cols=ET.Element('{'+tx.M+'}cols')
    for i,width in enumerate((22,20,12,105),1):ET.SubElement(cols,'{'+tx.M+'}col',{'min':str(i),'max':str(i),'width':str(width),'customWidth':'1'})
    root.insert(1,cols)
    merges=ET.SubElement(root,'{'+tx.M+'}mergeCells',{'count':'2'})
    for ref in ('A1:D1','A2:D2'):ET.SubElement(merges,'{'+tx.M+'}mergeCell',{'ref':ref})
    base=roots['1st Shift']
    for tag in ('printOptions','pageMargins','pageSetup','headerFooter'):
        n=base.find('m:'+tag,tx.NS)
        if n is not None:root.append(deepcopy(n))
    root.find('m:pageSetup',tx.NS).set('fitToHeight','0')
    put('Source Text','A1','PPG Quality · Complete source text · HELD')
    put('Source Text','A2','Segments preserve exact characters in order. Record ID and field identify the approved source value.')
    for col,value in zip('ABCD',('Source record ID','Field','Segment','Complete text')):put('Source Text',col+'4',value)
    for rr,entry in enumerate(entries,5):
        for col,value in zip('ABCD',(entry['record_id'],entry['field'],entry['segment'],entry['text'])):put('Source Text',col+str(rr),value)
    styles=ET.fromstring(package['xl/styles.xml']);xfs=styles.find('m:cellXfs',tx.NS)
    basecell=tx.cell(roots['1st Shift'],'L16');style=deepcopy(xfs[int(basecell.get('s','0'))]);style.set('numFmtId','49');style.set('applyNumberFormat','1')
    alignment=style.find('m:alignment',tx.NS)
    if alignment is None:alignment=ET.SubElement(style,'{'+tx.M+'}alignment')
    alignment.set('wrapText','1');alignment.set('vertical','top');alignment.attrib.pop('shrinkToFit',None)
    idx=len(xfs);xfs.append(style);xfs.set('count',str(len(xfs)));package['xl/styles.xml']=tx.xml(styles)
    for row in data:
        row.set('ht','96' if int(row.get('r'))>=5 else '32');row.set('customHeight','1')
        for cell in row:cell.set('s',str(idx))
    set_print(package,'Source Text',f'$A$1:$D${4+len(entries)}','$4:$4')


def write_ledger(rows,cap,put):
    metadata=('source_record_id','source_version','source_record_hash','snapshot_hash')
    for i,name in enumerate(metadata,1):put('_Source',letters(i)+'1',name)
    for fi,field in enumerate(sf.FIELDS):
        for i,suffix in enumerate(('original_type','original_value','approved_type','approved_value')):put('_Source',letters(5+fi*4+i)+'1',field+'_'+suffix)
    put('_Source',letters(5+len(sf.FIELDS)*4)+'1','mapped_source_columns')
    byid={r['id']:r for r in cap['records']}
    for rn,row in enumerate(rows,2):
        source=byid[row['id']]
        put('_Source',letters(5+len(sf.FIELDS)*4)+str(rn),sf.canonical(row['mapped_fields']).decode())
        for i,value in enumerate((row['id'],source['version'],row['record_hash'],cap['snapshot_hash']),1):put('_Source',letters(i)+str(rn),value)
        if 'original_values' not in row:raise sf.ReviewError('Original mapped typed provenance is required for expanded generation.')
        for fi,field in enumerate(sf.FIELDS):
            original=row['original_values'][field];approved=row['values'][field]
            for i,value in enumerate((original['type'],original.get('value'),approved['type'],approved.get('value'))):put('_Source',letters(5+fi*4+i)+str(rn),value)


def verify_ledger(values,rows,cap):
    byid={r['id']:r for r in cap['records']};sheet=values['_Source']
    for rn,row in enumerate(rows,2):
        if row['record_hash']!=sf.digest(byid[row['id']]):raise sf.ReviewError('Source row hash no longer matches captured provenance.')
        if sheet.cell(rn,5+len(sf.FIELDS)*4).value!=sf.canonical(row['mapped_fields']).decode():raise sf.ReviewError('Mapped field identity differs from source provenance.')
        for field in sf.FIELDS:
            if row['original_values'][field]!=sf.typed(byid[row['id']]['raw'],row['mapped_fields'][field]):raise sf.ReviewError('Original mapped source value changed.')
        for i,want in enumerate((row['id'],byid[row['id']]['version'],row['record_hash'],cap['snapshot_hash']),1):
            if sheet.cell(rn,i).value!=want or sheet.cell(rn,i).data_type!='s':raise sf.ReviewError('Saved source identity provenance failed.')
        for fi,field in enumerate(sf.FIELDS):
            for start,source in ((5,row['original_values'][field]),(7,row['values'][field])):
                typ=sheet.cell(rn,start+fi*4).value;actual=sheet.cell(rn,start+fi*4+1).value;want=source.get('value')
                if typ!=source['type'] or not (actual==want or actual is None and want==''):raise sf.ReviewError('Saved typed source provenance failed.')
                if want not in (None,'') and type(want) is not type(actual):raise sf.ReviewError('Saved source value type changed.')


def package_limits(package,roots,raw):
    xml_bytes=sum(len(v) for k,v in package.items() if k.endswith(('.xml','.rels')))
    estimated_pages=0
    for name,root in roots.items():
        if name.startswith('_'):continue
        height=sum(float(r.get('ht','15')) for r in root.findall('m:sheetData/m:row',tx.NS))
        estimated_pages+=math.ceil(height/600)+len(root.findall('m:rowBreaks/m:brk',tx.NS))
    if xml_bytes>MAX_XML_BYTES or len(raw)>MAX_WORKBOOK_BYTES or estimated_pages>MAX_ESTIMATED_PAGES:
        raise sf.ReviewError('Expanded report exceeds the verified output or printable-page budget. Nothing was truncated.')
    return dict(xml_bytes=xml_bytes,workbook_bytes=len(raw),conservative_page_budget=estimated_pages)
