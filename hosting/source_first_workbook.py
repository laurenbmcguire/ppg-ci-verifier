"""Pinned DAILY-template population and independent saved-package verification.

Only approved scoped records enter the workbook. Native OOXML is edited directly
so template layout, print controls and original selected image bytes survive.
"""
from collections import Counter
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from hosting import source_first as sf
from hosting import source_first_template as tx
from hosting import source_first_layout as lx

ASSETS=Path(__file__).resolve().parent/'assets'/'source_first'
HEADER_FIELDS=('area','service_type','description','paying_client','client_contact','worksite','prepared_by','part_name')


def contract():
    c=json.loads((ASSETS/'contract.json').read_text())
    if sf.digest((ASSETS/c['clean_filename']).read_bytes())!=c['clean_sha256']:
        raise sf.ReviewError('The pinned clean DAILY template failed its hash check.')
    return c


def generator_hash():
    return sf.digest({p.name:sf.digest(p.read_bytes()) for p in (Path(__file__),Path(tx.__file__),Path(sf.__file__),Path(lx.__file__))})


def header(value,cap):
    if not isinstance(value,dict) or set(value)-set(HEADER_FIELDS)-{'photo_choices','evidence'}:
        raise sf.ReviewError('Unknown report details.')
    out={}
    for key in HEADER_FIELDS:
        v=value.get(key,'')
        if not isinstance(v,str) or len(v)>500 or any(ord(c)<32 and c not in '\n\t' for c in v):
            raise sf.ReviewError('Report details must be bounded plain text.')
        out[key]=v
    evidence=value.get('evidence','')
    if not isinstance(evidence,str) or len(evidence)>2000 or any(out.values()) and len(evidence.strip())<5:
        raise sf.ReviewError('Add evidence for manually supplied report details. These are reviewer statements, not downloaded source values.')
    out['evidence']=evidence.strip()
    out['origin']='reviewer_entered_report_metadata'
    choices=value.get('photo_choices',{})
    if not isinstance(choices,dict) or len(choices)>11:
        raise sf.ReviewError('This template supports up to eleven explicitly selected defect photos.')
    for item,sha in choices.items():
        if not any(p['record_id']==item and p.get('sha256')==sha and p.get('status')=='supported' for p in cap['photos']):
            raise sf.ReviewError('Selected photo is not supported evidence for that exact source record.')
    out['photo_choices']=deepcopy(choices);out['reviewed']=True
    return out


from copy import deepcopy


def scope_findings(rows,head,cap,c):
    out=[]
    def hold(code,text):out.append(dict(code=code,text=text,blocking=True))
    if not head.get('reviewed'):hold('header_review_required','Review report details and explicitly select any representative defect photos.')
    out.extend(lx.findings(rows))
    for item,sha in head.get('photo_choices',{}).items():
        if not any(r['id']==item and r['resolved']['rejected'] for r in rows):hold('photo_scope','A selected defect photo belongs to a record without a verified rejected quantity.')
        if not any(p.get('record_id')==item and p.get('sha256')==sha and p.get('status')=='supported' and p.get('record_hash')==r['record_hash'] for p in cap['photos'] for r in rows if r['id']==item):
            hold('photo_changed','A selected photo changed or is no longer supported. Review this snapshot and select its exact source photo again.')
    out.append(dict(code='labor_not_mapped',text='Labor hours and inspector names are not mapped in this inspection-only pilot. They remain unavailable in the held report.',blocking=False))
    return out


def _native_layout(raw):
    p=tx.read_package(raw);out={};names=ET.fromstring(p['xl/workbook.xml']).findall('m:definedNames/m:definedName',tx.NS)
    for index,(name,part) in enumerate(tx.sheets(p).items()):
        root=ET.fromstring(p[part]);out[name]={k:tx.xml(n) for k in ('sheetPr','mergeCells','cols','pageMargins','pageSetup','printOptions','sheetViews','headerFooter','rowBreaks') if (n:=root.find('m:'+k,tx.NS)) is not None}
        out[name]['print_names']=[(n.get('name'),n.text) for n in names if n.get('localSheetId')==str(index) and n.get('name','').startswith('_xlnm.Print')]
        out[name]['validation_ranges']=[dict(n.attrib) for n in root.findall('m:dataValidations/m:dataValidation',tx.NS)]
        drawing=root.find('m:drawing',tx.NS)
        if drawing is not None:
            dr=ET.fromstring(p[tx.relationships(p,part)[drawing.get('{'+tx.R+'}id')]])
            out[name]['chart_anchors']=[tx.xml(n) for n in dr if n.find('.//c:chart',tx.NS) is not None]
    return out


