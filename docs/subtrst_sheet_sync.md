# Dedicated Substitute Trustee sheet sync

Target: existing spreadsheet `1njU3FG3_gtzdhJeitL2sE-bXKPMgR-kYgVDwqmCMbMc`, existing `Sheet1` only.

## Rollout gate

Before enabling the workflow, seed and review `data/subtrst_records.json` from the audited historical union. No seed is generated automatically: the current owner-merged `data/records.json` cannot reconstruct filings lost from historical exports. The workflow fails closed if its archive is absent or incomplete. A seed may use the existing complete-snapshot JSON envelope (`scrape_complete: true`, `records: [...]`, optional exact `total`). Preserve provenance separately; a completeness flag is a source assertion, not independent proof of county-wide historical coverage.

The dedicated workflow is manual audit by default. Daily schedule (14:30 UTC) and successful main-scraper completion use apply. The main scraper's existing collection schedule, scoring, merged exports, old spreadsheet export and CRM paths are unchanged.

## Source and identity contract

- New full collections freeze deep copies of **all enriched/scored SUBTRST records from `in_window`**, before owner-level merge and institutional exclusions. The capture does not use score, business, phone, motivation or address filters.
- After complete collection, `fetch.py` atomically accumulates these records in `data/subtrst_records.json`. Existing archived records win for an already-known exact identity. Failed collection never reaches archive persistence; malformed prior archives fail rather than being replaced. A run with no SUBTRST captures leaves the archive unchanged.
- `doc_num` is a literal string: an 11-digit identifier, or the historical exact ` | `-joined composite. Composites are retained intact, not split, reformatted, reordered or described as independently verified filings. A component and a composite are different source identities.
- Historical `doc_types` membership alone does not establish which filing is SUBTRST. A non-SUBTRST primary record claiming merged SUBTRST membership fails closed until original filing/type provenance is recovered. Other non-SUBTRST records are ignored.
- Duplicate exact source records collapse; conflicting source records or duplicate existing sheet identities fail. Unknown score remains blank; genuine zero remains zero. No default address, city or state is invented.

## Running

Install the Sheets client dependencies used in the workflow:

```sh
python -m pip install google-auth google-auth-httplib2 google-api-python-client
```

Provide the repository's existing `GOOGLE_CREDENTIALS` service-account JSON through the environment/secret store, never command-line arguments or logs. Share the existing spreadsheet with that account as an editor for apply. Audit uses the Sheets **read-only OAuth scope**; apply uses only the Sheets scope, not Drive.

```sh
# Default: audit, cumulative archive
python scraper/subtrst_sheet_sync.py
# Explicit historical snapshot/archive audit
python scraper/subtrst_sheet_sync.py --source path/to/reviewed-archive.json --mode audit
# Only after audit, authorization and source review
python scraper/subtrst_sheet_sync.py --source path/to/reviewed-archive.json --mode apply
```

Equivalent environment settings: `SUBTRST_SOURCE` and `SUBTRST_SYNC_MODE`. The command exits nonzero on validation, authorization, API or readback failure. Audit logs spreadsheet title, tabs and aggregate counts, not lead rows. Access failures report only the safe service-account `client_email`, never the key or raw API exception.

## Preservation and verification

Header mapping is by trimmed, case-insensitive header name, not positional first/last-name assumptions. All 22 existing standard headers must be uniquely present; extra custom/blank columns are left blank on new rows. Missing/ambiguous schema is never automatically repaired. Malformed document cells in recognized lead rows fail; unrelated shifted tables and `Column` headers are preserved, not interpreted as malformed leads or used as document identities. Comma-separated clerk owner names are split; otherwise the intact owner is retained in Last Name instead of inventing a first name.

The script reads **the entire Sheet1**, including far-right cells, blank gaps, bottom notes and unrelated rows. It recognizes existing leads by their filing/lead/source markers, including manual leads absent from the archive. All recognized leads, not just new SUBTRST arrivals, become one contiguous table immediately below the unchanged header, ordered by **Date Filed descending**. Equal-date rows retain their current relative order (new equal-date arrivals are inserted before existing rows). Newest ingestion alone does not determine placement: an older filing arriving later belongs farther down the table.

