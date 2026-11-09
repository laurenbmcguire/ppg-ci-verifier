"""Limits shared by every report reader.

MAX_FINDINGS is a safety bound against hostile files, not a display limit: every finding a check makes is stored
and counted up to it (an ordinary report has a few hundred). If a file ever goes past it the result says so
(findings_truncated) and the report is held until a person looks, because unlisted findings cannot be reviewed.
"""
MAX_FINDINGS = 20000

# Records (one per inspection row, with its SharePoint item when the row links to one) are kept for every row up to
# this bound: a reviewer's decision is tied to the record a finding is about, so it can never move to another row.
MAX_RECORDS = 20000
