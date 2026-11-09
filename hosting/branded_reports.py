"""Explicit PPG client-report layout checks. Never evaluates or rewrites formulas.

Client workbooks are inputs, not authoritative sources. A matching template only
selects a documented mapping; it cannot establish source authenticity or release.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

TEMPLATE_ID='ppg-daily-client-report/1'
SECTIONED_TEMPLATE_ID='ppg-daily-sectioned-report/2'
ALL_FIELDS_TEMPLATE_ID='ppg-daily-all-fields-report/3'
ACTIVE_TABS_TEMPLATE_ID='ppg-daily-active-tabs-report/4'
HEADERS=('Date','Shift','Area','Part number','Identifier / lot','Inspected for','Inspected','Condition','Reject reason','Rejected','Certified','Comments','Inspection photo')
SHIFT_SHEETS={'1st Shift':'1','2nd Shift':'2','3rd Shift':'3'}
QUANTITIES={'inspected':6,'rejected':9,'certified':10}
SUMMARY_BRAND='PPG QUALITY   /   END OF SHIFT DATA AND BILLING REPORT'
DETAIL_BRAND='PPG QUALITY / DAILY INSPECTION REPORT'
from hosting.limits import MAX_FINDINGS  # every finding is stored (bounded only against hostile files)


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def fingerprint(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def cell(rows,row,col):return rows[row-1][col-1] if row<=len(rows) and col<=len(rows[row-1]) else None

def day(value):
    if isinstance(value,datetime):return value.date().isoformat() if value.time().isoformat()=='00:00:00' else None
    if isinstance(value,date):return value.isoformat()
    if isinstance(value,str):
        for fmt in ('%Y-%m-%d','%B %d, %Y'):
            try:return datetime.strptime(value,fmt).date().isoformat()
            except ValueError:pass
    return None

def quantity(value):
    # Numeric-looking strings are not silently cast to source quantities.
    if type(value) not in (int,float) or value<0 or value>10**15:return None
    n=Decimal(str(value))
    return int(n) if n.is_finite() and n==n.to_integral_value() else None

def totals(records):
    result={}
    for field in QUANTITIES:
        values=[r['values'][field] for r in records]
        known=sum(v for v in values if v is not None);over=known>10**15
        result[field]=dict(total=known if values and all(v is not None for v in values) and not over else None,
            known_sum=known if not over else None,numeric_range_exceeded=over,missing_count=sum(v is None for v in values),reported_count=sum(v is not None for v in values))
    return result


def inspect_branded(sources,base):
    """Return None unless branded markers claim this explicit layout."""
    sheets=dict(sources)
    claimed=(cell(sheets.get('Summary',[]),1,2)==SUMMARY_BRAND or any(cell(rows,1,1)==DETAIL_BRAND for name,rows in sources))
    if not claimed:return None
    sectioned=any(any(row and row[0]=='SHIFT INSPECTION RESULTS' for row in rows[15:]) for name,rows in sources if name in SHIFT_SHEETS)
    all_fields=sectioned and (cell(sheets.get('Summary',[]),99,2)=='RESULTS BY PART NUMBER' or cell(sheets.get('Summary',[]),65,2)=='RECORDED RESULTS BY SHIFT')
    active_tabs=all_fields and ('Rejects' in sheets or any(name not in sheets for name in (*SHIFT_SHEETS,'Rejects & Photos')))
    result=dict(base,template_id=ACTIVE_TABS_TEMPLATE_ID if active_tabs else ALL_FIELDS_TEMPLATE_ID if all_fields else SECTIONED_TEMPLATE_ID if sectioned else TEMPLATE_ID,template_mapping_status='matched',records=[],formulas=[],sheets=[],findings=[],finding_counts={},
        limitations=['Only the explicit PPG daily client-report layout is mapped.','Formulas are preserved and structurally checked, never executed; cached values are not trusted.',
                     'No independent source was supplied. Unknown quantities and unobserved shifts are not zero.','Client workbook does not contain stable source record IDs.'],
        source_authenticity='not_supplied',source_reconciliation_status='not_performed',approval_effective=False,release_status='held')
    counts=Counter()
    def finding(code,sheet,row=None,column=None,message=None):
        counts[code]+=1
        if len(result['findings'])<MAX_FINDINGS:result['findings'].append(dict(code=code,severity='blocking',sheet=sheet,row=row,column=column,message=message or code.replace('_',' ')))
    expected={'Summary','Rejects & Photos',*SHIFT_SHEETS}
    allowed=expected|({'Rejects'} if active_tabs else set())
    valid_sheets=(set(sheets)==expected) if not sectioned or (all_fields and not active_tabs) else ('Summary' in sheets and bool(set(sheets)&set(SHIFT_SHEETS)) and not(set(sheets)-allowed) and not('Rejects' in sheets and 'Rejects & Photos' in sheets))
    if active_tabs:
        finding('photo_evidence_not_verified','Workbook',message='An absent photo tab or reference is not evidence that photos are complete, unnecessary or clean. Source photo evidence remains unverified.')
    if not valid_sheets:
        finding('template_sheet_mapping_mismatch','Workbook',message='Expected Summary, three named shift sheets, and Rejects & Photos. No sheet is silently ignored.')
        result['template_mapping_status']='incomplete'
    summary=sheets.get('Summary',[]);project=cell(summary,13,4);report_date=day(cell(summary,14,4))
    result['report_identity']=dict(project_number=project if isinstance(project,str) else None,report_date=report_date)
    if cell(summary,1,2)!=SUMMARY_BRAND or cell(summary,13,2)!='Project Number' or cell(summary,14,2)!='Report Date' or not isinstance(project,str) or not project.strip() or report_date is None:
        finding('template_identity_mapping_mismatch','Summary');result['template_mapping_status']='incomplete'
    summary_headers=[(20,2,'Shift'),(20,6,'Records'),(20,7,'Inspected'),(20,9,'Certified'),(8,2,'Records'),(8,4,'Parts inspected'),(8,7,'Parts certified')]
    if not sectioned:summary_headers += [(20,11,'Rejected'),(8,10,'Parts rejected')]
    else:
        for r,c,allowed in ((20,11,(None,'Rejected','Recorded rejects')),(8,10,(None,'Parts rejected','Recorded rejects'))):
            if cell(summary,r,c) not in allowed:finding('template_header_mapping_mismatch','Summary',r,c);result['template_mapping_status']='incomplete'
        finding('additional_sections_not_verified','Workbook',message='Appended labor, rate, billing, contact, narrative and other business sections are not validated by inspection quantity checks. Their blanks remain unresolved.')
    for r,c,value in summary_headers:
        if cell(summary,r,c)!=value:finding('template_header_mapping_mismatch','Summary',r,c);result['template_mapping_status']='incomplete'
    shift_records={};ranges={};footers={};detail_expected={};absent_shifts=[]
    for name,shift in SHIFT_SHEETS.items():
        if sectioned and name not in sheets:
            absent_shifts.append(shift);shift_records[shift]=[];finding('shift_activity_unverified',name,message='Shift tab is absent; omitted or unobserved activity is not zero or completed.');continue
        rows=sheets.get(name,[]);header=tuple(cell(rows,15,i+1) for i in range(len(HEADERS)))
        markers=[r for r,row in enumerate(rows,1) if r>=16 and row and row[0]=='SHIFT INSPECTION RESULTS']
        footer=markers[0] if len(markers)==1 else None
        data_rows=rows[15:footer-1] if footer else rows[15:]
        header_ok=header==HEADERS
        if sectioned:
            header_ok=all(h==HEADERS[i] or (h is None and i not in (0,1,4,6,10) and all(i>=len(row) or row[i] is None for row in data_rows)) for i,h in enumerate(header))
            if footer:footers[name]=footer
            else:finding('record_boundary_unverified',name,message='Sectioned reports require exactly one explicit shift-results boundary.');result['template_mapping_status']='incomplete'
        mapped=header_ok and cell(rows,1,1)==DETAIL_BRAND and cell(rows,3,1)==name and cell(rows,14,1)=='INSPECTION RECORDS' and (not sectioned or footer is not None)
        if not mapped:finding('template_header_mapping_mismatch',name,15,message='Expected exact inspection headers in row 15. No quantities were inferred from a nearby row.');result['template_mapping_status']='incomplete'
        scope_ok=(cell(rows,7,3)==project and day(cell(rows,8,3))==report_date and cell(rows,9,3)=='Shift '+shift)
        if all_fields:
            # This documented revision has project metadata in rows7–9 instead of a second date/shift block.
            # Physical shift is the exact sheet/title; every record date is checked against Summary D14.
            scope_ok=(cell(rows,7,1)=='Project' and cell(rows,7,3)==project and cell(rows,8,1)=='Service type' and cell(rows,9,1)=='Project description' and cell(rows,9,9)=='Worksite')
        if not scope_ok:
            finding('template_scope_mismatch',name,message='Shift sheet project, date or physical shift disagrees with the daily summary.')
        records=[];numeric_cells=0;last=15
        for rownum,row in enumerate(data_rows,16):
            if not any(v is not None and v!='' for v in row):continue
            last=rownum
            if not mapped:continue
            v=lambda i: row[i] if i<len(row) else None
            vals={field:quantity(v(col)) for field,col in QUANTITIES.items()}
            record=dict(sheet=name,row=rownum,date=day(v(0)),shift_id=str(v(1)) if type(v(1)) is int else (v(1) if isinstance(v(1),str) else None),part=v(3) if isinstance(v(3),str) else None,identifier=v(4) if isinstance(v(4),str) else None,values=vals)
            for field,col in QUANTITIES.items():
                if vals[field] is None:
                    finding('quantity_missing' if v(col) is None or v(col)=='' else 'quantity_invalid',name,rownum,col+1,'Quantity is unknown or invalid; it was not treated as zero.')
                else:numeric_cells+=1
            if record['date']!=report_date or record['shift_id']!=shift:finding('record_scope_mismatch',name,rownum,message='Record date or shift disagrees with its sheet and daily scope.')
            if not isinstance(record['identifier'],str) or not record['identifier'].strip():finding('record_identifier_missing',name,rownum,5)
            if not isinstance(record['part'],str) or not record['part'].strip():finding('part_number_missing',name,rownum,4)
            # Certified and rejected are not automatically a partition of inspected.
            if vals['inspected'] is not None and vals['certified'] is not None and vals['certified']>vals['inspected']:
                finding('certified_exceeds_inspected',name,rownum,11)
            if vals['inspected'] is not None and vals['rejected'] is not None and vals['rejected']>vals['inspected']:
                finding('rejected_exceeds_inspected',name,rownum,10)
            records.append(record)
        if not records:finding('shift_activity_unverified',name,message='No mapped records were reported for this shift. This does not establish zero activity or completion.')
        shift_records[shift]=records;result['records'].extend(records)
        if records:ranges[shift]=(name,16,last)
        if footer:
            for r,row in enumerate(rows[footer:],footer+1):
                if day(row[0] if row else None) is not None or (len(row)>6 and quantity(row[6]) is not None):
                    finding('record_outside_mapped_table',name,r,message='Potential inspection data appears after the declared record boundary; it was not silently counted or discarded.')
            for offset,label,col,fn in ((1,'Inspection records','A','COUNTA'),(2,'Parts inspected','G','SUM'),(3,'Parts certified','K','SUM')):
                coordinate='E'+str(footer+offset);formula=f'={fn}({col}16:{col}{last})' if records else None
                if cell(rows,footer+offset,1)!=label:finding('section_label_mismatch',name,footer+offset,1)
                if formula:detail_expected[(name,coordinate)]=formula
                elif cell(rows,footer+offset,5) not in (None,'Unknown','Not observed','n/a'):finding('empty_shift_result_claim',name,footer+offset,5)
            rejected=totals(records)['rejected']
            if cell(rows,footer+4,5) is not None:
                empty_label='' if all_fields else 'Unreported';partial_label=' known; partial' if all_fields else ' known; incomplete'
                detail_expected[(name,'E'+str(footer+4))]=f'=IF(COUNT(J16:J{last})=0,"{empty_label}",IF(COUNT(J16:J{last})=COUNT(G16:G{last}),SUM(J16:J{last}),SUM(J16:J{last})&"{partial_label}"))'
                if cell(rows,footer+4,1)!='Rejected quantity':finding('section_label_mismatch',name,footer+4,1)
            elif rejected['reported_count']:finding('known_rejects_section_omitted',name,footer+4,5)
            if all_fields:
                for r in (footer+5,footer+6):
                    if cell(rows,r,5) is not None:finding('unknown_rate_claim' if not records or rejected['missing_count'] else 'rate_not_verified',name,r,5,message='Rate semantics are not mapped; no rate was inferred or verified.')

        result['sheets'].append(dict(name=name,role='inspection_detail',header_row=15,data_row_start=16,row_count=len(rows),data_row_count=len(records),numeric_columns_checked=[field for field,col in QUANTITIES.items() if header[col] is not None] if mapped else [],numeric_cells_checked=numeric_cells,mapping_status='mapped' if mapped else 'unmapped',totals=totals(records)))
    # Exact formula grammar for this template; no interpreter and no workbook writes.
    expected_formulas={'B9':'=SUM(F21:F23)','D9':'=SUM(G21:G23)','G9':'=SUM(I21:I23)','F24':'=B9','G24':'=D9','I24':'=G9','K24':'=J9','J29':'=D9','L29':'=G9','N29':'=J9','J30':'=D9','L30':'=G9','N30':'=J9'}
    if sectioned:
        for coordinate in ('K24','J29','L29','N29','J30','L30','N30'):
            letters=''.join(c for c in coordinate if c.isalpha());r=int(''.join(c for c in coordinate if c.isdigit()));c=ord(letters)-64
            if cell(summary,r,c) is None:expected_formulas.pop(coordinate)
    reject_ranges=[]
    for shift,rownum in (('1',21),('2',22),('3',23)):
        if cell(summary,rownum,2)!='Shift '+shift and not(sectioned and shift not in ranges and cell(summary,rownum,2) is None):finding('summary_shift_label_mismatch','Summary',rownum,2)
        if shift in ranges:
            name,start,end=ranges[shift];q="'%s'!"%name
            for col,formula in (('F',f'=COUNTA({q}A{start}:A{end})'),('G',f'=SUM({q}G{start}:G{end})'),('I',f'=SUM({q}K{start}:K{end})')):expected_formulas[col+str(rownum)]=formula
            ref=f'{q}J{start}:J{end}';reject_ranges.append(ref)
            if not sectioned or cell(summary,rownum,11) is not None:expected_formulas['K'+str(rownum)]=f'=IF(COUNT({ref})=0,"Unreported",SUM({ref})&" known")'
            if sectioned and cell(summary,rownum,11) is None and totals(shift_records[shift])['rejected']['reported_count']:finding('known_rejects_summary_omitted','Summary',rownum,11)
        else:
            for col in (6,7,9,11):
                if cell(summary,rownum,col) not in ((None,'n/a') if sectioned else ('n/a',)):finding('unobserved_shift_claim','Summary',rownum,col,message='An unobserved shift must not claim a numeric zero or total.')
    if reject_ranges and (not sectioned or cell(summary,9,10) is not None):
        refs=','.join(reject_ranges);expected_formulas['J9']=f'=IF(COUNT({refs})=0,"Unreported",SUM({refs})&" known")'
    if sectioned:
        if cell(summary,9,10) is None and totals(result['records'])['rejected']['reported_count']:finding('known_rejects_summary_omitted','Summary',9,10)
        for coordinate in ('N29','N30','K24'):
            r=int(coordinate[1:]);c=ord(coordinate[0])-64
            if cell(summary,r,c)=='=J9' and cell(summary,9,10) is None:finding('unknown_total_reference','Summary',r,c,message='Formula references an omitted unknown rejected total; a blank reference may display as zero. It was not treated as a valid zero.')
        if cell(summary,42,2)=='RECORDED RESULTS BY SHIFT':
            if cell(summary,44,2)!='Shift' or cell(summary,44,3)!='Certified':finding('section_label_mismatch','Summary',44)
            for offset,(shift,(name,start,end)) in enumerate(ranges.items(),45):
                if cell(summary,offset,2)!=name:finding('section_label_mismatch','Summary',offset,2)
                expected_formulas['C'+str(offset)]=f"=SUM('{name}'!K{start}:K{end})"
                if cell(summary,offset,4) is not None:
                    expected_formulas['D'+str(offset)]=f"=SUM('{name}'!J{start}:J{end})"
                    if not totals(shift_records[shift])['rejected']['reported_count']:finding('unknown_reject_sum_claim','Summary',offset,4)
                    if cell(summary,44,4) not in ('Recorded rejects','Known rejected quantity'):finding('partial_reject_label_missing','Summary',44,4)
        if all_fields:
            if cell(summary,65,2)!='RECORDED RESULTS BY SHIFT' or tuple(cell(summary,66,c) for c in (2,3,4))!=('Shift','Certified','Known rejects'):
                finding('section_label_mismatch','Summary',65)
            for shift,r in (('1',67),('2',68),('3',69)):
                name=next(n for n,s in SHIFT_SHEETS.items() if s==shift)
                if cell(summary,r,2)!=name:finding('section_label_mismatch','Summary',r,2)
                if shift in ranges:
                    _,start,end=ranges[shift];expected_formulas['C'+str(r)]=f"=SUM('{name}'!K{start}:K{end})"
                    if totals(shift_records[shift])['rejected']['reported_count']:
                        expected_formulas['D'+str(r)]=f"=SUM('{name}'!J{start}:J{end})"
                    elif cell(summary,r,4) is not None:finding('unknown_reject_sum_claim','Summary',r,4)
                elif any(cell(summary,r,c) is not None for c in (3,4)):finding('unobserved_shift_claim','Summary',r)
            if cell(summary,99,2)!='RESULTS BY PART NUMBER' or tuple(cell(summary,100,c) for c in range(2,9))!=('Part number','Part name','Inspected for','Inspected','Certified','Known rejects','Defect rate'):
                finding('part_table_mapping_mismatch','Summary',100)
            part_records=defaultdict(list)
            for rec in result['records']:
                if rec['part']:part_records[rec['part']].append(rec)
            part_rows=[r for r in range(101,len(summary)+1) if any(cell(summary,r,c) is not None for c in range(2,9))]
            actual_parts=[cell(summary,r,2) for r in part_rows]
            if any(not isinstance(v,str) or not v.strip() for v in actual_parts) or Counter(v for v in actual_parts if isinstance(v,str))!=Counter(part_records.keys()):finding('part_table_coverage_mismatch','Summary',101,message='Per-part table must include each observed part exactly once, without inventing missing parts.')
            result['part_totals']={part:totals(rs) for part,rs in part_records.items()}
            folded=Counter(part.casefold() for part in part_records)
            for r in part_rows:
                part=cell(summary,r,2);rs=part_records.get(part,[]) if isinstance(part,str) else []
                if not rs:continue
                t=totals(rs)
                try:numeric_criterion=Decimal(part.strip()).is_finite()
                except Exception:numeric_criterion=False
                if any(char in part for char in '*?~') or part.startswith(('=','<','>')) or numeric_criterion or folded[part.casefold()]>1:
                    finding('part_sumif_criterion_ambiguous','Summary',r,2,message='SUMIF criteria can be numeric, operator-based, wildcard-based or case-insensitive. This part label cannot be accepted as exact text matching.')
                for out,field,detail in (('E','inspected','G'),('F','certified','K'),('G','rejected','J')):
                    if t[field]['reported_count']:
                        expected_formulas[out+str(r)]='='+'+'.join(f"SUMIF('{name}'!D{start}:D{end},B{r},'{name}'!{detail}{start}:{detail}{end})" for name,start,end in ranges.values())
                        if t[field]['missing_count'] and field!='rejected':finding('partial_part_total_claim','Summary',r,ord(out)-64)
                    elif cell(summary,r,ord(out)-64) is not None:finding('unknown_part_total_claim','Summary',r,ord(out)-64)
                if cell(summary,r,8) is not None:finding('unknown_rate_claim' if t['rejected']['missing_count'] else 'rate_not_verified','Summary',r,8)
            register_name='Rejects' if 'Rejects' in sheets else 'Rejects & Photos'
            register=sheets.get(register_name,[])
            if not register and active_tabs:
                finding('reject_register_not_present','Workbook',message='No separate reject register was supplied. Unknown rejected quantities and photo coverage are not inferred from an omitted tab.')
                result['reject_register_records_checked']=0
            else:
                expected_headers=('Shift','Identifier / lot','Part number','Inspected','Known rejected','Reason','Comments',None if register_name=='Rejects' else 'Photo')
                if cell(register,15,1)!='REJECT REGISTER' or tuple(cell(register,16,c) for c in range(1,9))!=expected_headers:
                    finding('reject_register_mapping_mismatch',register_name,16)
                ends=[len(register)+1] if register_name=='Rejects' else [r for r in range(17,len(register)+1) if cell(register,r,1)=='DEFECT PHOTOS']
                if len(ends)!=1:finding('reject_register_boundary_unverified',register_name)
                else:
                    actual=[]
                    for r in range(17,ends[0]):
                        vals=tuple(cell(register,r,c) for c in range(1,6))
                        if not any(v is not None for v in vals):continue
                        actual.append(vals)
                    expected_register=[(rec['sheet'],rec['identifier'],rec['part'],rec['values']['inspected'],rec['values']['rejected']) for rec in result['records'] if rec['values']['rejected'] is not None and rec['values']['rejected']>0]
                    if Counter(canonical(v) for v in actual)!=Counter(canonical(v) for v in expected_register):finding('reject_register_difference',register_name,17,message='Positive known-reject entries differ from mapped inspection rows. Unknown rejects and photos are not inferred.')
                    result['reject_register_records_checked']=len(actual)
                    for r in range(ends[0]+1,len(register)+1):
                        if any(quantity(cell(register,r,c)) is not None for c in (4,5)):finding('reject_record_outside_mapped_table',register_name,r,message='Numeric register-like content appears after the photo boundary and requires review; it was not silently counted or discarded.')
    expected_cells={}
    for coordinate,formula in expected_formulas.items():
        letters=''.join(c for c in coordinate if c.isalpha());rownum=int(''.join(c for c in coordinate if c.isdigit()));col=0
        for letter in letters:col=col*26+ord(letter)-64
        actual=cell(summary,rownum,col);expected_cells[coordinate]=formula
        safe_single_shift_alias=(len(ranges)==1 and coordinate=='K'+str(20+int(next(iter(ranges)))) and actual=='=J9' and cell(summary,9,10)==expected_formulas.get('J9'))
        if actual!=formula and not safe_single_shift_alias:finding('summary_formula_mapping_mismatch','Summary',rownum,col,'Summary formula does not reference the exact mapped detail range. It was not executed or corrected.')
    for (name,coordinate),formula in detail_expected.items():
        r=int(coordinate[1:]);c=ord(coordinate[0])-64
        if cell(sheets[name],r,c)!=formula:finding('section_formula_mapping_mismatch',name,r,c)
    for name,rows in sources:
        for r,row in enumerate(rows,1):
            for c,value in enumerate(row,1):
                if isinstance(value,str) and value.startswith('='):
                    result['formulas'].append(dict(sheet=name,row=r,column=c,formula=value))
                    coordinate='';x=c
                    while x:x,rem=divmod(x-1,26);coordinate=chr(65+rem)+coordinate
                    coordinate+=str(r)
                    if not ((name=='Summary' and coordinate in expected_cells) or (name,coordinate) in detail_expected):finding('unmapped_formula',name,r,c,message='Formula is outside the explicitly mapped summary cells; it was not evaluated.')
    result['sheets'].append(dict(name='Summary',role='summary_formulas',header_row=20,row_count=len(summary),data_row_count=3,numeric_columns_checked=[],formula_cells_checked=len(expected_formulas),mapping_status='formula_structure_only'))
    if 'Rejects' in sheets:result['sheets'].append(dict(name='Rejects',role='known_reject_register',header_row=16,row_count=len(sheets['Rejects']),data_row_count=result.get('reject_register_records_checked',0),numeric_columns_checked=['inspected','known_rejected'],mapping_status='duplicate_presentation_checked_not_new_inspection_rows'))
    if 'Rejects & Photos' in sheets:result['sheets'].append(dict(name='Rejects & Photos',role='supporting_content',row_count=len(sheets.get('Rejects & Photos',[])),data_row_count=0,numeric_columns_checked=[],mapping_status='not_quantity_data'))
    result.update(row_count=sum(len(rows) for _,rows in sources),cell_count=sum(len(row) for _,rows in sources for row in rows),record_count=len(result['records']),totals=totals(result['records']),shift_totals={s:totals(rs) for s,rs in shift_records.items()},numeric_cells_checked=sum(s.get('numeric_cells_checked',0) for s in result['sheets']),absent_shift_ids=absent_shifts,formulas_preserved=True,formula_count=len(result['formulas']),formula_sha256=fingerprint(result['formulas']))
    for field,summary_total in result['totals'].items():
        if summary_total['numeric_range_exceeded']:finding('quantity_aggregate_limit','Workbook',message='Aggregate '+field+' exceeds the supported exact numeric range; no rounded total was accepted.')
    if not result['numeric_cells_checked']:finding('no_quantity_checks_performed','Workbook',message='No mapped numeric quantity cells were checked. This workbook is unverified, never passed.')
    finding('source_not_supplied','Workbook',message='No independent source snapshot was supplied. Quantity checks do not establish source accuracy or completeness.')
    result.update(finding_counts=dict(counts),finding_count=sum(counts.values()),findings_truncated=sum(counts.values())>MAX_FINDINGS,internal_check_status='unverified' if not result['numeric_cells_checked'] or result['template_mapping_status']!='matched' else 'findings',verification_scope='mapped_template_internal_checks_only')
    return result


def reconcile_result(report,source):
    """Compare a parsed workbook with explicit, hash-bound, separately supplied data.

    No fetching, correction application, authenticated-source claim, or release.
    Natural-key collisions use exact multisets only; never guess row identities.
    """
    import copy
    from hosting.report_workflow import WorkflowError,parse,stamp
    source=parse(source)
    if source.get('schema')!='verifier-spreadsheet-source/1':raise WorkflowError('Unsupported spreadsheet source evidence schema.')
    claimed=source.get('source_sha256');body={k:v for k,v in source.items() if k!='source_sha256'}
    if not isinstance(claimed,str) or claimed!=fingerprint(body):raise WorkflowError('Source evidence hash mismatch.')
    if report.get('template_id') not in (TEMPLATE_ID,SECTIONED_TEMPLATE_ID,ALL_FIELDS_TEMPLATE_ID,ACTIVE_TABS_TEMPLATE_ID) or report.get('template_mapping_status')!='matched':raise WorkflowError('Reconciliation requires the exact supported branded template mapping.')
    if source.get('provenance_kind') not in ('retained_supplied_snapshot','independently_retrieved'):raise WorkflowError('Explicit source provenance classification required.')
    scope=source.get('scope');records=source.get('records')
    if not isinstance(scope,dict) or set(scope)!={'site_id','list_id','job_id','project_number','report_date'}:raise WorkflowError('Source requires exact site/list/job/project/date scope.')
    if not isinstance(records,list) or len(records)>10000:raise WorkflowError('Source records must be a bounded array.')
    if type(source.get('complete')) is not bool or type(source.get('shift_coverage_complete')) is not bool or source.get('consistency') not in ('confirmed','unknown') or source.get('timezone_status') not in ('confirmed','unknown'):raise WorkflowError('Source completeness and interpretation evidence must be explicit.')
    if stamp(source.get('captured_at'))>datetime.now(timezone.utc)+timedelta(minutes=5):raise WorkflowError('Source capture time is in the future.')
    result=copy.deepcopy(report);counts=Counter(result['finding_counts']);counts.pop('source_not_supplied',None)
    result['findings']=[f for f in result['findings'] if f['code']!='source_not_supplied']
    comparisons=[];difference_count=0;numeric_compared=0;matched_records=0
    def finding(code,message,record=None,field=None,reported=None,expected=None):
        nonlocal difference_count
        counts[code]+=1
        if code in ('source_value_difference','source_record_missing_from_workbook','workbook_record_not_in_source','source_duplicate_group_difference','source_scope_mismatch'):difference_count+=1
        item=dict(code=code,severity='blocking',message=message,sheet=(record or {}).get('sheet'),row=(record or {}).get('row'),column=QUANTITIES.get(field,-1)+1 if field in QUANTITIES else None)
        if len(result['findings'])<MAX_FINDINGS:result['findings'].append(item)
        if reported is not None or expected is not None:
            if len(comparisons)<MAX_FINDINGS:comparisons.append(dict(code=code,sheet=item['sheet'],row=item['row'],field=field,before=reported,source_value=expected,action='review_only',source_write=False))
    if any(not isinstance(scope[k],str) or not scope[k].strip() for k in ('site_id','list_id','job_id')):finding('source_identity_unverified','Stable source site, list or job identity is unresolved.')
    expected_hash=source.get('expected_workbook_sha256')
    binding=source.get('workbook_scope_binding')
    alias_matches=False
    if binding is not None:
        if not isinstance(binding,dict):raise WorkflowError('Workbook scope binding must be an object.')
        artifact=binding.get('independent_binding_artifact',{})
        evidence=bool(binding.get('evidence_reference')) or (isinstance(artifact,dict) and bool(artifact.get('library_file_id')) and isinstance(artifact.get('sha256'),str) and len(artifact['sha256'])==64)
        if not evidence or not {'source_project_number','report_date','workbook_project_number','workbook_sha256'}.issubset(binding):raise WorkflowError('Workbook scope alias requires explicit source project, date, exact workbook hash and evidence provenance.')
        alias_matches=(binding.get('workbook_sha256')==report['file_sha256'] and binding.get('workbook_project_number')==report['report_identity']['project_number'] and binding['source_project_number']==scope['project_number'] and binding['report_date']==scope['report_date'])
    same_scope=(scope['project_number']==report['report_identity']['project_number'] or alias_matches) and scope['report_date']==report['report_identity']['report_date']
    if not same_scope:finding('source_scope_mismatch','Workbook project/date and supplied source scope disagree. Rows were not reconciled across jobs.')
    if expected_hash is not None and expected_hash!=report['file_sha256']:finding('source_workbook_binding_changed','This workbook hash differs from the version recorded with the source evidence. Prior approval cannot carry forward.')
    if source['provenance_kind']=='retained_supplied_snapshot':finding('historical_supplied_source','Source rows were recovered from a separate evidence artifact, not freshly authenticated against the source service.')
    blockers=source.get('evidence_blockers',[])
    if not isinstance(blockers,list) or len(blockers)>100 or any(not isinstance(b,str) or len(b)>300 for b in blockers):raise WorkflowError('Invalid source evidence blocker list.')
    for blocker in blockers:finding('source_evidence_blocker','Source evidence requires review: '+blocker)
    if not source['complete']:finding('source_coverage_unverified','Observed source rows do not establish complete traversal.')
    if source['consistency']!='confirmed':finding('source_consistency_unverified','A transactionally consistent source snapshot was not established.')
    if not source['shift_coverage_complete']:finding('source_shift_coverage_unverified','All expected shifts and their completion have not been established.')
    if source['timezone_status']!='confirmed':finding('source_timezone_unverified','Business-date timezone interpretation is unconfirmed.')
    seen=set();normalized=[]
    for row in records:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in seen:raise WorkflowError('Source record IDs must be explicit and unique.')
        seen.add(row['id']);values=row.get('values')
        if not isinstance(values,dict) or set(values)!=set(QUANTITIES):raise WorkflowError('Source records need explicit inspected, certified and rejected fields.')
        if row.get('date')!=scope['report_date'] or not isinstance(row.get('shift_id'),str) or not row['shift_id']:raise WorkflowError('Source row date/shift is outside the selected scope.')
        presence=row.get('field_presence',{})
        if not isinstance(presence,dict) or set(presence)-set(QUANTITIES):raise WorkflowError('Invalid source field-presence map.')
        for field,value in values.items():
            if value is not None and quantity(value) is None:raise WorkflowError('Source quantity is invalid; no casting or filling is permitted.')
            if field in presence and presence[field] not in (('absent','null') if value is None else ('present',)):raise WorkflowError('Source field presence contradicts its value.')
            if value is None:finding('source_quantity_missing','A source quantity is absent or unknown; it cannot verify a complete total.',field=field)
        if not isinstance(row.get('identifier'),str) or not row['identifier'].strip():finding('source_row_mapping_unverified','Source row lacks the identifier needed for workbook matching.')
        normalized.append(dict(row,values={field:quantity(value) if value is not None else None for field,value in values.items()}))
    def key(row):
        identifier=row.get('identifier')
        return (row.get('date'),row.get('shift_id'),identifier if isinstance(identifier,str) and identifier.strip() else None)
    def signature(row):return canonical({'part':row.get('part'),'values':row['values']})
    if same_scope:
        left=defaultdict(list);right=defaultdict(list)
        for row in report['records']:left[key(row)].append(row)
        for row in normalized:right[key(row)].append(row)
        for k in sorted(set(left)|set(right),key=repr):
            a,b=left[k],right[k]
            if not k[2]:finding('row_mapping_ambiguous','Blank row identifiers cannot establish exact record correspondence.');continue
            if len(a)==len(b)==1:
                matched_records+=1
                for field in ('part',*QUANTITIES):
                    av=a[0]['values'][field] if field in QUANTITIES else a[0].get(field);bv=b[0]['values'][field] if field in QUANTITIES else b[0].get(field)
                    if field in QUANTITIES and av is not None and bv is not None:numeric_compared+=1
                    if canonical(av)!=canonical(bv):finding('source_value_difference','Workbook value differs from separately supplied source evidence.',a[0],field,av,bv)
            elif not a:
                for row in b:finding('source_record_missing_from_workbook','Source record '+row['id']+' is absent from the workbook.',field='record',reported=None,expected={'id':row['id'],'identifier':row.get('identifier'),'values':row['values']})
            elif not b:
                for row in a:finding('workbook_record_not_in_source','Workbook record has no matching source identifier.',row,field='record',reported={'identifier':row.get('identifier'),'values':row['values']},expected=None)
            else:
                finding('row_mapping_ambiguous','Repeated identifiers prevent exact source-ID-to-workbook-row mapping. Only whole-group multisets are compared.')
                numeric_compared+=sum(min(sum(r['values'][f] is not None for r in a),sum(r['values'][f] is not None for r in b)) for f in QUANTITIES)
                if Counter(signature(r) for r in a)!=Counter(signature(r) for r in b):finding('source_duplicate_group_difference','A repeated-identifier group differs in record count, part numbers or quantity values; no cell correction was guessed.',field='record_group',reported={'record_count':len(a),'totals':totals(a)},expected={'record_count':len(b),'totals':totals(b)})
                else:
                    matched_records+=len(a)
    if not numeric_compared:finding('no_source_quantity_checks','No known numeric quantities were independently compared. No pass is possible.')
    result.update(source_sha256=claimed,source_scope=scope,source_provenance_kind=source['provenance_kind'],source_authenticity='not_authenticated',source_reference=source.get('source_reference'),source_captured_at=source['captured_at'],source_record_count=len(records),source_totals=totals(normalized),matched_observed_record_count=matched_records,numeric_values_compared=numeric_compared,source_differences=comparisons,source_difference_count=difference_count,source_reconciliation_status='mismatch' if difference_count else ('observed_values_compared_with_limits' if numeric_compared else 'unverified'),verification_scope='mapped_template_vs_separate_supplied_source',finding_counts=dict(counts),finding_count=sum(counts.values()),findings_truncated=sum(counts.values())>MAX_FINDINGS,internal_check_status='findings',approval_effective=False,release_status='held',source_write_enabled=False,send_enabled=False)
    result['review_binding_sha256']=fingerprint(dict(workbook_sha256=report['file_sha256'],source_sha256=claimed,template_id=report['template_id'],checker_version=report.get('checker_version'),formula_sha256=report['formula_sha256']))
    return result
