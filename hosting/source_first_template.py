"""Native OOXML cleaner for the exact selected DAILY template.

No round-trip authoring conversion: untouched package parts, styles, print setup,
merges and validation remain native. Formula caches and sample media are removed.
"""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import posixpath
import re
from xml.etree import ElementTree as ET
import zipfile
import threading
from hosting import source_first as sf

M='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL='http://schemas.openxmlformats.org/package/2006/relationships'
C='http://schemas.openxmlformats.org/drawingml/2006/chart'
D='http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
A='http://schemas.openxmlformats.org/drawingml/2006/main'
NS={'m':M,'r':R,'rel':REL,'c':C,'xdr':D,'a':A}
for prefix,url in [('','http://schemas.openxmlformats.org/spreadsheetml/2006/main'),('r',R),('c',C),('xdr',D),('a',A)]:
    ET.register_namespace(prefix,url)


def read_package(raw):
    with zipfile.ZipFile(BytesIO(raw)) as z:
        return {i.filename:z.read(i.filename) for i in z.infolist()}


def packed(parts):
    out=BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,raw in sorted(parts.items()):
            info=zipfile.ZipInfo(name,(1980,1,1,0,0,0)); info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,raw)
    return out.getvalue()


_XML_LOCK=threading.RLock()

def xml(root):
    # OPC consumers require the package Types/Relationships root in its default
    # namespace. Registration is global in ElementTree, so serialize under a lock.
    namespace=root.tag[1:].split('}')[0] if root.tag.startswith('{') else M
    with _XML_LOCK:
        ET.register_namespace('',namespace if namespace in (REL,'http://schemas.openxmlformats.org/package/2006/content-types') else M)
        try:
            return ET.tostring(root,encoding='utf-8',xml_declaration=True)
        finally:
            ET.register_namespace('',M)


def relpath(part):
    folder,name=posixpath.split(part)
    return posixpath.join(folder,'_rels',name+'.rels')


def relationships(parts,part):
    key=relpath(part)
    if key not in parts:return {}
    return {n.get('Id'):posixpath.normpath(posixpath.join(posixpath.dirname(part),n.get('Target'))).lstrip('/')
            if not n.get('Target','').startswith('/') else n.get('Target').lstrip('/')
            for n in ET.fromstring(parts[key]) if n.get('TargetMode')!='External'}


def sheets(parts):
    refs=relationships(parts,'xl/workbook.xml')
    return {n.get('name'):refs[n.get('{'+R+'}id')] for n in ET.fromstring(parts['xl/workbook.xml']).findall('m:sheets/m:sheet',NS)}


def number_col(label):
    n=0
    for ch in label:n=n*26+ord(ch)-64
    return n


def coord(address):
    m=re.fullmatch(r'([A-Z]+)([1-9][0-9]*)',address)
    return int(m[2]),number_col(m[1])


def clear(cell):
    for child in list(cell):cell.remove(child)
    cell.attrib.pop('t',None)


def cell(root,address):
    data=root.find('m:sheetData',NS); rr,cc=coord(address)
    row=next((r for r in data if int(r.get('r'))==rr),None)
    if row is None:
        row=ET.Element('{'+M+'}row',{'r':str(rr)});data.append(row);data[:]=sorted(data,key=lambda r:int(r.get('r')))
    found=next((c for c in row if c.get('r')==address),None)
    if found is None:
        found=ET.Element('{'+M+'}c',{'r':address});row.append(found);row[:]=sorted(row,key=lambda c:coord(c.get('r'))[1])
    return found


def write(root,address,value=None,formula=None):
    n=cell(root,address);clear(n)
    if formula is not None:ET.SubElement(n,'{'+M+'}f').text=formula.lstrip('=')
    if value is None:return
    if isinstance(value,str):
        n.set('t','str' if formula is not None else 'inlineStr')
        if formula is None:
            t=ET.SubElement(ET.SubElement(n,'{'+M+'}is'),'{'+M+'}t');t.text=value
            if value!=value.strip():t.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
        else:ET.SubElement(n,'{'+M+'}v').text=value
    else:
        if isinstance(value,bool):n.set('t','b');value=int(value)
        ET.SubElement(n,'{'+M+'}v').text=str(value)


