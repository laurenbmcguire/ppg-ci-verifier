# Photo export correction review

Local candidate follow-up to 5194bd9. Not live until deployment and download verification complete.

The original export could write image parts and a new worksheet without registering that worksheet when the workbook used namespace-prefixed XML. Workbook, relationship and content-type registries now use namespace-aware DOM operations. Existing worksheet/media/drawing parts remain byte-identical except the three registries that intentionally add the new sheet. Unique sheet names, sheet IDs and relationship IDs are preserved. The reported tab is the actual added tab.

A separate synthetic reproduction showed that item-only matching could fetch photos from an assigned list despite conflicting record identity. The photo API now requires a resolved, complete, non-needs-person assignment and verifies exact list metadata. Every contributing record is checked against complete IDs or a strict HTTPS origin/list/item identity before deduplication or fetching. Partial, conflicting, foreign and mixed sources fail closed. More than 600 unique records is rejected explicitly rather than silently returning a truncated copy.

The implementer and independent reviewer each ran the frozen nine-suite synthetic gate: 141 tests and 48 subtests passed. Tests cover prefixed registries, repeated export, original cells/formulas/merged ranges/existing image anchors, quote escaping, all relationship targets, content types, explicit invalid-registry errors, and a 31-JPEG workbook with a visible fifth sheet and all 31 reachable image anchors loaded through openpyxl. Source-binding tests exercise the actual API handler with mocked storage/provider boundaries. No real provider requests, customer data, auth/configuration changes or deployment were used by these tests.

This is structural/openpyxl and synthetic API validation, not native Excel UI or a post-deployment real download. The complete combined regression is recorded in the release handoff. Certificate/consent configuration is handled separately and is not included in this code change.
