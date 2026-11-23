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

The script reads **the entire Sheet1**, including far-right cells, blank gaps, bottom notes and unrelated rows. Formula rendering preserves formula text for comparison. It rechecks the snapshot immediately before writing, extends the grid at its bottom when needed (`appendDimension`), then writes only the exact new bottom row range using `RAW`. It never inserts above existing data, clears cells, sorts, replaces headers, creates tabs or invokes CRM/tracing.

Apply reads the entire target back and requires exact existing values/formulas plus exactly the planned appended rows, allowing only the API's omitted trailing empty cells. It independently verifies every source identity is now present. A second run plans zero appends. If a write times out, **do not blindly retry the write**: rerun audit, then apply; source identity deduplication detects already-landed rows.

GitHub concurrency serializes this workflow, but Google Sheets has no compare-and-swap transaction here. Avoid simultaneous human edits or unrelated writers during apply. A change after preflight can only be detected at readback, not safely rolled back; failure requires operator audit. Grid expansion alone may remain after a later write fails; existing cells are not altered by that expansion. Formatting, comments and protected ranges are not readback-verified; this code does not write them.

## Tests

```sh
python -m pip install PyYAML==6.0.3
python -m unittest discover -s tests -v
```

New tests exercise real source/row planning, an offline mutable Sheets API contract double (no live writes), exact readback and concurrent-edit failures, CLI redaction, atomic cumulative persistence, the real scraper main's premerge capture boundary and partial-collection failure, and workflow safety gates. CI runs Python 3.11.