Planning parses ISO `YYYY-MM-DD`, US `m/d/YYYY`, and numeric Sheets date serials (epoch 1899-12-30, fractional days ignored). Unsupported or missing lead dates fail closed. Dates are **sort keys only**: existing date cells are never normalized or rewritten. Google `sortRange` is deliberately not used because mixed date representations cannot safely be sorted as raw values.

A changing apply sends **one atomic `spreadsheets.batchUpdate`**:

1. If source identities are missing, `insertDimension` adds exactly that many rows below the header, and `updateCells` sets only those newly inserted rows' `userEnteredValue`. Explicit string/number/bool values give RAW-equivalent semantics, including literal strings beginning `=` and genuine numeric zero.
2. Sequential upward-only `moveDimension` requests move **whole rows**, including every column, to the planned chronological positions. Destination indexes are coordinates before removal; using only upward moves avoids ambiguous downward index adjustment. Existing/scattered leads are moved, never copied then cleared, deleted, broadly overwritten or reconstructed. Whole-row operations carry per-lead notes/formatting with their rows; unrelated rows and internal blank gaps retain their relative sequence. Absolute row numbers of unrelated content necessarily change.

No tabs are created or renamed. No existing row's values are updated; no clear, delete, sortRange, CRM or tracing call is made. Offline reconciliation of the actual 334-lead CSV planned 265 whole-row moves to bring the 100 scattered additions into the chronological main table; no new identities were needed.

### Formula gate and exact readback

Before any planned mutation, audit/apply reads all existing tabs with `valueRenderOption=FORMULA` and `dateTimeRenderOption=SERIAL_NUMBER`. **Any existing formula-like cell beginning `=` anywhere in the spreadsheet blocks mutation**, including other-tab references, because this implementation does not attempt to prove rewritten formula-reference semantics. This gate conservatively blocks literal existing strings beginning `=` too. A public CSV showing no formula text does not satisfy this gate: authenticated API reads are required. With no planned changes, existing formulas do not trigger mutation.

Immediately before the batch, apply rechecks all tab value snapshots. Afterward it reads the entire target and requires equality to the deterministic full-sheet simulation:

- Unchanged header, exactly one full-width payload for every original document identity (including all manual/custom columns), plus only the planned inserted identities.
- Exactly the planned newest-filed-first lead ordering and contiguous location.
- Exact ordered nonlead row sequence, including all internal blank rows and far-right values.
- API-omitted trailing empty cells/rows are ignored only for value comparison. Grid/tab metadata must match exactly, except target row count increases by the inserted-row count; moving existing rows must not reduce grid size.
- Other tab snapshots must remain unchanged, and source identity deduplication must independently plan zero further inserts.

Only after all checks pass are `inserted`/`moved` reported as verified. Audit is read-only and reports the insertion/move plan; a second apply plans zero writes. If a batch times out, **do not blindly resend it**: rerun audit, then apply using the refreshed sheet snapshot.

GitHub concurrency serializes this workflow, but Sheets has no compare-and-swap precondition here. Avoid concurrent human edits or unrelated writers during apply; an edit after preflight can only be detected at readback, not safely rolled back. A failed readback requires operator audit and is never reported as no-write or success. Notes, comments, formatting and protected ranges are carried by whole-row operations but are not independently readback-verified. External-spreadsheet formulas are outside the verified scope; coordinate with their owners before moving rows.

## Tests

```sh
python -m pip install PyYAML==6.0.3
python -m unittest discover -s tests -v
```

New tests exercise real source/row planning, an offline mutable Sheets API contract double (no live writes), exact readback and concurrent-edit failures, CLI redaction, atomic cumulative persistence, the real scraper main's premerge capture boundary and partial-collection failure, and workflow safety gates. CI runs Python 3.11.
