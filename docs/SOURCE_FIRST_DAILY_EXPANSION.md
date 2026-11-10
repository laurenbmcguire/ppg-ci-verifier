# Bounded daily-template expansion

This is an isolated, private, held-report generator delta. It does not activate a source read, scheduler, transport, deployment, release operation or client-facing inbox. The runtime base includes the owner's frozen source-first commits f1c58b8196f7155d08dec1530f37294fa1a03038, 9c26ff4eaaf8da83ecea7d0a83243b1e4daf80a6, 529342ef53eeb8d0ef0f41751b3cbf294756fa53 and bf554678aae69daa65dc4d977c49fb5f6c76de37. Their imported baseline is local commit 3e2136577b80355d74a8dc47431c05459f19f98e. The owner worktree was not edited.

## What changes

- Native OOXML clones the selected shift-detail row styles and moves totals, billing placeholders, merges, validation/conditional-format ranges and print ranges below all approved rows. Source records are neither truncated nor deduplicated.
- The Summary has one row for every distinct exact part string. Numeric surrogate part keys avoid Excel SUMIF's coercion of identifiers such as `0001` and `1` into the same part. Unmapped per-part names remain blank. Manual metadata is not relabeled as downloaded source evidence.
- The chart and ranked table explicitly show **Top 13 rejected records**. The adjacent subtotal is **Shown-record subtotal**. Overall totals and the complete rejection table use all approved records. Ranking formulas recalculate by rejected quantity, then a deterministic ordinal derived from exact source ID, so equal quantities do not repeat the first match. The source and ranked `_Calc` ranges cannot overlap.
- Every rejected record remains in `Rejects & Photos`, with its exact source ID and actual recorded shift. At most eleven explicitly selected representative photos are embedded. The report distinguishes supported captured photos, selected photos, supported photos not selected, unavailable/unattempted candidates, and records with no selected photo. Same image bytes on different records retain separate record associations. Selected photo record hashes must match the captured source version; unchanged original photo bytes remain hash-verified.
- Hidden `_Source` contains one row per approved source record: ID, version, record hash, snapshot hash, mapped source column identity, and original/approved type and value for each mapped field. No unrelated or other-day raw fields enter this ledger. Absent, null, empty text, zero, raw shift representation, raw date representation, leading zeros and overlays remain distinct. Formula-like source text and URLs are inert strings. Hidden is provenance, not secrecy, which is stated in the report.
- Long fields use clearly labeled references to `Source Text`, a printable continuation with record ID, field and segment number. Concatenated segments exactly reproduce the approved value, including whitespace. Line breaks, tabs and wide glyphs are bounded independently. The complete typed values also remain in `_Source`.

## Intentional layout changes

The selected branded workbook and both original brand image byte hashes remain pinned. Existing page size, orientation, margins, widths, headers/footers and styling remain native. Required expansion-only differences are moved rows/merges/drawing anchors/print areas, repeated rejection-table headers, photo-card page breaks, automatic page height for expanded sheets, wrapped detail text with fitted row heights, an expanded subtotal-label merge, and the added source ledger/text continuation. Fit-to-width remains one page. It is not truthful to call an expanded workbook byte-identical to the selected layout; the verifier reconstructs and checks the permitted structural plan instead.

The generator retains the existing labor/inspector, source-uncertainty, required quantity, date/shift, approval, snapshot-binding, private storage, freshness and human-release holds. Additional fields on reviewed rows are provenance-only: original mapped values, source version and mapped column names. No broader source data is exposed.

## Bounds

Existing capture limits are unchanged, including 2,000 scoped rows and the source-photo attempt budget. Expansion has additional explicit fail-closed bounds: 32,767 characters per source cell, 40-character source record IDs, 300-character source versions, 1,200 printed continuation rows, 32 MiB aggregate XML, 3 MiB workbook bytes and a conservative 300-page budget. Oversized or unsupported input is held with no truncation. A supported 2,000-row fixture generated completely within these bounds; this is not a claim that every 2,000-row/long-text combination fits.

Every source-review JSON response also has a 3 MiB exact encoded-byte ceiling, matching the existing private API pilot budget. A small explicit error replaces an oversized response, never truncated rows. A response-size error may follow a successful durable save, so the error tells the reviewer to reload saved receipts before retrying and does not claim that nothing was saved. Binary downloads receive the same ceiling, and generation checks it before saving any new artifact. Vercel documents a 4.5 MB request/response ceiling except verified streamed responses; this pilot does not claim streaming, pagination for oversized reviews, or delivery of every possible 2,000-row/photo combination. See https://vercel.com/docs/functions/limitations .

## Verification and integration

Run the source-first and expansion tests:

    python -m unittest tests.test_source_first_daily tests.test_source_first_expansion -v

Run native generation, conversion and checking in separate processes to avoid retaining Python workbook memory during LibreOffice conversion:

    python tests/verify_source_first_expansion_native.py prepare verification/expansion
    for n in large small long_identifiers changed; do soffice --headless --convert-to xlsx --outdir verification/expansion/native verification/expansion/native-input/$n.xlsx; done
    for n in large small long_identifiers; do soffice --headless --convert-to pdf --outdir verification/expansion/pdf verification/expansion/$n.xlsx; done
    python tests/verify_source_first_expansion_native.py check verification/expansion

Native input copies have every formula cache removed. Therefore the cache comparison requires real native evaluation rather than merely accepting matching preexisting caches. The long-identifier/part case checks that direct-reference caches match displayed continuation markers while exact source strings remain in the typed ledger. The disposable changed copy tests quantity-driven totals and ranking. It is not a deliverable and does not authorize changes to an approved workbook. Any user edits invalidate the original saved verification and require regeneration/review.

The main synthetic fixture has 621 records, all three shifts, three exact parts including `0001` versus `1`, tied quantities, 496 rejected records, twelve supported photos with eleven explicit representatives, and a complete long comment. Unit tests also cover null/absent/blank/zero, typed overlays, stale photo provenance, source-only ledger fields, pathological long-text holds and row-count boundaries through 2,000. Native receipts and rendered PDFs are review evidence, separate from this code-only patch. Parent review of representative pages and ledger coverage remains required before integration. Native PG/browser gates belong to the runtime base and must not be described as evidence for this generator delta without rerunning the relevant combined checks.