def build(rows,head,cap,bind,blobs):
    c=contract();holds=[f for f in scope_findings(rows,head,cap,c) if f['blocking']]
    if holds:raise sf.ReviewError(holds[0]['text'])
    template=(ASSETS/c['clean_filename']).read_bytes();p=tx.read_package(template);parts=tx.sheets(p)
    roots={name:ET.fromstring(p[part]) for name,part in parts.items()}
    c,layout=lx.expand(p,roots,c,rows)
    parts=tx.sheets(p)
    expected={};locations={};photo_expect=[];text_entries=[];text_locations={}
    def actual(sheet,address,value=None,formula=None):
        tx.write(roots[sheet],address,value,formula)
        expected[(sheet,address)]={'value':value,'formula':formula.lstrip('=') if formula else None}
    def put(sheet,address,value=None,formula=None):
        # Calls in the original Summary remain relative to its selected layout.
        # Its part-table insertions move the entire dependent lower section.
        if sheet=='Summary':
            address=lx.moved(address,30,layout['part_extra'])
            if formula:
                formula=re.sub(r"('[^']+'![A-Z$]+[0-9]+|_[A-Za-z]+![A-Z$]+[0-9]+)|([A-Z$]+[0-9]+)",
                    lambda m:m[0] if m[1] else lx.moved(m[0],30,layout['part_extra']),formula)
        actual(sheet,address,value,formula)
    def display(row,field,value):
        if not lx.needs_continuation(field,value):return value
        key=(row['id'],field)
        if key not in text_locations:
            first=5+len(text_entries);text_locations[key]=first
            for i,text in enumerate(lx.text_segments(value),1):text_entries.append(dict(record_id=row['id'],field=field,segment=i,text=text))
        return 'Full text: Source Text row '+str(text_locations[key])
    # Preserve internal navigation, then rebuild all affected calculations. This
    # prevents stale sample-specific category, rank and shift-state logic.
    for title,root in roots.items():
        for cell in root.findall('.//m:sheetData/m:row/m:c',tx.NS):
            f=cell.find('m:f',tx.NS)
            if f is not None:
                value=f.text or ''
                link=re.fullmatch(r'HYPERLINK\("(#[^"]+)","([^"]+)"\)',value)
                actual(title,cell.get('r'),link[2] if link else None,value if link else None)
        for cf in list(root.findall('m:conditionalFormatting',tx.NS)):
            if any('holes' in (n.text or '').lower() or '$N' in (n.text or '') or '$O' in (n.text or '') for n in cf.findall('.//m:formula',tx.NS)):
                root.remove(cf)
    day=cap['scope']['business_date'];serial=(date.fromisoformat(day)-date(1899,12,30)).days
    job=cap['scope']['job'];summary='Summary';photo_sheet='Rejects & Photos'
    for key,address in c['summary'].items():
        if key in HEADER_FIELDS:put(summary,address,head.get(key) or None)
    put(summary,'D13',job);put(summary,'D14',serial)
    put(summary,'B5','Daily Report · HELD')
    title=f"{job} · {day} · reviewed source snapshot"
    put(summary,'B6',title)
    put(summary,'B10',f'{len(rows)} recorded inspections. Shift completion and future submissions are not established.')
    put(summary,'B25','Totals include every approved recorded row once. Blank shifts mean no rows were captured, not that production was completed or not run.')
    put(summary,'B29',rows[0]['resolved']['part'] if rows else None)
    inspected_for={r['resolved']['inspected_for'] for r in rows if r['resolved']['inspected_for'] is not None}
    put(summary,'H29',next(iter(inspected_for)) if len(inspected_for)==1 else 'See shift detail' if inspected_for else None)
    put(summary,'B33',f"Matches reviewed snapshot {cap['snapshot_hash'][:16]}. Original source remains unchanged. Human release held.")
    put(summary,'B41','DEFECT PHOTO COVERAGE')
    put(summary,'B42','Defect quantities and reasons are recorded below. No unverified holes/glue categories are inferred from comments.')
    for address in ('B43','D43','G43','J43','B44','D44','G44','J44'):put(summary,address,None)
    put(summary,'B45','Explicit representative photo selections only. Complete capture and selection counts appear below.')
    put(summary,'B74','This report contains the exact reviewed business-date snapshot. It does not assert shift completion or future data coverage. Corrections are separately recorded review overlays. Labor and inspector fields are not mapped in this pilot. Regenerate after source changes; edits to this workbook invalidate its saved verification. Hidden _Source is typed provenance, not secrecy. Long fields are reproduced completely in Source Text.')
    put(summary,'B82','PPG Quality · Source-first daily report · Internal hold. No automatic client delivery.')
    # Retain a truthful source-record unit instead of calling arbitrary scans boxes.
    for address in ('B8','F20'):put(summary,address,'Records')
    for address in ('G48','H48'):put(summary,address,None)
    totals={f:sum(r['resolved'][f] for r in rows) for f in sf.QUANTITIES}
    selected=head.get('photo_choices',{})
    def photo_status(item):
        available=[p for p in cap['photos'] if p.get('record_id')==item]
        if any(p.get('status')=='supported' for p in available):return 'Not selected'
        return 'Unavailable / not captured' if available else 'No captured photo'
    for index,spec in enumerate(c['shift_details'].values(),21):
        sheet=spec['sheet'];subset=[r for r in rows if r['resolved']['shift']==sheet];start,end=spec['first_row'],spec['last_row'];total=spec['total_row']
        put(sheet,'A4',title+' · '+sheet)
        for address,source in {'C7':'D13','K7':'M13','C8':'D16','K8':'M14','C9':'D17','K9':'M15','C10':'D14','G10':'D15','K10':'M16'}.items():
            value=expected[(summary,source)]['value'];put(sheet,address,value,f'=Summary!{source}' if value is not None else '=IF(Summary!'+source+'="","",Summary!'+source+')')
        put(sheet,'C11','Reviewed source snapshot. Completion is unknown. Labor and inspector fields are not mapped.')
        for rr in range(start,end+1):
            for col in range(1,16):put(sheet,chr(col+64)+str(rr),None)
        for rr,row in enumerate(subset,start):
            locations[row['id']]=(sheet,rr)
            for field,col in spec['columns'].items():
                if field in ('photo','holes','glue'):continue
                if field=='date':value=serial
                elif field=='shift':value=row['values']['shift'].get('value')
                else:value=row['resolved'][field]
                # Certified is the independently checked source value, not a
                # blank-coercing calculation; the reconciliation is independent.
                put(sheet,col+str(rr),display(row,field,value))
            put(sheet,'M'+str(rr),photo_status(row['id']) if row['resolved']['rejected'] and row['id'] not in selected else None)
        quantities={f:sum(r['resolved'][f] for r in subset) for f in sf.QUANTITIES}
        for col,inputcol,field in (('A','G',None),('C','G','inspected'),('E','K','certified'),('G','J','rejected')):
            value=len(subset) if field is None else quantities[field]
            put(sheet,f'{col}{total}',value,f'={"COUNT" if field is None else "SUM"}({inputcol}{start}:{inputcol}{end})')
        put(sheet,f'A{total-1}','Records inspected')
        for col,num in (('J','G'),('L','E')):
            value=(quantities['rejected' if col=='J' else 'certified']/quantities['inspected']) if quantities['inspected'] else ''
            put(sheet,f'{col}{total}',value,f'=IF(C{total}=0,"",{num}{total}/MAX(1,C{total}))')
        for key in ('billing_name','billing_hours','billing_total'):put(sheet,spec[key],'Unavailable')
        put(sheet,'D'+str(int(spec['billing_total'][1:])+1),'Unavailable')
        put(sheet,'F'+str(int(spec['billing_hours'][1:])),'Labor and inspector fields are not mapped in this pilot.')
        put(sheet,'N15',None);put(sheet,'O15',None)
        put(summary,'D'+str(index),'Recorded' if subset else 'No recorded rows')
        for col,source,field in (('F','A',None),('G','C','inspected'),('I','E','certified'),('K','G','rejected')):
            value=(len(subset) if field is None else quantities[field]) if subset else ''
            put(summary,f'{col}{index}',value,f'=IF(\'{sheet}\'!A{total}=0,"",\'{sheet}\'!{source}{total})')
        for col,num,field in (('L','K','rejected'),('M','I','certified')):
            value=quantities[field]/quantities['inspected'] if quantities['inspected'] else ''
            put(summary,f'{col}{index}',value,f'=IF(G{index}="","",IF(G{index}=0,"",{num}{index}/MAX(1,G{index})))')
        put(summary,'N'+str(index),'Unavailable');put(summary,'P'+str(index),'Unavailable')
    for col,field in (('F',None),('G','inspected'),('I','certified'),('K','rejected')):put(summary,col+'24',len(rows) if field is None else totals[field],f'=SUM({col}21:{col}23)')
    for col,num,field in (('L','K','rejected'),('M','I','certified')):put(summary,col+'24',totals[field]/totals['inspected'] if totals['inspected'] else '',f'=IF(G24=0,"",{num}24/MAX(1,G24))')
    put(summary,'N24','Unavailable');put(summary,'P24','Unavailable')
    for dest,source in {'B9':'F24','D9':'G24','G9':'I24','J9':'K24','M9':'L24','O9':'M24'}.items():put(summary,dest,expected[(summary,source)]['value'],'='+source)
    for dest,source in {'J29':'G24','L29':'I24','N29':'K24','P29':'L24','J30':'J29','L30':'L29','N30':'N29','P30':'P29'}.items():put(summary,dest,expected[(summary,source)]['value'],'='+source)
    # Rebuild hidden metrics and rank with explicit source-row references. Equal
    # reject quantities never select the first identifier more than once.
    for row in roots['_Calc'].findall('.//m:sheetData/m:row',tx.NS):
        for n in row:put('_Calc',n.get('r'),None)
    rejected=sorted([r for r in rows if r['resolved']['rejected']],key=lambda r:(-r['resolved']['rejected'],r['id']))
    for ri,row in enumerate(rows,2):
        sh,rn=locations[row['id']]
        for col,source,field in (('A','B','shift'),('B','E','identifier'),('C','G','inspected'),('D','J','rejected'),('G','D','part')):
            # This formula links to the displayed cell, which may contain a
            # continuation reference. Exact source values remain in _Source.
            value=expected[(sh,f'{source}{rn}')]['value']
            put('_Calc',f'{col}{ri}',value,f"='{sh}'!{source}{rn}")
        put('_Calc','H'+str(ri),'Yes' if row['id'] in selected else 'No')
    metrics={'K2':len(rows),'K4':len(rejected),'K6':len(selected),'K8':len([r for r in rejected if r['id'] not in selected])}
    for address,value in metrics.items():put('_Calc',address,value)
    put(summary,'M43','Selected photos');put(summary,'O43','No selected photo')
    put(summary,'M44',len(selected),'=_Calc!K6');put(summary,'O44',metrics['K8'],'=_Calc!K8')
    shown=rejected[:13]
    put(summary,'B47','TOP 13 REJECTED RECORDS · quantity, then source record ID')
    source_end=len(rows)+1
    source_ids={r['id']:i for i,r in enumerate(sorted(rows,key=lambda r:r['id']),1)}
    for ri,row in enumerate(rows,2):
        ordinal=source_ids[row['id']]
        rank=1+sum(r['resolved']['rejected']>row['resolved']['rejected'] or r['resolved']['rejected']==row['resolved']['rejected'] and r['id']<row['id'] for r in rows)
        put('_Calc','L'+str(ri),ordinal)
        put('_Calc','F'+str(ri),rank,f'=COUNTIF($D$2:$D${source_end},">"&D{ri})+COUNTIFS($D$2:$D${source_end},D{ri},$L$2:$L${source_end},"<"&L{ri})+1')
    for i in range(13):
        outrow=49+i;cr=layout['rank_start']+i;n=i+1
        row=rejected[i] if i<len(rejected) else None;v=row['resolved'] if row else {}
        values={'A':n if row else None,'B':display(row,'identifier',v['identifier']) if row else None,
                'C':v.get('inspected'),'D':v.get('rejected'),'G':v['rejected']/v['inspected'] if row and v['inspected'] else ''}
        for col,val in values.items():
            if col in ('B','C','D'):
                formula=f'=IF({n}>COUNTIF($D$2:$D${source_end},">0"),"",INDEX(${col}$2:${col}${source_end},MATCH({n},$F$2:$F${source_end},0)))'
            elif col=='G':formula=f'=IF(C{cr}="","",IF(C{cr}=0,"",D{cr}/MAX(1,C{cr})))'
            else:formula=f'=IF({n}>COUNTIF($D$2:$D${source_end},">0"),"",{n})'
            put('_Calc',col+str(cr),val,formula)
        put('_Calc','M'+str(cr),v.get('certified'),f'=IF({n}>COUNTIF($D$2:$D${source_end},">0"),"",INDEX($E$2:$E${source_end},MATCH({n},$F$2:$F${source_end},0)))')
        put('_Calc','N'+str(cr),v.get('rejected'),f'=IF(D{cr}="","",D{cr})')
        for outcol,incol in {'B':'A','C':'B','E':'C','F':'D','I':'G'}.items():put(summary,outcol+str(outrow),values[incol],f'=IF(_Calc!{incol}{cr}="","",_Calc!{incol}{cr})')
        for col in ('G','H'):put(summary,col+str(outrow),None)
    for col,field in (('E','inspected'),('F','rejected')):put(summary,col+'62',sum(r['resolved'][field] for r in shown),f'=SUM({col}49:{col}61)')
    put(summary,'G62',None);put(summary,'H62',None)
    put(summary,'B62','Shown-record subtotal')
    denom=sum(r['resolved']['inspected'] for r in shown)
    put(summary,'I62',sum(r['resolved']['rejected'] for r in shown)/denom if denom else '', '=IF(E62=0,"",F62/MAX(1,E62))')
    for r,label,result,note in [(66,'Defect categories','Unavailable','No category totals were inferred from comments.'),(67,'Reject quantity vs. inspected','Checked','Every approved row was checked independently.'),(68,'Rejected records without selected photo',str(metrics['K8']),'Missing images remain explicit.'),(69,'Billable hours','Unavailable','Labor fields are not mapped in this pilot.'),(70,'Inspector name','Unavailable','Inspector fields are not mapped in this pilot.'),(71,'Certified + rejected = inspected','Checked','Independently checked source quantities are retained.')]:
        for col,value in (('B',label),('E',result),('J',note)):put(summary,col+str(r),value)
    # No generic category chart should imply holes/glue counts. Keep its native
    # dimensions/anchor but rebind its two series to actual inspected/rejected.
    for name in list(p):
        if name.startswith('xl/charts/') and name.endswith('.xml'):
            chart=ET.fromstring(p[name])
            for t in chart.findall('.//c:title//a:t',tx.NS): t.text='Top 13 rejected records'
            for index,ser in enumerate(chart.findall('.//c:ser',tx.NS)):
                for f in ser.findall('.//c:cat//c:f',tx.NS):f.text=f"'Summary'!$C${49+layout['part_extra']}:$C${61+layout['part_extra']}"
                for f in ser.findall('.//c:val//c:f',tx.NS):f.text=f"'_Calc'!${'M' if index==0 else 'N'}${layout['rank_start']}:${'M' if index==0 else 'N'}${layout['rank_start']+12}"
                txnode=ser.find('c:tx',tx.NS)
                if txnode is not None:
                    txnode.clear();ET.SubElement(txnode,'{'+tx.C+'}v').text='Certified' if index==0 else 'Rejected'
            # Cache the correct distinct category IDs and corresponding totals.
            for ser in chart.findall('.//c:ser',tx.NS):
                for cache in ser.findall('.//c:numCache',tx.NS)+ser.findall('.//c:strCache',tx.NS):
                    for n in list(cache):cache.remove(n)
                    ET.SubElement(cache,'{'+tx.C+'}ptCount',{'val':'0'})
            p[name]=tx.xml(chart)
    put(photo_sheet,'A4',title);put(photo_sheet,'I7','Source record ID');put(photo_sheet,'J7',None);put(photo_sheet,'K7',None)
    for r in range(8,21+layout['reject_extra']):
        for cc in range(1,12):put(photo_sheet,chr(cc+64)+str(r),None)
    for i,row in enumerate(rejected,8):
        v=row['resolved'];vals={'A':f'R{i-7:02d}','B':row['values']['shift'].get('value'),'C':display(row,'part',v['part']),'D':display(row,'identifier',v['identifier']),'E':display(row,'reject_reason',v['reject_reason']),'F':v['rejected'],'G':display(row,'comments',v['comments']),'H':photo_status(row['id']),'I':row['id']}
        for col,val in vals.items():put(photo_sheet,col+str(i),val)
    photo_total=21+layout['reject_extra']
    put(photo_sheet,'F'+str(photo_total),totals['rejected'],f'=SUM(F8:F{photo_total-1})');put(photo_sheet,'J'+str(photo_total),None);put(photo_sheet,'K'+str(photo_total),None)
    slots=[(col,r+layout['reject_extra']) for r in (24,41,58,75) for col in ('A','D','G') if not (r==75 and col=='G')]
    for (item,sha),(col,base) in zip(selected.items(),slots):
        row=next(r for r in rejected if r['id']==item);photo=next(p for p in cap['photos'] if p['record_id']==item and p.get('sha256')==sha)
        blob=blobs.get(sha)
        if blob is None or sf.digest(blob)!=sha:raise sf.ReviewError('Selected original photo bytes are missing or changed.')
        put(photo_sheet,f'{col}{base}',f"Record {item} · {row['resolved']['reject_reason'] or 'Recorded defect'}")
        put(photo_sheet,f'{col}{base+1}','ID '+row['resolved']['identifier'])
        put(photo_sheet,f'{col}{base+2}',row['resolved']['shift']+' · Part '+row['resolved']['part'])
        put(photo_sheet,f'{col}{base+13}',display(row,'comments',row['resolved']['comments']))
        put(photo_sheet,f'{col}{base+15}','Original source photo · '+sha[:12])
        sh,rn=locations[item];link=f'HYPERLINK("#\'{photo_sheet}\'!{col}{base}","View source photo")'
        put(sh,'M'+str(rn),'View source photo',link)
        table_row=8+next(i for i,r in enumerate(rejected) if r['id']==item)
        put(photo_sheet,'H'+str(table_row),'View source photo',link)
        _add_photo(p,roots[photo_sheet],parts[photo_sheet],blob,sha,col,base+3,item)
        photo_expect.append(dict(record_id=item,sha256=sha,sheet=photo_sheet,cell=f'{col}{base+3}',source_field=photo['field'],record_hash=photo['record_hash'],source_version=row['source_version']))
    # Explicit text formats prevent viewers from coercing numeric-looking IDs.
    text_cells={(summary,'B29')}
    for row in rows:
        sh,rn=locations[row['id']];text_cells.update({(sh,'D'+str(rn)),(sh,'E'+str(rn))})
    text_cells.update((summary,'C'+str(r)) for r in range(49,62))
    text_cells.update(('_Calc',col+str(r)) for r in range(2,len(rows)+2) for col in ('B','G'))
    text_cells.update(('_Calc','B'+str(r)) for r in range(layout['rank_start'],layout['rank_start']+13))
    text_cells.update((photo_sheet,col+str(r)) for r in range(8,21+layout['reject_extra']) for col in ('C','D','I'))
    # All parts receive distinct exact source-string rows, with formula totals
    # referencing the complete reviewed detail range rather than a top-N list.
    part_totals={}
    for i,part in enumerate(layout['parts'],29):
        part_rows=[r for r in rows if r['resolved']['part']==part]
        part_totals[part]={f:sum(r['resolved'][f] for r in part_rows) for f in sf.QUANTITIES}
        actual(summary,'B'+str(i),display(part_rows[0],'part',part))
        actual(summary,'E'+str(i),head.get('part_name') or None if len(layout['parts'])==1 else None)
        purposes={r['resolved']['inspected_for'] for r in part_rows if r['resolved']['inspected_for'] is not None}
        actual(summary,'H'+str(i),display(next(r for r in part_rows if r['resolved']['inspected_for'] is not None),'inspected_for',next(iter(purposes))) if len(purposes)==1 else 'See detail' if purposes else None)
        for dest,source,field in [('J','C','inspected'),('L','E','certified'),('N','D','rejected')]:
            formula='=SUMIF(_Calc!$J$2:$J$'+str(len(rows)+1)+','+str(i-28)+',_Calc!$'+source+'$2:$'+source+'$'+str(len(rows)+1)+')'
            actual(summary,dest+str(i),part_totals[part][field],formula)
        actual(summary,'P'+str(i),part_totals[part]['rejected']/part_totals[part]['inspected'] if part_totals[part]['inspected'] else '',f'=IF(J{i}=0,"",N{i}/MAX(1,J{i}))')
        text_cells.add((summary,'B'+str(i)))
    end=29+layout['part_extra'];total=end+1
    for col,field in [('J','inspected'),('L','certified'),('N','rejected')]:actual(summary,col+str(total),totals[field],f'=SUM({col}29:{col}{end})')
    actual(summary,'P'+str(total),totals['rejected']/totals['inspected'] if totals['inspected'] else '',f'=IF(J{total}=0,"",N{total}/MAX(1,J{total}))')
    # The source block includes certified quantities separately; no inferred
    # relationship substitutes for their reviewed source representation.
    for ri,row in enumerate(rows,2):
        sh,rn=locations[row['id']]
        put('_Calc','E'+str(ri),row['resolved']['certified'],f"='{sh}'!K{rn}")
        put('_Calc','I'+str(ri),row['id'])
        put('_Calc','J'+str(ri),layout['parts'].index(row['resolved']['part'])+1)
    supported=sum(p.get('status')=='supported' for p in cap['photos'])
    unavailable=len(cap['photos'])-supported
    coverage=f"Representative photos: {len(selected)} selected; {supported} supported captured; {supported-len(selected)} supported not selected; {unavailable} unavailable or not attempted. {metrics['K8']} rejected records have no selected photo."
    put(summary,'B42',coverage)
    put(photo_sheet,'A6','ALL REJECTED RECORDS · exact source record IDs below')
    put(summary,'B10',f'{len(rows)} recorded inspections. See all parts and complete rejected-record traceability. Completion is unknown.')
    lx.write_ledger(rows,cap,actual)
    lx.make_text_sheet(p,roots,text_entries,actual)
    parts=tx.sheets(p)
    # Row height expands to fit a conservative line count, never shrinks text.
    for spec in c['shift_details'].values():
        for rr in range(spec['first_row'],spec['last_row']+1):
            node=tx.cell(roots[spec['sheet']],'A'+str(rr))
            rownode=next(n for n in roots[spec['sheet']].find('m:sheetData',tx.NS) if n.get('r')==str(rr))
            vals=[expected.get((spec['sheet'],col+str(rr)),{}).get('value') for col in ('C','D','E','F','H','I','L','M')]
            height=max([31.5]+[15*lx.printed_lines(v) for v in vals if v is not None])
            rownode.set('ht',str(min(135,height)));rownode.set('customHeight','1')
    for rr in range(8,photo_total):
        rownode=next(n for n in roots[photo_sheet].find('m:sheetData',tx.NS) if n.get('r')==str(rr))
        vals=[expected.get((photo_sheet,col+str(rr)),{}).get('value') for col in 'BCDEGHI']
        height=max([31.5]+[15*lx.printed_lines(v,13) for v in vals if v is not None])
        rownode.set('ht',str(min(135,height)));rownode.set('customHeight','1')
    tx.cell(roots[summary],lx.moved('B42',30,layout['part_extra']))
    for rr,height in ((42,60),(43,30),(45,30),(47,30),(62,30),(74,40)):
        rr+=layout['part_extra'];node=next(n for n in roots[summary].find('m:sheetData',tx.NS) if n.get('r')==str(rr));node.set('ht',str(height));node.set('customHeight','1')
    text_cells={(s,lx.moved(a,30,layout['part_extra']) if s==summary and a.startswith('C') else a) for s,a in text_cells}
    text_cells.update((s,a) for (s,a),entry in expected.items() if s=='_Source' and isinstance(entry['value'],str))
    _text_formats(p,roots,text_cells)
    wraps={(spec['sheet'],col+str(rr)) for spec in c['shift_details'].values() for rr in range(spec['first_row'],spec['last_row']+1) for col in ('C','D','E','F','H','I','L','M')}
    wraps.update((photo_sheet,col+str(rr)) for rr in range(8,photo_total) for col in ('B','C','D','E','G','H','I'))
    wraps.update((summary,col+str(rr)) for rr in range(29,end+1) for col in ('B','E','H'))
    _wrap_formats(p,roots,wraps)
    for rr in range(29,end+1):
        node=next(n for n in roots[summary].find('m:sheetData',tx.NS) if n.get('r')==str(rr))
        height=max([18]+[15*lx.printed_lines(expected[(summary,col+str(rr))]['value'],16) for col in ('B','E','H') if expected[(summary,col+str(rr))]['value'] is not None])
        node.set('ht',str(min(135,height)));node.set('customHeight','1')
    for title,root in roots.items():
        for dv in root.findall('m:dataValidations/m:dataValidation',tx.NS):
            for formula in dv.findall('m:formula1',tx.NS):
                if 'Complete' in (formula.text or ''):formula.text='"Recorded,No recorded rows"'
        p[parts[title]]=tx.xml(root)
    workbook=ET.fromstring(p['xl/workbook.xml']);calc=workbook.find('m:calcPr',tx.NS)
    if calc is not None:calc.set('fullCalcOnLoad','1');calc.set('forceFullCalc','1');calc.set('calcMode','auto')
    p['xl/workbook.xml']=tx.xml(workbook)
    p['docProps/core.xml'] = ('<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>PPG Quality</dc:creator><dc:title>Held daily source report</dc:title><dc:description>Source review binding '+sf.digest(bind)+'</dc:description></cp:coreProperties>').encode()
    raw=tx.packed(p)
    budgets=lx.package_limits(p,roots,raw)
    verification=verify(raw,rows,head,cap,bind,expected,locations,photo_expect,layout,text_entries)
    verification['part_totals']=part_totals
    verification['output_budgets']=budgets
    return raw,verification


