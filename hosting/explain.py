"""Plain-English explanation and certainty for every finding code.

Each code maps to three things the reviewer sees next to the issue:
  why   one sentence: why it matters to the client or the bill
  fix   one sentence: what the reviewer should do
  sure  True only when the check is exact and the problem is certain (a sum that is wrong, an Excel error,
        a negative count). False when the check is a judgement or a pattern guess (a possible typo, an
        outlier, an unusual length), or when the cause is unclear. Owner's rule: anything not sure
        has to be looked at by a person.

Written for a quality coordinator: plain words, no jargon. The codes are listed in groups that share one
explanation. Unknown codes (including the free-text categories the AI review makes up) get a safe generic
explanation and are never "sure".
"""

CATALOG = {}


def _add(codes, why, fix, sure):
    for code in codes.split():
        assert code not in CATALOG, f'{code} is listed twice'
        CATALOG[code] = dict(why=why, fix=fix, sure=sure)


# ---------------------------------------------------------------- numbers on a row that cannot be right
_add('rejected_exceeds_inspected certified_exceeds_inspected',
     'More parts are shown as rejected or OK than were inspected, so the counts are wrong and the totals and the bill will be off.',
     'Check this row against the scan and correct whichever of Inspected, OK or Rejected was keyed wrong.', True)
_add('inspected_count_mismatch',
     'Inspected should equal OK plus rejected; when it does not, one of the counts is wrong and the totals will not add up.',
     'Recount this row from the scan and fix the number that is off.', True)
_add('negative_count_or_quantity',
     'A count can never be below zero, so this number was keyed wrong and pulls the totals down.',
     'Replace it with the real count (often the same number without the minus sign).', True)
_add('fractional_count',
     'Parts are counted in whole pieces, so a decimal usually means a typing slip or a number in the wrong cell.',
     'Change it to the whole number that was counted.', True)
_add('invalid_numeric_value invalid_value quantity_invalid',
     'A quantity cell holds something that is not a number, so it is left out of the totals.',
     'Replace it with the number that was counted.', True)
_add('number_stored_as_text',
     'The number is stored as text, so Excel skips it when it adds up the totals.',
     'Retype it as a plain number (remove the apostrophe or set the cell format to Number).', True)
_add('missing_cell quantity_missing',
     'A cell that should hold a quantity is blank, so the total is too low or cannot be worked out.',
     'Fill it in from the scan data, or enter 0 if nothing was counted.', True)
_add('missing_part_number part_number_missing',
     'Without a part number the row cannot be billed or matched to the customer records.',
     'Add the part number from the scan or the label.', True)
_add('missing_identifier record_identifier_missing',
     'Without the VIN or scan number the row cannot be matched to SharePoint or checked for duplicates.',
     'Add the VIN or scan number from the scan.', True)
_add('missing_defect_reason',
     'Rejected parts need a reason so the customer can see what went wrong.',
     'Add the defect reason for the rejected parts on this row.', True)
_add('zero_inspected',
     'A row with a part or VIN but 0 inspected may have lost its count, which would understate the day.',
     'Check the scan and enter the real count, or remove the row if nothing was inspected.', False)
_add('condition_conflicts_with_quantity',
     'The condition says OK but parts were rejected (or the other way round), so the row contradicts itself.',
     'Check the scan, then fix either the condition or the reject quantity.', False)
_add('condition_count_mismatch',
     'The number of positions marked suspect does not equal the suspect quantity, so the reject count may be wrong.',
     'Count the marked positions again and make the quantity match.', False)
_add('unexpected_condition_value',
     'A condition value is not one the report normally uses, which is usually a typing mistake.',
     'Correct it to OK or SUSPECT/NOK.', False)
_add('comment_count_mismatch',
     'The counts written in the comment do not add up to the reject quantity, so one of them may be wrong.',
     'Read the comment against the reject quantity and fix whichever is wrong.', False)
_add('quantity_outlier',
     'This quantity is far from the rest of the report, which is often a dropped or extra zero.',
     'Check the number against the scan and correct it if it was mistyped.', False)
_add('value_in_wrong_column',
     'A value looks like it was typed into the wrong column, so it could be counted as a quantity or missed as a part number.',
     'Move the value to the right column and check the neighbouring cells.', False)