def clean(raw,contract):
    if sf.digest(raw)!=contract['selected_sha256']:
        raise sf.ReviewError('Selected DAILY template bytes differ from the pinned approved attachment.')
    p=read_package(raw); mapping=sheets(p)
    strings=[]
    if 'xl/sharedStrings.xml' in p:
        strings=list(ET.fromstring(p['xl/sharedStrings.xml']))
    for title,part in mapping.items():
        root=ET.fromstring(p[part])
        for c in root.findall('.//m:sheetData/m:row/m:c',NS):
            if c.get('t')=='s':
                idx=int(c.find('m:v',NS).text);clear(c);c.set('t','inlineStr')
                inline=ET.SubElement(c,'{'+M+'}is')
                for node in strings[idx]:inline.append(deepcopy(node))
            if c.find('m:f',NS) is not None:
                for cached in c.findall('m:v',NS):c.remove(cached)
                c.attrib.pop('t',None)
            # All external sample links are forbidden in the clean master.
            f=c.find('m:f',NS)
            if f is not None and re.search(r'HYPERLINK\("(?:https?:|file:)',f.text or '',re.I):clear(c)
        if title=='Summary':
            for address in contract['cleanup']['summary_inputs']+contract['cleanup']['summary_sample_instructions']:
                clear(cell(root,address))
        for spec in contract['shift_details'].values():
            if title!=spec['sheet']:continue
            for r in range(spec['first_row'],spec['last_row']+1):
                for col in spec['clear_input_columns']:clear(cell(root,f'{col}{r}'))
            for key in ('billing_name','billing_hours','daily_notes'):clear(cell(root,spec[key]))
        if title=='Rejects & Photos':
            for r in list(range(8,21))+list(range(24,91)):
                for cc in range(1,12):clear(cell(root,chr(cc+64)+str(r)))
        hf=root.find('m:headerFooter',NS)
        if hf is not None:
            for child in hf:child.text='&LPPG Quality&CInternal held report&RPage &P of &N'
        links=root.find('m:hyperlinks',NS)
        if links is not None:
            root.remove(links)
        p[part]=xml(root)
    # Drop sample drawings by hash while retaining hero/logo anchors untouched.
    remove=set()
    for name,blob in p.items():
        if name.startswith('xl/media/') and sf.digest(blob) in contract['remove_sample_media_sha256']:remove.add(name)
    for name in list(p):
        if not re.fullmatch(r'xl/drawings/drawing[^/]*\.xml',name):continue
        root=ET.fromstring(p[name]); refs=relationships(p,name)
        for anchor in list(root):
            if any(refs.get(b.get('{'+R+'}embed')) in remove for b in anchor.findall('.//a:blip',NS)):root.remove(anchor)
        p[name]=xml(root)
    for name in list(p):
        if name.endswith('.rels'):
            root=ET.fromstring(p[name]); folder=posixpath.dirname(posixpath.dirname(name))
            for n in list(root):
                target=n.get('Target',''); absolute=posixpath.normpath(posixpath.join(folder,target)).lstrip('/') if not target.startswith('/') else target.lstrip('/')
                if n.get('TargetMode')=='External' or absolute in remove or target.endswith(('sharedStrings.xml','calcChain.xml')):root.remove(n)
            p[name]=xml(root)
        elif name.startswith('xl/charts/') and name.endswith('.xml'):
            root=ET.fromstring(p[name])
            for cache in root.findall('.//c:numCache',NS)+root.findall('.//c:strCache',NS):
                for n in list(cache):
                    if n.tag.endswith('}pt'):cache.remove(n)
                    elif n.tag.endswith('}ptCount'):n.set('val','0')
            p[name]=xml(root)
    for name in remove|{'xl/sharedStrings.xml','xl/calcChain.xml','docProps/custom.xml'}:p.pop(name,None)
    # Remove custom metadata relationship and all orphaned content-type entries.
    ct=ET.fromstring(p['[Content_Types].xml'])
    for n in list(ct):
        if n.get('PartName') and n.get('PartName').lstrip('/') not in p:ct.remove(n)
    p['[Content_Types].xml']=xml(ct)
    rels=ET.fromstring(p['_rels/.rels'])
    for n in list(rels):
        if n.get('Target','').endswith('custom.xml'):rels.remove(n)
    p['_rels/.rels']=xml(rels)
    p['docProps/core.xml']=b'<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>PPG Quality</dc:creator><dc:title>Daily report template</dc:title></cp:coreProperties>'
    result=packed(p)
    audit(result,contract)
    return result


def audit(raw,contract):
    p=read_package(raw)
    text='\n'.join(v.decode('utf-8','ignore') for k,v in p.items() if k.endswith(('.xml','.rels')))
    for token in contract['cleanup']['sample_tokens']:
        if token in text:raise sf.ReviewError('Template sample content remains: '+token)
    hashes={sf.digest(v) for k,v in p.items() if k.startswith('xl/media/')}
    if set(contract['remove_sample_media_sha256'])&hashes:raise sf.ReviewError('Sample photo bytes remain.')
    if not set(contract['preserve_media_sha256'])<=hashes:raise sf.ReviewError('Required brand image bytes were changed or lost.')
    if any(n.get('TargetMode')=='External' for k,v in p.items() if k.endswith('.rels') for n in ET.fromstring(v)):
        raise sf.ReviewError('External template relationships remain.')
    return {'sample_tokens_absent':True,'sample_photos_absent':True,'brand_hashes_preserved':True}