def _text_formats(parts,roots,targets):
    styles=ET.fromstring(parts['xl/styles.xml']);xfs=styles.find('m:cellXfs',tx.NS);cache={}
    for sheet,address in sorted(targets):
        cell=tx.cell(roots[sheet],address);original=int(cell.get('s','0'))
        if original not in cache:
            style=deepcopy(xfs[original]);style.set('numFmtId','49');style.set('applyNumberFormat','1')
            cache[original]=len(xfs);xfs.append(style)
        cell.set('s',str(cache[original]))
    xfs.set('count',str(len(xfs)));parts['xl/styles.xml']=tx.xml(styles)


def _wrap_formats(parts,roots,targets):
    styles=ET.fromstring(parts['xl/styles.xml']);xfs=styles.find('m:cellXfs',tx.NS);cache={}
    for sheet,address in sorted(targets):
        cell=tx.cell(roots[sheet],address);original=int(cell.get('s','0'))
        if original not in cache:
            style=deepcopy(xfs[original]);alignment=style.find('m:alignment',tx.NS)
            if alignment is None:alignment=ET.SubElement(style,'{'+tx.M+'}alignment')
            alignment.set('wrapText','1');alignment.attrib.pop('shrinkToFit',None);style.set('applyAlignment','1')
            cache[original]=len(xfs);xfs.append(style)
        cell.set('s',str(cache[original]))
    xfs.set('count',str(len(xfs)));parts['xl/styles.xml']=tx.xml(styles)