# ---------------------------------------------------------------- totals
_add('total_mismatch totals_block_mismatch summary_mismatch shift_total_mismatch detail_summary_mismatch '
     'failure_row_count_mismatch failure_title_count_mismatch audit_result_balance_mismatch shift_record_count '
     'reject_log_total_mismatch part_table_coverage_mismatch',
     'A total shown does not equal the rows it should add up, so the client would see or be billed the wrong number.',
     'Recalculate the total in Excel, or find the row that was changed or left out.', True)
_add('rows_after_total_not_reconciled',
     'There are rows below the Total row, so they are not in the total and the total is too low.',
     'Move those rows above the Total row, or add them to the total.', True)
_add('labeled_total_mismatch',
     'A figure next to a "Total ..." label does not match the rows above it, so the total may be wrong.',
     'Recalculate that total, or find the row that was changed or left out.', False)
_add('labeled_total_unit_differs',
     'A total is counted in different units from the rows (for example weights instead of vehicles), so the two numbers will never match.',
     'Confirm which number is billed and make the label say which one it is.', False)
_add('total_row_blank',
     'A Total label has no number beside or under it, so the report shows no total for rows that clearly have counts.',
     'Fill in or recalculate the total, or delete the label if no total is meant.', False)
_add('defect_reject_mismatch',
     'A defect reason is filled in but the reject quantity is 0, so either a reject was not counted or the reason does not belong.',
     'Check the scan, then fix the reject quantity or clear the reason.', False)
_add('reject_log_mismatch',
     'The reject log shows a different reject count for a scan than the shift sheet does, so one of them is wrong.',
     'Check that scan on both sheets and make the two numbers agree.', True)
_add('reject_log_row_not_in_detail',
     'A reject is listed on the log but on no shift sheet, so it is either missing from the counts or listed by mistake.',
     'Find that scan on the shift sheets and add it, or take it off the log.', True)
_add('total_unknown shift_total_unknown unknown_total_reference unknown_part_total_claim unknown_reject_sum_claim '
     'unknown_rate_claim rate_not_verified partial_part_total_claim partial_reject_label_missing',
     'A total or rate cannot be confirmed because some of the numbers under it are blank or unreadable.',
     'Fill in the missing numbers, then check the total again.', False)
_add('aggregate_limit aggregate_precision quantity_aggregate_limit',
     'A total is too large or too exact to be added safely, so it was left unchecked.',
     'Check this total by hand.', False)

# ---------------------------------------------------------------- duplicates
_add('duplicate_row',
     'This row is an exact copy of another row, so its parts are counted and billed twice.',
     'Delete the copy if it is a mistake.', True)
_add('duplicate_identifier',
     'The same VIN or scan number is listed more than once, which may mean a part was counted or billed twice.',
     'Check the scans and remove the extra line unless it was a real second inspection.', False)
_add('duplicate_identifier_variant',
     'The same VIN is written two ways (spaces, dashes or capitals), so it probably counts twice and the duplicate check misses it.',
     'Check whether it is the same scan and remove one line if so.', False)
_add('duplicate_count_block',
     'Several rows in a row show exactly the same counts, which can happen when one row is copied down instead of counted.',
     'Check these rows against the scans and correct any that were copied.', False)
_add('duplicate_header duplicate_part_summary',
     'A heading or part appears twice, so the report can add or look up the wrong column or row.',
     'Rename or merge the repeated heading or part.', True)
_add('fill_series_identifiers',
     'Several VINs count up by one, which is what Excel does when a cell is dragged down, so they may not be real scans.',
     'Check these VINs against the scanner list.', False)

# ---------------------------------------------------------------- typos and formats
_add('possible_identifier_typo possible_part_number_typo possible_defect_typo possible_condition_typo possible_auditor_typo',
     'This value looks almost the same as a more common one, so it may be a typing mistake that splits or hides a count.',
     'Compare it with the similar value and correct it if it was mistyped.', False)
_add('identifier_format_differs part_format_differs',
     'This value is shaped differently from the others in the report, which often means it was mistyped or pasted wrongly.',
     'Check it against the scan or label.', False)
_add('identifier_unusual_length',
     'This identifier is shorter or longer than the rest, so a character may be missing or extra.',
     'Check it against the scan and fix the missing or extra character.', False)
