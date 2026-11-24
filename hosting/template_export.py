"""A stored report, written into the selected PPG Daily End of Shift template for download.

The stored report is never changed: this reads its inspection records and writes a new workbook with the pinned native
template writer (hosting.source_first_workbook), which adds rows and part lines as needed and keeps the template's layout,
print setup and branding. The wording is the client-ready template wording: nothing marks the file as a draft.

Quantities follow the template's own rule, chosen by the operations owner on 2026-11-24: where no reject quantity was
recorded it counts as zero, and Quantity Certified is Inspected minus Rejected when it was not recorded. The Summary
sheet says so, and how many records that applied to. Pratt & Whitney reports are never converted.
"""
from datetime import date
from decimal import Decimal

from hosting import source_first as sf

SHEETS = {1: '1st Shift', 2: '2nd Shift', 3: '3rd Shift'}
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
INTERNAL = ('HELD', 'DRAFT', 'snapshot', 'pilot', 'Internal hold', 'Unavailable', 'not mapped', 'Completion is unknown', 'Source-first')


class TemplateUnavailable(ValueError):
    """This report cannot be written into the template; the stored file is still available as it is."""


def _number(value):
    if value is None or value == '':
        return None
    try:
        n = Decimal(str(value))
    except Exception:
        return None
    return int(n) if n == n.to_integral_value() else float(n)


def _text(value):
    text = ' '.join(str(value).split()) if value is not None else ''
    return text or None


def _shift_sheet(record):
    if record.get('sheet') in SHEETS.values():
        return record['sheet']
    digits = ''.join(ch for ch in str((record.get('labels') or {}).get('shift') or '') if ch.isdigit())
    return SHEETS.get(int(digits)) if digits else None


def rows_from(records, day):
    """-> (writer rows, the records they stand on, notes). One row per inspection record of the stored report, in order.

    The writer keeps a hidden record of where every value came from and checks it after saving, so each row is given
    in the same typed form a captured source record has: here the "source" is the stored report's own row.
    """
    rows, stored, notes = [], [], dict(reject_blank=0, certified_derived=0, inspected_blank=0)
    fields = {f: f for f in sf.FIELDS}
    for r in records:
        if r.get('table_kind') != 'detail' or r.get('is_total'):
            continue
        sheet = _shift_sheet(r)
        if not sheet:
            raise TemplateUnavailable(f'{r.get("sheet")} row {r.get("row")} has no recorded shift, so it has no place in the template.')
        labels, values = r.get('labels') or {}, r.get('values') or {}
        inspected, rejected, certified = (_number(values.get(k)) for k in ('inspected', 'rejected', 'certified'))
        if inspected is None:
            notes['inspected_blank'] += 1
            inspected = 0
        if rejected is None:
            notes['reject_blank'] += 1
            rejected = 0                                  # the template's own formula: a blank reject quantity counts as zero
        if certified is None:
            notes['certified_derived'] += 1
            certified = inspected - rejected              # the template's own formula: certified = inspected - rejected
        ident = f'{r.get("sheet")}!{r.get("row")}'
        if any(type(q) is not int or q < 0 for q in (inspected, rejected, certified)):
            raise TemplateUnavailable(f'{ident} has a quantity that is not a whole number of parts, which the template cannot show.')
        raw = dict(date=str(day), shift=_text(labels.get('shift')) or sheet.split()[0], location=_text(labels.get('area')), part=_text(labels.get('part')),
                   identifier=_text(labels.get('identifier')), inspected_for=_text(r.get('row_label') or labels.get('inspected_for')), inspected=inspected,
                   condition=_text(labels.get('condition')), reject_reason=_text(labels.get('defect')), rejected=rejected, certified=certified,
                   comments=_text(labels.get('comment')))
        record = dict(id=ident, version='stored', day=str(day), raw=raw)
        typed = {f: sf.typed(raw, f) for f in sf.FIELDS}
        resolved = {f: None if sf.missing(v) else v.get('value') for f, v in typed.items()}
        resolved.update(date=str(day), shift=sheet)
        stored.append(record)
        rows.append(dict(id=ident, record_hash=sf.digest(record), source_version='stored', photos=[], mapped_fields=dict(fields),
                         original_values={f: dict(v) for f, v in typed.items()}, values=typed, resolved=resolved))
    if not rows:
        raise TemplateUnavailable('No inspection records were found in this report, so there is nothing to put in the template.')
    return rows, stored, notes


def _plural(n, word):
    return f'{n:,} {word}{"" if n == 1 else "s"}'