def _add_photo(parts,sheet_root,sheet_part,blob,sha,col,row,item):
    from hosting.photo_embed import image_size
    drawing=sheet_root.find('m:drawing',tx.NS)
    if drawing is None:raise sf.ReviewError('Pinned photo drawing is missing.')
    dpart=tx.relationships(parts,sheet_part)[drawing.get('{'+tx.R+'}id')]
    dr=ET.fromstring(parts[dpart]);rp=tx.relpath(dpart);rels=ET.fromstring(parts[rp])
    rid='rIdSource'+str(len(rels)+1);ext='png' if blob.startswith(b'\x89PNG') else 'jpeg'
    media='xl/media/source_'+sha+'.'+ext;parts[media]=blob
    ET.SubElement(rels,'{'+tx.REL+'}Relationship',{'Id':rid,'Type':tx.R+'/image','Target':'../media/'+media.rsplit('/',1)[-1]})
    size=image_size(blob);scale=min(240/size[0],150/size[1]);cx=int(size[0]*scale*9525);cy=int(size[1]*scale*9525)
    an=ET.SubElement(dr,'{'+tx.D+'}oneCellAnchor');fr=ET.SubElement(an,'{'+tx.D+'}from')
    for k,v in [('col',tx.number_col(col)-1),('colOff',38100),('row',row-1),('rowOff',38100)]:ET.SubElement(fr,'{'+tx.D+'}'+k).text=str(v)
    ET.SubElement(an,'{'+tx.D+'}ext',{'cx':str(cx),'cy':str(cy)})
    pic=ET.SubElement(an,'{'+tx.D+'}pic');nv=ET.SubElement(pic,'{'+tx.D+'}nvPicPr')
    ET.SubElement(nv,'{'+tx.D+'}cNvPr',{'id':str(100+len(rels)),'name':'Source record '+item,'descr':'Original photo SHA256 '+sha})
    ET.SubElement(nv,'{'+tx.D+'}cNvPicPr');bf=ET.SubElement(pic,'{'+tx.D+'}blipFill');ET.SubElement(bf,'{'+tx.A+'}blip',{'{'+tx.R+'}embed':rid})
    ET.SubElement(ET.SubElement(bf,'{'+tx.A+'}stretch'),'{'+tx.A+'}fillRect');sp=ET.SubElement(pic,'{'+tx.D+'}spPr')
    ET.SubElement(ET.SubElement(sp,'{'+tx.A+'}prstGeom',{'prst':'rect'}),'{'+tx.A+'}avLst');ET.SubElement(an,'{'+tx.D+'}clientData')
    parts[dpart]=tx.xml(dr);parts[rp]=tx.xml(rels)