_add('vin_has_i_o_q',
     'Real VINs never contain I, O or Q, so this VIN was misread or mistyped and will not match other records.',
     'Check the VIN against the car or scan; the letter is usually a 1 or a 0.', True)
_add('vin_check_digit_wrong',
     'The VIN fails the standard check digit, so a character was probably typed or scanned wrong.',
     'Check the VIN against the car or scan and correct the wrong character.', False)
_add('leading_zeros_lost',
     'The same part number appears with and without its leading zero, which splits its counts and can fail to match the customer list.',
     'Enter the part number with its full leading zeros, stored as text.', False)
_add('scientific_notation_code',
     'Excel turned a long number into scientific notation (like 8.01E+09), so digits were lost and it cannot be matched.',
     'Re-enter it from the scan as text.', False)
_add('inconsistent_spelling',
     'The same thing is written different ways, so counts by part, reason or person get split.',
     'Use one spelling everywhere.', False)
_add('typed_identifier_invalid',
     'A part identifier is not stored as exact text, so it may be altered or fail to match.',
     'Re-enter the identifier as text.', False)
_add('placeholder_value',
     'A placeholder such as TBD, xxx or 9999 is still in the report instead of real data.',
     'Replace it with the real value, or clear the cell.', False)
_add('extra_spaces header_whitespace',
     'Extra spaces stop values from matching each other, so lookups and duplicate checks can miss them.',
     'Delete the extra spaces.', True)
_add('hidden_characters',
     'Invisible or garbled characters are in this cell, which can stop it matching other records.',
     'Retype the value instead of pasting it.', True)

# ---------------------------------------------------------------- blank, text and wrong dates / shifts
_add('missing_value',
     'A cell is blank in a column that is filled in on almost every other row, so something was probably skipped.',
     'Fill it in from the scan or paperwork.', False)
_add('blank_header',
     'A column has no heading, so it cannot be read or checked.',
     'Add a heading to the column.', True)
_add('inconsistent_row_width',
     'This row has a different number of cells from the heading row, so values may have slipped into the wrong columns.',
     'Check the row and put each value under its heading.', True)
_add('date_stored_as_text',
     'The date is typed as text while the others are real dates, so it will not sort or filter with them.',
     'Retype it as a real date.', True)
_add('invalid_date',
     'The date cannot be read, so the row cannot be tied to a day.',
     'Enter a real date such as 2026-10-07.', False)
_add('date_differs_from_report',
     'The date on this row is not the report date, so the row may belong to a different day and be billed on the wrong one.',
     'Check the scan date and move the row or fix the date.', False)
_add('future_date',
     'The date is in the future, so it was mistyped or the row is not real.',
     'Correct the date.', True)
_add('invalid_shift',
     'The shift value is not a shift the system recognises, so the row cannot be put on a shift.',
     'Enter 1, 2 or 3 (1st, 2nd or 3rd shift).', False)
_add('shift_differs_from_sheet',
     'The shift on the row is different from the shift tab it sits on, so it may be counted on the wrong shift.',
     'Move the row to the right tab or fix the shift value.', True)
_add('wrong_shift',
     'By the time of the scan this row belongs to a different shift than the one it is listed under, so shift totals are off.',
     'Move the row to the right shift tab.', True)

# ---------------------------------------------------------------- hidden or empty content
_add('hidden_sheet_with_data hidden_rows_with_data hidden_columns_with_data',
     'Hidden data is still counted in totals but nobody sees it, so it can hide duplicates or mistakes.',
     'Unhide it and check that it belongs in the report.', False)
_add('excel_error_value',
     'The cell shows an Excel error such as #REF!, so any total that uses it is wrong.',
     'Fix the formula or enter the real value.', True)
_add('empty_sheet no_data_rows',
     'A tab has no inspection rows, so nothing on it could be checked.',
     'Check that the tab is meant to be empty.', True)
_add('no_quantity_mapping no_quantity_columns_detected',
     'No quantity columns were found on this tab, so its numbers could not be checked.',
     'Check that the tab has headings such as Inspected and Rejected.', True)
_add('no_rows_found',
     'No inspection rows were found in the file, so nothing could be checked.',
     'Check that this is the end-of-shift report and not a blank or wrong file.', True)

# ---------------------------------------------------------------- formulas and links (information, not mistakes)
_add('formula_results_used quantity_formulas_not_evaluated formula_not_evaluated',
     'Some numbers are formulas, so they were checked using the result Excel last saved, or could not be checked at all.',
     'Open the file in Excel and recalculate if it was edited somewhere else.', True)
