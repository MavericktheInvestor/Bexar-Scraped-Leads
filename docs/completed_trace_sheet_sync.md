# Completed trace → master contact backfill

Destination is explicitly fixed to spreadsheet
`1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk`, tab `Sheet1`.
This tool does **not** call Deal Machine, consume tracing credits, or modify a CRM.

## Run

```bash
python -m unittest discover -s tests -p "test_completed_trace*.py" -v
python scraper/completed_trace_sheet_sync.py --mode audit --summary completed-trace-audit.json
# After reviewing the hosted audit and resolving exceptions:
python scraper/completed_trace_sheet_sync.py --mode apply --summary completed-trace-audit.json
```

Use the existing hosted `GOOGLE_CREDENTIALS` service-account secret. Audit requests a
Sheets read-only scope; apply requests only the Sheets scope. Local OAuth setup is
not a prerequisite. Never download the hosted secret for a local run.

The workflow dispatch defaults to **audit** and allows explicit manual **apply**.
The daily schedule and successful, same-repository/main completions of
**Skip Trace Leads (Deal Machine)** and **Build Skip Trace Queue** default to
**audit**. Only after reviewing the hosted audit, explicitly set the repository
variable `COMPLETED_TRACE_MASTER_APPLY_ENABLED` to `true` to enable recurring
**apply**; unset it or set it to `false` to return automatic runs to audit.
This is a nonsecret variable; no new secret is required. Checkout takes current `main`
with full history because tracing commits its CSV during the producer run.
The workflow runs credential-free stdlib tests before accessing the secret and
uploads a sanitized JSON report even on a synchronization failure.

## Source precedence and holds

* Parse every available Git revision of `data/skip_traced.csv`, plus the current
  working copy, and read the live `Skip Trace Queue` using its actual headers.
  Shallow history, deleted/unreadable revisions, malformed CSV, and ambiguous
  headers fail closed. An old pre-trace CSV schema is valid but cannot qualify.
* Live queue is the newest observed source snapshot; then current CSV, then Git
  revisions in Git date/topology order. A blank status only in the rebuilt live
  queue is a reset, not a revocation: a compatible historical Done can qualify.
  Nonblank live decisions (including Pending, Hold, Skipped and No Phone) block.
  The newest present historical non-Done row still blocks, including historical
  blanks; absence never revokes a result. Reset live rows still participate in
  duplicate and owner/property collision checks.
* Only Done rows with supported phones and an explicit ISO trace date qualify.
  Choose the newest supported completion date; conflicting phones on the same
  date, duplicate identities within a snapshot, owner/property collisions, and
  invalid literal document numbers hold the whole identity.
* Composite document numbers are **one exact identity**, never split into
  filings. Numeric document cells returned by Sheets are matched losslessly;
  CSV identity strings are never trimmed, split or repaired.

## Write contract

* Match an existing master row by exact document identity, then require matching
  first/last name and property address (case/whitespace comparison only).
  Missing master leads are reported as holds: no row reconstruction/insertion.
* Require one destination row per identity. Differing nonblank primary/alternate
  phones or trace status/date hold the entire row. Equivalent US phone formatting
  is recognized without rewriting manual values; remaining numbers use blank
  phone slots. Manual lead Hold/Skipped/No Phone/DNC/Do Not Contact status is held.
  Valid integer source phones are written as literal strings, not numeric cells.
  Unrecognized phone-related headers fail closed for schema review.
* Map columns by header labels, not letters. Never write Date Filed or generic
  lead Status as trace status/date. Missing trace columns are added only after
  the existing grid's far-right edge, and only if a lead actually needs a write.
* One atomic Sheets `batchUpdate` appends required columns and writes exact
  `userEnteredValue` cells only. There is no clear/rebuild, row movement, table
  append, or contact-data overwrite. That field mask leaves native notes and
  formatting intact; these are not independently proved by values readback.
* Read every tab's full FORMULA matrix and grid metadata. Immediately before
  writing, re-read all snapshots and abort on any change. After writing compare
  every old cell/formula, every new cell, every other tab and grid dimensions
  against the exact expected result, then require a zero-cell immediate replan.
  Sheets has no compare-and-swap transaction: avoid concurrent human/other writers.
  On a timeout/failure do not blindly replay; rerun audit and inspect the report.

Reports/logs contain counts, schema headers, tab/grid metadata and constant error
messages only—no names, addresses, document IDs, phones, credentials or raw API
errors. A verified safe patch can still have held exceptions; it is **not** a
claim that every requested lead was copied. Review `source_holds`, `master_holds`,
`planned_leads`, `planned_cells`, and `history` separately.
