"""Compatibility guard for deterministic extraction against saved authoritative detail counts."""


def saved_detail_count(item):
    # Generic first-row counts include total/blank rows. Pratt has separate completed,
    # failure and child-record grains. Neither is a comparable adaptive detail count.
    known_scope = item.get('mapping_mode') == 'adaptive' or (
        item.get('template_mapping_status') == 'matched' and
        str(item.get('template_id') or '').startswith('ppg-daily-'))
    historical_ai = item.get('layout_source') == 'ai' or bool(item.get('ai_layout'))
    if not known_scope and not historical_ai:
        return None
    count = item.get('record_count')
    if type(count) is not int or count < 0:
        raise ValueError('Report layout needs review: the saved authoritative detail-row count is unknown. Review recognized column headers or a supported template and preview a recheck before comparing the source. The original and saved source checks are unchanged.')
    return count


def require_complete_detail(item, records):
    expected = saved_detail_count(item)
    if not records or (expected is not None and len(records) != expected):
        raise ValueError('Report layout needs review: locally recognized detail rows do not match the saved authoritative report scope, or no detail rows were recognized. Review recognized column headers or a supported template and preview a recheck before comparing the source. No comparison or source upload was saved; the original and saved source checks are unchanged.')