_add('known_template_layout_differs',
     'The report looks like a known PPG template but is laid out differently, so columns were found automatically.',
     'Check that the detected columns are the right ones.', True)
_add('external_reference_formula web_function_formula dde_or_macro_call_formula external_hyperlink hyperlink_formula '
     'external_workbook_link external_data_connection dde_link csv_formula_injection_risk',
     'The file links to another file or website, or has a formula that could run something, so it may show numbers that are not in this file.',
     'Check where the link goes and remove it if it is not needed.', True)

# ---------------------------------------------------------------- against SharePoint
_add('not_on_sharepoint',
     'This scan is in the report but not on SharePoint for this day, so it may be a typo or not a real scan.',
     'Look for the scan on SharePoint, then fix the VIN or remove the row.', True)
_add('missing_from_report',
     'A scan is on SharePoint but not in the report, so the report is short and the client would be under-billed.',
     'Add the scan to the report.', True)
_add('differs_from_sharepoint total_differs_from_sharepoint',
     'A number in the report is different from the one on SharePoint, so the client may be shown or billed the wrong amount.',
     'Check which one is right and correct the report.', True)
_add('unmatched_report_row',
     'This row could not be matched to any SharePoint scan, so it may be missing from the source or mistyped.',
     'Look for the part on SharePoint and fix or remove the row.', False)
_add('identifier_typo_vs_sharepoint',
     'This VIN is not on SharePoint but one very close to it is, so it is probably a typing mistake.',
     'Check the VIN against SharePoint and correct it.', False)
_add('duplicate_identifier_vs_sharepoint',
     'The report lists this VIN more times than SharePoint does, so it may be billed twice.',
     'Remove the extra lines.', True)
_add('sharepoint_duplicate_identifier',
     'This VIN was scanned more than once on SharePoint, which may be a rescan or a double scan.',
     'Check whether the second scan was real before billing it twice.', False)

# ---------------------------------------------------------------- against the usual day
_add('unusual_scans_volume unusual_inspected_volume unusual_rejected_volume',
     'The volume is far from this job\'s usual day, which can mean a missing shift, a duplicated upload or a short day.',
     'Check that every shift is included and the file is not a copy of another day.', False)
_add('unusual_reject_rate',
     'The reject rate is far from this job\'s usual rate, so rejects may have been mistyped, left out, or there is a real quality problem.',
     'Check the reject counts and tell the client if the problem is real.', False)

# ---------------------------------------------------------------- proving the report against the source (not a mistake in the file itself)
_add('source_not_supplied source_missing source_incomplete source_scope source_scope_mismatch source_date_scope_mismatch '
     'source_candidate_eligibility_unconfirmed source_capture_time_unverified source_completeness_unverified '
     'source_consistency_unconfirmed source_consistency_unverified source_coverage_unverified source_shift_coverage_unverified '
     'source_timestamp source_timezone_unconfirmed source_timezone_unverified source_identity_unverified '
     'source_list_identity_unverified source_id_not_displayed source_row_mapping_unverified source_field_mapping '
     'source_envelope_blocker source_evidence_blocker source_workbook_binding_changed source_result_unmapped '
     'source_status_or_shift_unmapped source_detail_group_unmapped source_failure_defects_unmapped source_quantity_missing '
     'historical_supplied_source empty_source_evidence project_metadata_not_source_bound photo_evidence_not_verified '
     'no_source_quantity_checks no_quantity_checks_performed summary_source_id_mapping_unavailable '
     'daily_member_no_data daily_member_timestamp daily_member_unconfirmed daily_record_shift daily_shift_coverage_unconfirmed '
     'daily_known_shift_missing shift_completion_unconfirmed shift_activity_unverified unobserved_shift_claim '
     'empty_shift_result_claim client_identity_missing no_data',
     'The report could not be proven against the original records, so its numbers are only as good as the file itself.',
     'Compare the totals with the SharePoint list for this job and day before the report goes out.', False)
