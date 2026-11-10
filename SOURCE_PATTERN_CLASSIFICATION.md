# Source-confirmed identifier patterns

Base: db0678f7ac19ba61806717e32c4e827cc47af36d. Isolated synthetic development only; no deployment, real workbook/source mutation, external communication or PostgreSQL use.

## Root cause and behavior

The shape warning defaulted to high severity. AI cannot dismiss high-severity findings, and a row-level warning has no editable target. Changing explanation text therefore could not remove it from Needs a person. Quantity reconciliation never proved the exact part field.

The reconciliation now captures bounded outlier-only exact-string receipts for uniquely mapped part/identifier fields on an exact site/list/item and business day. Workbook parsing retains bounded raw text and the actual column separately from normalized display labels. Descriptions/model names, formulas, duplicate headers, ambiguous source columns, duplicate items, incomplete source coverage and unequal text provide no proof. Case, leading zeros and whitespace are significant. No identifier is normalized or written.

`identifier_pattern_evidence` binds immutable file/checker, snapshot, capture, site/list, day, physical cell, field, record identity, source column and exact values. Digest checks catch accidental receipt mutation; this is internal captured evidence, not a signature or external authenticity claim. Source-date contracts and a maximum ten-minute age apply. Truncated/partial records, expired/stale receipts and changed mapping/assignment fail closed. A recheck deliberately invalidates the prior source comparison; a fresh source capture is required. Historical reports lacking the new raw-label metadata need a reviewed recheck plus capture, not a retroactive claim.

`SpreadsheetInbox.review_summary` supplies current source settings to the otherwise fail-closed pure summary. Only undecided checker pattern hints become informational, with explicit unchanged-cell wording. Current and legacy human decisions remain authoritative. AI does not supply proof. Missing NOK evidence and other quantity differences still hold the report but do not invalidate independently complete exact identifier evidence. Approval/release requirements are not relaxed.

UI, paginated inventory, public API/connector responses, delivery previews, AI finding summary/selection, chat and AI saved-result summaries use the source-supported classification. Pagination bindings include classification, so expiry or assignment changes cannot mix pages. Saved-result accounting distinguishes informational source support from both saved corrections and AI dismissals. Generic pure `summarize` callers without current settings remain conservative rather than granting source authority.

## Verification

- Combined focused Python regression: 310 passed, 1 PostgreSQL-specific skip. Exact-pattern module rerun after bounding receipts: 8 passed, including actual report handler, persisted SQLite reopen/recheck/new capture, byte-identical original, missing NOK hold and AI selection exclusion.
- All Node regression tests: 304 passed; JavaScript syntax and diff whitespace checks passed.
- Four pre-existing `test_api_source_binding.py` harness failures reproduce on untouched db0678f (missing stubbed `hosting.photo_embed` import); 2 tests in that module pass. This is not a full-suite pass.
- No real browser/device, hosted, provider, production, PostgreSQL or live-source validation claimed.

Reproduce with the existing test Python environment, CI_LOCAL_DEV=1, VERCEL=0 and PYTHONDONTWRITEBYTECODE=1. Run tests/test_source_pattern_classification.py, test_reconcile.py, test_review_summary.py, test_review_decisions.py, test_finding_summary.py, test_selected_ai_proposals.py, test_source_layout_guard.py, test_format_warning_guidance.py, test_api.py, test_checked_copy.py, test_correction_workflow.py, test_selected_mechanical_corrections.py test_identifiers_layout_evidence.py and test_report_chat.py. Run `node --test tests/test_*.js` and `node --check web/app.js`.

## Independent-review follow-up

Zero-proposal AI completion now re-reads full current report/working-copy evidence before retaining Needs a person entries. Source-confirmed hints are counted separately, with zero saved changes and zero corrected findings; current report controls refresh without overriding newer navigation. Stale informational selections are rejected before provider use with explicit reload/unchanged wording.

Receipts are capped at 256 KiB in addition to 2,000 proofs. Overflow produces an incomplete receipt with no usable proofs, so patterns stay open. Validation hashes the receipt once per summary and builds record/proof indexes once. Synthetic scaling coverage uses 2,000 records with 500 outliers, including 4,000-character identifiers: normal supported hints become informational, oversized receipts stay bounded and all affected hints remain high. Summary time is guarded below five seconds and the full-receipt hash count at most one.

Reusable real-XLSX fixture: tests.test_source_pattern_classification.persisted_fixture_inputs() returns (raw_bytes, snapshot, assigned_source). It includes exact 00356870-V-LH at Inspection Detail row 15 among 26 other shapes, with NOK unknown. The persisted test uses temporary SQLite only and demonstrates the real report handler, public get_report and delivery summary consistency.

Follow-up combined validation: 328 Python tests passed, 1 PostgreSQL-specific skip, 34 subtests passed. Node aggregate rerun includes zero-proposal refresh, stale/partial/expired/current-working checks, navigation protection, stale-ID wording, and informational-banner wording. Parent owns the independent combined browser/integration gate.

## Combined actual-handler browser gate

`tests/browser_source_patterns.py` runs a synthetic local HTTP app at 1200px and 390px. It verifies exact-source shape hints are excluded from open review, AI/manual/Ignore remain available for other findings, unknown NOK remains held, original workbook bytes stay identical, and no approval/send exists. External connections are rejected. Set SOURCE_FIRST_CHROMIUM and EVIDENCE_DIR with the existing Python Playwright environment. The classifier is also integrated with the separate source-first/recovery browser regressions. No live-source verification is implied.
