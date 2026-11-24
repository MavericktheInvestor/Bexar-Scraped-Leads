# Bexar County Motivated Seller Lead Scraper

Automated daily scraper for Bexar County, San Antonio TX public records.
Collects motivated seller signals from the County Clerk portal and enriches
them with BCAD parcel/address data.

---

## File Structure

```
.github/
  workflows/
    scrape.yml          ← GitHub Actions (daily 7 AM UTC + manual)
scraper/
  fetch.py              ← Main scraper (Playwright + requests)
  requirements.txt      ← Python dependencies
dashboard/
  index.html            ← Live intelligence dashboard (GitHub Pages)
  records.json          ← Latest run data (auto-updated)
data/
  records.json          ← Mirror of dashboard/records.json
  leads.csv             ← GHL-ready CSV export
```

---

## Lead Types Scraped

| Code | Label |
|------|-------|
| LP | Lis Pendens |
| NOFC | Notice of Foreclosure |
| TAXDEED | Tax Deed |
| JUD / CCJ / DRJUD | Judgment |
| LNCORPTX / LNIRS / LNFED | Tax / Federal Lien |
| LN / LNMECH / LNHOA | Lien |
| MEDLN | Medicaid Lien |
| PRO | Probate |
| NOC | Notice of Commencement |
| RELLP | Release Lis Pendens |

---

## Seller Score (0–100)

| Component | Points |
|-----------|--------|
| Base score | 30 |
| Per distress flag | +10 |
| LP + Foreclosure combo | +20 |
| Amount > $100k | +15 |
| Amount > $50k | +10 |
| Filed within 7 days | +5 |
| Has property address | +5 |

**Flags:** Lis pendens · Pre-foreclosure · Judgment lien · Tax lien ·
Mechanic lien · Probate / estate · LLC / corp owner · New this week

---

## Setup

### 1. Fork / clone this repo

### 2. Enable GitHub Pages
- Settings → Pages → Source: **GitHub Actions**

### 3. Enable Actions
- Actions tab → enable workflows

### 4. Run manually
- Actions → "Scrape Bexar County Leads" → Run workflow

The scraper runs daily at 07:00 UTC and pushes updated JSON/CSV, then
deploys the dashboard to GitHub Pages automatically.

---

## Local Development

```bash
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
python scraper/fetch.py
```

Output files: `dashboard/records.json`, `data/records.json`, `data/leads.csv`

---

## GHL Import

Use `data/leads.csv` — columns match GoHighLevel contact import format:

- First Name, Last Name
- Mailing Address / City / State / Zip
- Property Address / City / State / Zip
- Lead Type, Document Type, Date Filed, Document Number
- Amount/Debt Owed, Seller Score, Motivated Seller Flags
- Source, Public Records URL

---

## Data Sources

| Source | URL |
|--------|-----|
| Bexar County Clerk | https://bexar.tx.publicsearch.us/ |
| BCAD Bulk Parcel Data | https://www.bcad.org/ |

---

## Notes

- The scraper uses Playwright (Chromium) to navigate the clerk portal's
  JavaScript-heavy search UI.
- BCAD enrichment uses the official `maps.bcad.org` ArcGIS property table (MapServer/9).
  It queries small, ordered batches of complete owner-name variants only after
  county collection succeeds; it does not download the entire appraisal roll.
  The latest valuation year among each matching property's returned rows is
  retained internally (not independently verified as the county's current year);
  no appraisal value is used to change the seller-scoring formula.
- Enrichment rejects API errors, incomplete/repeated pages, and changing counts.
  Any failed batch discards all enrichment for that run. Multiple properties
  for the same name are left unmatched; surname-only matches are not accepted.
  Existing filing addresses are preserved and only missing fields are filled.
- A county-portal security verification or access denial remains a collection
  blocker. The scraper does not bypass that protection; restore a supported,
  authorized county/vendor access route before expecting fresh filings.
  Passing regression tests or working BCAD enrichment alone is not proof of
  a successful live county collection. Failed collection preserves accepted
  dashboard data and does not request parcel enrichment.
- All records are stored as JSON; no database required.
- Bad/malformed records are skipped silently (never crash).
- Retry logic: 3 attempts per document type with 3-second backoff.