_add('source_difference source_value_difference additional_defect_difference part_audit_count_difference '
     'failure_comment_difference completed_record_group_difference source_duplicate_group_difference '
     'source_failure_group_difference reject_register_difference part_summary_coverage_difference extra_record missing_record '
     'source_record_missing_from_workbook workbook_record_not_in_source defect_occurrence_mismatch',
     'The report disagrees with the original source records, so the client may be shown or billed numbers that are wrong.',
     'Open the source record, decide which side is right, and correct the report.', True)

# ---------------------------------------------------------------- layout the checker could not read with certainty
_add('template_header_mapping_mismatch template_identity_mapping_mismatch template_mapping_mismatch template_scope_mismatch '
     'template_sheet_mapping_mismatch section_label_mismatch section_formula_mapping_mismatch summary_formula_mapping_mismatch '
     'summary_shift_label_mismatch part_table_mapping_mismatch reject_register_mapping_mismatch record_scope_mismatch '
     'row_mapping_ambiguous completed_row_mapping_ambiguous failure_row_mapping_ambiguous part_sumif_criterion_ambiguous '
     'unmapped_formula unmapped_production_value unreported_production_fields record_boundary_unverified '
     'reject_register_boundary_unverified record_outside_mapped_table reject_record_outside_mapped_table '
     'completed_record_scope_invalid failure_scope_invalid failure_defects_invalid failure_field_missing '
     'nonfailure_defects_excluded additional_defect_alias_review additional_sections_not_verified comment_type_invalid '
     'known_rejects_section_omitted known_rejects_summary_omitted reject_register_not_present',
     'Part of the report does not follow the usual layout, so the checker could not read it with certainty and some numbers went unchecked.',
     'Look at the cells named here and check them by hand against the usual template.', False)


GENERIC = dict(
    why='This does not look right and could make the numbers in the report wrong.',
    fix='Look at the cell shown, compare it with the scan or source, and correct it or note why it is fine.',
    sure=False)


# Codes where the only problem is how something is written. Shown as "Only the writing is different" so nobody
# goes looking for a bigger problem. Wording stays honest where the app is guessing ("looks like").
WRITTEN = {
    'inconsistent_spelling': 'Only the spelling is different from the other rows. It means the same thing; nothing else is wrong.',
    'duplicate_identifier_variant': 'Only the way this ID is written is different (capitals, spaces or dashes). It is the same ID; nothing else is wrong.',
    'extra_spaces': 'Only extra spaces. The value itself is right; nothing else is wrong.',
    'header_whitespace': 'Only extra spaces in the heading. Nothing else is wrong.',
    'hidden_characters': 'Only invisible characters. The value you see is right; nothing else is wrong.',
    'number_stored_as_text': 'Only the way the number is stored. The number itself is right, but Excel leaves it out of totals until it is fixed.',
    'date_stored_as_text': 'Only the way the date is stored. The date itself is right; nothing else is wrong.',
    'leading_zeros_lost': 'Only the leading zero that Excel dropped. It is the same code; nothing else is wrong.',
    'possible_identifier_typo': 'Looks like the same value written a little differently (for example the letter O typed for a zero). If that is all, nothing else is wrong.',
    'possible_part_number_typo': 'Looks like the same part number written a little differently (for example the letter O typed for a zero). If that is all, nothing else is wrong.',
    'possible_defect_typo': 'Looks like the same defect written a little differently. If that is all, nothing else is wrong.',
    'possible_condition_typo': 'Looks like the same condition written a little differently. If that is all, nothing else is wrong.',
    'possible_auditor_typo': 'Looks like the same name written a little differently. If that is all, nothing else is wrong.',
    'identifier_typo_vs_sharepoint': 'Looks like the same ID as on SharePoint, written a little differently. If that is all, nothing else is wrong.',
    'identifier_format_differs': 'Often the same ID typed a little differently (like the letter O for a zero). Check it; if that is all, nothing else is wrong.',
    'part_format_differs': 'Often the same part number typed a little differently (like the letter O for a zero). Check it; if that is all, nothing else is wrong.',
    'vin_has_i_o_q': 'Most likely the letter I, O or Q typed for a 1 or a 0. If that is all, the rest of the VIN is right.',
}


def lookup(code):
    """The catalog entry for a code, or None when it is not listed."""
    return CATALOG.get(code or '')


def explain(code):
    """{'why', 'fix', 'sure'} for a code; unknown codes get the generic explanation and are never sure."""
    return dict(lookup(code) or GENERIC, only=WRITTEN.get(code or ''))