def wording(job, day, head, rows, notes):
    """The client-ready words for every place the writer would otherwise put its internal-draft wording."""
    d = date.fromisoformat(day)
    title = '   |   '.join(x for x in (head.get('paying_client'), head.get('area'), f'Job {job}', f'{MONTHS[d.month - 1]} {d.day}, {d.year}') if x)
    parts = len({r['resolved']['part'] for r in rows})
    with_rejects = sum(1 for r in rows if r['resolved']['rejected'])
    totals = {k: sum(r['resolved'][k] for r in rows) for k in ('inspected', 'rejected', 'certified')}
    over = sum(1 for r in rows if r['resolved']['rejected'] > r['resolved']['inspected'])
    unequal = sum(1 for r in rows if r['resolved']['inspected'] != r['resolved']['rejected'] + r['resolved']['certified'])
    basis = 'Figures come from the PPG inspection records for the report date.'
    if notes['reject_blank']:
        basis += f' No reject quantity was recorded on {_plural(notes["reject_blank"], "record")}; as on the template, a blank reject quantity counts as zero.'
    if notes['certified_derived']:
        basis += f' Quantity Certified is Quantity Inspected minus Reject Quantity on {_plural(notes["certified_derived"], "record")} where it was not recorded.'
    if notes['inspected_blank']:
        basis += f' No inspected quantity was recorded on {_plural(notes["inspected_blank"], "record")}; it counts as zero.'
    summary = {
        'B5': 'End of Shift Report', 'B6': title, 'B8': 'Boxes', 'F20': 'Boxes',
        'B10': f'{_plural(len(rows), "inspection record")}   |   {_plural(parts, "part number")}   |   {_plural(with_rejects, "record")} with rejects',
        'B25': 'Daily totals include every recorded inspection for the report date.',
        'B33': f'{_plural(len(rows), "inspection record")}: {totals["inspected"]:,} parts inspected, {totals["certified"]:,} certified, {totals["rejected"]:,} rejected.',
        'B41': 'DEFECT SUMMARY', 'B42': 'Reject quantities and reasons are listed by record on the shift sheets and on Rejects & Photos.', 'B45': None,
        'M43': 'Photos on file', 'O43': 'Records without a photo', 'B47': 'REJECTED PARTS BY IDENTIFIER', 'B62': 'TOTAL',
        'E66': 'Not recorded', 'J66': None, 'E67': 'OK' if not over else f'{_plural(over, "record")} above inspected', 'J67': None, 'J68': None,
        'E69': 'Not recorded', 'J69': None, 'E70': 'Not recorded', 'J70': None,
        'E71': 'OK' if not unequal else f'{_plural(unequal, "record")} differ', 'J71': None,
        'B74': basis, 'B82': 'PPG Quality  ·  Daily End of Shift Data and Billing Report',
    }
    photos = {'A4': title, 'A6': 'RECORDED REJECTS', 'I7': 'Record'}

    def say(sheet, address, text):
        if sheet == 'Summary':
            if address in summary:
                return summary[address]
            if text == 'No recorded rows':
                return 'Not used'                         # the template's own word for a shift with no records
        elif sheet in SHEETS.values():
            if address == 'A4':
                return title + '   |   ' + sheet
            if address == 'C11' or text in ('Unavailable', 'No captured photo', 'Not selected', 'Unavailable / not captured') or 'pilot' in text:
                return None
            if text == 'Records inspected':
                return 'Boxes Inspected'
        elif sheet == 'Rejects & Photos' and address in photos:
            return photos[address]
        if text == 'Unavailable' or 'pilot' in text:
            return None
        return text
    return say


def in_template(filename, raw, job, day, header=None):
    """-> (xlsx bytes in the PPG Daily template, notes). Raises TemplateUnavailable when the report cannot be laid out."""
    from hosting import source_first_workbook as writer
    from hosting.spreadsheets import report_records
    if not str(filename).lower().endswith('.xlsx'):
        raise TemplateUnavailable('Only Excel (.xlsx) reports can be put in the template.')
    if not job or not day:
        raise TemplateUnavailable('The report has no job number or report date, which the template needs.')
    try:
        date.fromisoformat(str(day))
    except ValueError:
        raise TemplateUnavailable('The report date is not a calendar date.') from None
    rows, stored, notes = rows_from(report_records(filename, raw), str(day))
    head = {k: _text((header or {}).get(k)) or '' for k in writer.HEADER_FIELDS}
    head.update(photo_choices={}, reviewed=True, evidence='', origin='stored_report')
    capture = dict(scope=dict(job=str(job), business_date=str(day)), snapshot_hash=sf.digest([filename, len(raw)]), photos=[], records=stored)
    try:
        out, _ = writer.build(rows, head, capture, {}, {}, wording=wording(str(job), str(day), head, rows, notes))
    except sf.ReviewError as exc:
        raise TemplateUnavailable(str(exc)) from None
    return out, notes


def for_download(inbox, found, filename, raw):
    """-> (filename, bytes, in_template). What an ordinary download of a stored report gives: the report in the PPG
    Daily template. When it cannot be laid out, the stored file is returned unchanged with a name that says so."""
    from hosting import registry
    from hosting.review_summary import summarize
    review = summarize(found, with_issues=False)
    assignment = inbox.assignment_for(found, review)
    settled = assignment.get('status') in registry.RESOLVED
    who = ' '.join(str(assignment.get(k) or '') for k in ('client_key', 'client', 'source')).lower()
    if 'pratt' in who or 'pratt' in str(filename).lower():
        return filename, raw, False                      # Pratt & Whitney reporting is done elsewhere: never converted
    saved = {}
    if settled and assignment.get('site_id') and assignment.get('list_id'):
        saved = inbox.setting(f'report_header|{assignment["site_id"]}|{assignment["list_id"]}', {}) or {}
    header = dict(paying_client=(assignment.get('client') if settled else '') or saved.get('customer') or '', worksite=saved.get('worksite') or '',
                  description=saved.get('program') or '', prepared_by=saved.get('prepared_by') or 'PPG Quality')
    try:
        out, _ = in_template(filename, raw, review.get('job') or (assignment.get('job') if settled else None), review.get('day'), header)
    except Exception:
        # Whatever the reason, a report that cannot be laid out still downloads: as stored, with a name that says so.
        stem, dot, ext = str(filename).rpartition('.')
        return (f'{stem} (as stored - not in PPG template).{ext}' if dot else f'{filename} (as stored - not in PPG template)'), raw, False
    return filename, out, True
