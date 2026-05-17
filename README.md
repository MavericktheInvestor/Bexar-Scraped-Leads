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
- BCAD bulk data is downloaded as a ZIP containing a DBF file; the largest
  DBF is indexed by owner name for address enrichment.
- All records are stored as JSON; no database required.
- Bad/malformed records are skipped silently (never crash).
- Retry logic: 3 attempts per document type with 3-second backoff.