def verify(raw,rows,head,cap,bind,expected,locations,photo_expect,layout=None,text_entries=()):
    """Reopen actual saved bytes, not the pre-save workbook object."""
    from openpyxl import load_workbook
    from hosting.photos import images
    c=contract();audit_contract=deepcopy(c)
    approved_text='\n'.join(str(e['value']) for e in expected.values() if e['value'] is not None)
    audit_contract['cleanup']['sample_tokens']=[t for t in c['cleanup']['sample_tokens'] if t not in approved_text]
    audit_contract['remove_sample_media_sha256']=[h for h in c['remove_sample_media_sha256'] if h not in {p['sha256'] for p in photo_expect}]
    tx.audit(raw,audit_contract)
    # Independently reconstruct the permitted structural delta from the pinned
    # clean template and row counts. No unrelated page/merge/column changes.
    base=tx.read_package((ASSETS/c['clean_filename']).read_bytes());br={n:ET.fromstring(base[p]) for n,p in tx.sheets(base).items()}
    lx.expand(base,br,c,rows)
    for n,p in tx.sheets(base).items():base[p]=tx.xml(br[n])
    actual_layout=_native_layout(raw);wanted_layout=_native_layout(tx.packed(base))
    if any(actual_layout[n]!=v for n,v in wanted_layout.items()):raise sf.ReviewError('Native expansion layout differs from its bounded plan.')
    values=load_workbook(BytesIO(raw),data_only=True);formulas=load_workbook(BytesIO(raw),data_only=False)
    if values.properties.description != 'Source review binding '+sf.digest(bind):
        raise sf.ReviewError('Workbook metadata is not bound to this exact reviewed snapshot and generator.')
    date_cells={(sh,'A'+str(r)) for sh,r in locations.values()}
    for (sheet,address),entry in expected.items():
        v=values[sheet][address].value;want=entry['value'];f=formulas[sheet][address]
        if hasattr(v,'date') and isinstance(want,(int,float)):
            v=(v.date()-date(1899,12,30)).days
        if sheet=='Summary' and address=='D14' or (sheet,address) in date_cells:
            actual=(date(1899,12,30)+__import__('datetime').timedelta(days=v)).isoformat() if isinstance(v,(int,float)) else str(v)
            if actual!=cap['scope']['business_date']:raise sf.ReviewError(f'Saved business date does not match the reviewed date: {sheet}!{address}: {actual!r}, {want!r}.')
        elif not ((want in (None,'') and v is None) or v==want or isinstance(want,(int,float)) and isinstance(v,(int,float)) and abs(v-want)<1e-10):
            raise sf.ReviewError(f'Saved cell {sheet}!{address} differs from approved data.')
        if entry['formula'] is not None and f.value!='='+entry['formula']:
            raise sf.ReviewError('Saved formula differs from the approved generator expression.')
        if entry['formula'] is None and f.data_type=='f':raise sf.ReviewError('Source text was interpreted as a formula.')
    if len(locations)!=len(rows) or len(set(locations.values()))!=len(rows):raise sf.ReviewError('Not every source record is represented exactly once.')
    # Recompute per-row field provenance and aggregate values independently.
    for ri,row in enumerate(rows,2):
        sheet,r=locations[row['id']]
        for calc_col,source_col in (('A','B'),('B','E'),('C','G'),('D','J'),('E','K'),('G','D')):
            if values['_Calc'][f'{calc_col}{ri}'].value!=values[sheet][f'{source_col}{r}'].value:
                raise sf.ReviewError('Saved source-reference cache differs from its displayed source cell.')
        for col,field in [('D','part'),('E','identifier'),('G','inspected'),('J','rejected'),('K','certified')]:
            actual=values[sheet][f'{col}{r}'].value
            want=row['resolved'][field]
            if lx.needs_continuation(field,want):
                actual=''.join(t['text'] for t in text_entries if t['record_id']==row['id'] and t['field']==field)
            if actual!=want or field in ('part','identifier') and type(actual)is not str:
                raise sf.ReviewError('Saved row identifier/quantity provenance failed.')
        if values[sheet][f'B{r}'].value!=row['values']['shift'].get('value'):raise sf.ReviewError('Recorded shift representation changed.')
    lx.verify_ledger(values,rows,cap)
    rejected=sorted((r for r in rows if r['resolved']['rejected']),key=lambda r:(-r['resolved']['rejected'],r['id']))
    for rr,row in enumerate(rejected,8):
        if values['Rejects & Photos']['I'+str(rr)].value!=row['id'] or values['Rejects & Photos']['F'+str(rr)].value!=row['resolved']['rejected']:raise sf.ReviewError('Complete rejected-record traceability differs from the approved source.')
    ledger_ids=[values['_Source']['A'+str(rr)].value for rr in range(2,len(rows)+2)]
    if ledger_ids!=[r['id'] for r in rows] or len(set(ledger_ids))!=len(rows):raise sf.ReviewError('Source ledger record identity coverage failed.')
    for rr,t in enumerate(text_entries,5):
        if values['Source Text']['D'+str(rr)].value!=t['text']:raise sf.ReviewError('Complete source text continuation failed.')
    totals={f:sum(r['resolved'][f] for r in rows) for f in sf.QUANTITIES}
    for col,f in [('G','inspected'),('I','certified'),('K','rejected')]:
        if values['Summary'][col+'24'].value!=totals[f]:raise sf.ReviewError('Saved daily total differs from the complete approved scope.')
    actual=images(raw);source_images=[im for im in actual if im['sha256'] not in c['preserve_media_sha256']]
    if Counter((i['sheet'],i['cell'],i['sha256']) for i in source_images)!=Counter((p['sheet'],p['cell'],p['sha256']) for p in photo_expect):
        raise sf.ReviewError('Saved source photo hashes or relationships do not match the approved record bindings.')
    for sheet in formulas:
        for row in sheet:
            for cell in row:
                if cell.data_type=='f' and (sheet.title,cell.coordinate) not in expected:raise sf.ReviewError('Unexpected unverified formula remains in the workbook.')
                if values[sheet.title][cell.coordinate].data_type=='e':raise sf.ReviewError('A saved formula error remains.')
    return dict(matches_reviewed_snapshot=True,current_sharepoint_claim=False,human_release='held',
                workbook_sha256=sf.digest(raw),binding=bind,record_count=len(rows),totals=totals,
                row_provenance=[dict(record_id=r['id'],record_hash=r['record_hash'],sheet=locations[r['id']][0],row=locations[r['id']][1],
                                    approved_values_hash=sf.digest(r['values'])) for r in rows],
                photo_provenance=photo_expect,cell_manifest_hash=sf.digest({s+'!'+a:v for (s,a),v in expected.items()}),
                formula_expectations_checked=True,formula_cache_values_checked=True,source_reference_cache_values_checked=True,native_layout_preserved=True,layout_expansion=layout,typed_source_ledger_verified=True,
                complete_rejected_record_count=sum(bool(r['resolved']['rejected']) for r in rows),
                top_ranked_count=min(13,sum(bool(r['resolved']['rejected']) for r in rows)),
                printable_text_segments=len(text_entries),
                note='Saved values and supported formula expressions match the sealed reviewed snapshot. Live freshness is a separate ten-minute receipt.')
