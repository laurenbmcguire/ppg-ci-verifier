"""Everything the verifier checks, in plain words, for the app's "What it checks" page."""

GROUPS = [
    ('The file itself', 'Every upload, the moment it arrives.', [
        'Finds the inspection table on every tab, even when the layout differs from the usual template.',
        'Rows with no part number, or no VIN / identifier / scan number.',
        'The same VIN or identifier listed twice, and rows that are exact copies of another row.',
        'VINs and part numbers that look like typos of the others (a letter O for a zero, one character off).',
        'Quantities typed as text (they will not add into the totals), letters or symbols in number cells, fractions or negative counts.',
        'More parts rejected than inspected, and Inspected not equal to OK plus rejected.',
        'A condition (OK / Suspect / NOK) that does not match the reject quantity, and unexpected condition words.',
        'Rejects with no defect reason.',
        'Dates that are invalid, in the future, or different from the report date.',
        'Shift values that are not a real shift, or that differ from the shift on that tab.',
        'Totals rows, shift totals and the summary tab not adding up to the rows above them.',
        'Reject log rows that are not in the detail table, and reject log totals that disagree.',
        'Extra spaces that stop values matching, and Excel error values (#REF!, #VALUE!).',
        'Hidden rows or columns that still hold data.',
        'Links to other workbooks or websites, and formulas that could run code when opened.',
    ]),
    ('Against SharePoint', 'The job\'s "Data #" list (or daily export) for the same day and shift.', [
        'Every VIN in the report is on SharePoint, and every SharePoint scan for that day and shift is in the report.',
        'Quantities, part numbers, dates and shift match SharePoint row by row.',
        'VINs in the report that look like a typo of a SharePoint scan.',
        'VINs scanned twice on SharePoint.',
        'Report totals match SharePoint totals.',
        'Each scan is put on a shift by its scan time and the shift times you set, not by the shift the inspector picked. '
        'A 3rd-shift scan after midnight counts toward the day the shift started.',
        'Rows that are on the wrong shift by their scan time (and can be moved to the right shift tab in one click).',
    ]),
    ('Against the usual day', 'The job\'s earlier days, mostly from the SharePoint list.', [
        'Scans, parts inspected, and reject rate far from the job\'s usual day (missing shifts, a duplicated upload, a short or extra shift).',
    ]),
    ('AI review (Claude)', 'Reads the whole report like a careful person would.', [
        'Anything the fixed checks cannot catch: odd patterns, values that do not fit the job, comments that contradict the numbers.',
        'Everything on your "Also look for" list.',
        'Then double-checks every flagged item against the actual cells and dismisses false alarms (you can still see and restore them).',
        'Drafts the cell fixes it is sure of, for you to accept or reject.',
    ]),
    ('Before it goes to the client', 'Approve and send.', [
        'Must-fix issues still open, or a newer corrected copy, need "Approve anyway".',
        'A report can only be emailed after a named person approves that exact version.',
        'A second send of the same report needs "Send again".',
        'Recipients come from the last report email sent for the same job; nothing internal goes in the email.',
    ]),
]


def catalog():
    return [dict(title=t, when=w, items=items) for t, w, items in GROUPS]
