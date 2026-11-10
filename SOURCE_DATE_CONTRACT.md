# Source business-date repair

The previous parser deliberately kept timestamp business dates unknown without a verified timezone. Comparison then incorrectly described these existing items as wrong-day records with a blank date. Unknown business days now produce a verification-gap finding, preserve raw evidence and report rows, and retain verification holds. Confirmed wrong-day records and quantity differences still produce real mismatch findings.

## Exact source and runtime evidence

The Alexander City site ID and lists e402d426-4595-47bc-a305-a0704df18258 (2200) and d1a655b8-dc19-4d0f-bad2-35ed9999ccc2 (2214) were verified read-only on 2026-11-10. Regional Settings selected option 11, Central Time (US and Canada), mapped to America/Chicago. Internal Date is Date Only with no default. Date0 also displays Date Only and defaults to Today; it is not substituted for Date.

Each new source capture requires the exact configured Graph site/list identities, current Graph Date/dateOnly metadata with a column GUID, an exact-ID Graph site metadata read, and one read-only REST RegionalSettings/TimeZone GET to that verified site URL using the existing stored photo certificate/token helper. The URL must be HTTPS in the configured SharePoint tenant with a valid site path, no query or fragment. Caller and workbook URLs never determine the credential destination. The capture requires ID 11, exact Central description, bias 360, standard bias 0, and daylight bias -60. Missing auth, unsupported metadata, HTTP errors, invalid schema or changed timezone fail closed. There is no static timezone fallback, credential generation, new grant, or retry loop. Other configured sites/lists can use the same supported timezone only when runtime metadata independently matches; unsupported regional zones remain held. No permanent Alexander City-only allowlist is used.

The metadata protocol has been tested with synthetic replies, not a live app-token request. A post-publication authorized read-only check must establish whether the existing app can retrieve this endpoint and its actual JSON shape. Unavailability remains a verification gap, not proof that a report row is wrong.

Only offset-aware Date timestamps converting to local midnight qualify. Plain calendar dates retain existing behavior. Snapshot hashes include the captured policy revision, column identity, regional metadata, capture time and expiry. Raw timestamps remain untouched.

## Freshness and rollback

New contract-bearing receipts currently expire ten minutes after their metadata capture. Their existing current-receipt, proposal/apply, hold and PDF checks fail closed on expiry or policy change. They require a new source check, not deletion or movement of report rows. This is a bounded freshness policy, not a guarantee of instantaneous detection of a settings change during that interval. Stored pre-contract receipts remain historical and are neither rewritten nor automatically upgraded.

Disabling the exact source policy makes new timestamp captures incomplete and existing contract-bearing receipts non-current. No report data, source settings, stored historical receipts or checker-version number is rewritten by this change. No live source recheck, report mutation or deployment was performed during implementation.

Display links are built from validated exact list metadata as DispForm.aspx?ID=<numeric item>, preserving legacy source identity parsing. Row context uses parsed table header rows; previous records can no longer be mistaken for headers.

## Coverage, consumers, and transport

Verification-gap rows are stored separately in counts from actual mismatch rows. Date/partial-capture gaps are informational review items, not hundreds of confirmed errors; source hard holds still prevent release. The UI shows a dedicated Verification gaps section. Unknown source findings cannot be sent to AI cell-proposal generation, and source-derived changes require a current receipt.

Builder day selection, list-day selection, list-history selection and reconciliation share the same runtime contract and business Date parser. Public build/nexus and history/source handlers read existing certificate settings on a cold worker; no credential is generated or granted. Builder records carry the raw Date unchanged and the capture contract. Legacy dashboard activity endpoints and the separate Pratt retained-snapshot workflow were audited but not enabled as new verified source paths by this repair.

Regional settings and Graph site discovery use an exact-destination no-redirect opener. Token acquisition using the existing helper is limited to the expected Microsoft tenant endpoint under the same no-redirect transport. Synthetic redirect tests establish that foreign-host and same-host wrong-path redirects receive no second request. This does not change general photo-download behavior.

The exact historical signature `not_on_sharepoint_for_day` plus `On SharePoint the item is dated nothing (timezone unconfirmed).` is normalized read-only for review/display into a source-day gap. The stored receipt, snapshot and raw note remain unchanged; a real dated mismatch is not normalized. New canonical source links are captured on a fresh source check; historical receipt URLs are not rewritten.

Legacy activity endpoints now return explicit incomplete/unknown counts when any source day is unavailable, and the Today aggregate displays Unknown rather than summing missing evidence as zero. This is a bounded fail-closed change, not a new source-verification authority for those dashboard endpoints.
