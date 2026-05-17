"""
Bexar County Motivated Seller Lead Scraper
==========================================
Fetches clerk documents + enriches with BCAD parcel data.
Outputs: dashboard/records.json, data/records.json, data/leads.csv
"""

import asyncio
import json
import os
import re
import csv
import time
import io
import zipfile
import tempfile
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── optional dbfread ────────────────────────────────────────────────────────
try:
    from dbfread import DBF
    HAS_DBF = True
except ImportError:
    HAS_DBF = False
    print("⚠  dbfread not installed – parcel enrichment will be skipped")

# ── Config ──────────────────────────────────────────────────────────────────
CLERK_BASE      = "https://bexar.tx.publicsearch.us"
LOOKBACK_DAYS   = 7
MAX_RETRIES     = 3
RETRY_DELAY     = 3          # seconds between retries

# Document type → category label
DOC_TYPE_MAP = {
    "LP":       "Lis Pendens",
    "NOFC":     "Notice of Foreclosure",
    "TAXDEED":  "Tax Deed",
    "JUD":      "Judgment",
    "CCJ":      "Certified Judgment",
    "DRJUD":    "Domestic Judgment",
    "LNCORPTX": "Corp Tax Lien",
    "LNIRS":    "IRS Lien",
    "LNFED":    "Federal Lien",
    "LN":       "Lien",
    "LNMECH":   "Mechanic Lien",
    "LNHOA":    "HOA Lien",
    "MEDLN":    "Medicaid Lien",
    "PRO":      "Probate",
    "NOC":      "Notice of Commencement",
    "RELLP":    "Release Lis Pendens",
}

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent.parent
DASH_DIR    = ROOT / "dashboard"
DATA_DIR    = ROOT / "data"
DASH_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# ── Helpers ──────────────────────────────────────────────────────────────────

def today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

def date_range_str() -> tuple[str, str]:
    end   = datetime.utcnow()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def parse_amount(raw: str) -> float:
    if not raw:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", raw)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def safe_str(val) -> str:
    return str(val).strip() if val else ""

def retry(fn, *args, attempts=MAX_RETRIES, delay=RETRY_DELAY, **kwargs):
    """Call fn(*args, **kwargs) up to `attempts` times, returning result or None."""
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if i < attempts - 1:
                print(f"  ↻ retry {i+1}/{attempts-1} – {exc}")
                time.sleep(delay)
            else:
                print(f"  ✗ failed after {attempts} attempts – {exc}")
    return None

# ── Seller-Score Engine ──────────────────────────────────────────────────────

def compute_flags_and_score(rec: dict, filed_cutoff: str) -> tuple[list[str], int]:
    flags: list[str] = []
    score = 30  # base

    dt   = rec.get("doc_type", "")
    amt  = rec.get("amount", 0.0)
    owner = (rec.get("owner") or "").upper()

    # Flags from doc type
    if dt in ("LP", "RELLP"):
        flags.append("Lis pendens")
    if dt in ("NOFC", "TAXDEED"):
        flags.append("Pre-foreclosure")
    if dt in ("JUD", "CCJ", "DRJUD"):
        flags.append("Judgment lien")
    if dt in ("LNCORPTX", "LNIRS", "LNFED"):
        flags.append("Tax lien")
    if dt == "LNMECH":
        flags.append("Mechanic lien")
    if dt in ("PRO",):
        flags.append("Probate / estate")
    if dt == "LNHOA":
        flags.append("HOA lien")

    # LLC / Corp owner
    if any(kw in owner for kw in ("LLC", "INC", "CORP", "LP", "LTD", "TRUST")):
        flags.append("LLC / corp owner")

    # Combo bonus
    if "Lis pendens" in flags and "Pre-foreclosure" in flags:
        score += 20

    # Amount bonuses
    if amt > 100_000:
        score += 15
    elif amt > 50_000:
        score += 10

    # New this week
    filed = rec.get("filed", "")
    if filed >= filed_cutoff:
        flags.append("New this week")
        score += 5

    # Has address
    if rec.get("prop_address"):
        score += 5

    # Per-flag bonus
    score += 10 * len([f for f in flags if f != "New this week"])

    return flags, min(score, 100)

# ── BCAD Parcel Lookup ───────────────────────────────────────────────────────

class ParcelLookup:
    """
    Downloads the BCAD bulk DBF export (or falls back to CSV),
    builds an owner-name index for fast look-ups.
    """

    BCAD_DBF_URLS = [
        # Primary: BCAD direct shapefile / DBF bundle
        "https://www.bcad.org/clientdb/PropertyExport.zip",
        # Alternate mirrors sometimes used
        "https://www.bcad.org/Downloads/PropertyExport.zip",
    ]

    def __init__(self):
        self.index: dict[str, dict] = {}   # normalised-name → parcel row
        self._load()

    # ── internal ──────────────────────────────────────────────────────────────

    def _download_zip(self, url: str) -> bytes | None:
        print(f"  ↓ Downloading parcel data from {url} …")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (BexarLeadScraper/1.0)"}
            r = requests.get(url, headers=headers, timeout=120, stream=True)
            r.raise_for_status()
            buf = io.BytesIO()
            for chunk in r.iter_content(chunk_size=65536):
                buf.write(chunk)
            return buf.getvalue()
        except Exception as exc:
            print(f"  ✗ Download failed: {exc}")
            return None

    def _load(self):
        if not HAS_DBF:
            print("⚠  Parcel lookup disabled (dbfread missing)")
            return

        raw = None
        for url in self.BCAD_DBF_URLS:
            raw = self._download_zip(url)
            if raw:
                break

        if not raw:
            print("⚠  Could not download parcel data – address enrichment skipped")
            return

        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            print("⚠  Downloaded file is not a valid ZIP")
            return

        dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
        if not dbf_names:
            print("⚠  No .dbf found in ZIP")
            return

        # Pick the largest DBF (usually the property table)
        dbf_names.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
        dbf_name = dbf_names[0]
        print(f"  📄 Reading {dbf_name} …")

        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)
            dbf_path = os.path.join(tmp, dbf_name)
            self._index_dbf(dbf_path)

        print(f"  ✅ Parcel index built: {len(self.index):,} entries")

    def _index_dbf(self, path: str):
        try:
            table = DBF(path, ignore_missing_memofile=True, encoding="latin-1")
            for row in table:
                rec = {k.upper(): safe_str(v) for k, v in row.items()}
                owner_raw = (
                    rec.get("OWNER") or rec.get("OWN1") or rec.get("OWNERNAME") or ""
                ).upper().strip()
                if not owner_raw:
                    continue

                parcel = {
                    "prop_address": rec.get("SITE_ADDR") or rec.get("SITEADDR") or "",
                    "prop_city":    rec.get("SITE_CITY") or "",
                    "prop_state":   rec.get("SITE_STATE") or "TX",
                    "prop_zip":     rec.get("SITE_ZIP") or rec.get("SITEZIP") or "",
                    "mail_address": rec.get("ADDR_1") or rec.get("MAILADR1") or rec.get("MAIL_ADDR") or "",
                    "mail_city":    rec.get("CITY") or rec.get("MAILCITY") or "",
                    "mail_state":   rec.get("STATE") or rec.get("MAILSTATE") or "TX",
                    "mail_zip":     rec.get("ZIP") or rec.get("MAILZIP") or "",
                }

                for key in self._name_variants(owner_raw):
                    self.index.setdefault(key, parcel)
        except Exception as exc:
            print(f"  ✗ DBF read error: {exc}")

    @staticmethod
    def _name_variants(name: str) -> list[str]:
        """Return normalised look-up keys for FIRST LAST / LAST FIRST / LAST,FIRST."""
        name = re.sub(r"\s+", " ", name).strip().upper()
        variants = {name}
        # "LAST, FIRST" → also index "FIRST LAST"
        if "," in name:
            parts = [p.strip() for p in name.split(",", 1)]
            variants.add(f"{parts[1]} {parts[0]}")
        else:
            parts = name.split()
            if len(parts) >= 2:
                variants.add(f"{parts[-1]},{' '.join(parts[:-1])}")
                variants.add(f"{parts[-1]} {' '.join(parts[:-1])}")
        return list(variants)

    # ── public ────────────────────────────────────────────────────────────────

    def lookup(self, owner: str) -> dict:
        if not owner:
            return {}
        for key in self._name_variants(owner.upper()):
            hit = self.index.get(key)
            if hit:
                return hit
        return {}

# ── Clerk Portal Scraper (Playwright) ───────────────────────────────────────

async def scrape_clerk(doc_types: list[str], start_date: str, end_date: str) -> list[dict]:
    """
    Uses Playwright to search the Bexar County public search portal for each
    document type and collect results over the lookback window.
    """
    records: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        page = await context.new_page()

        for doc_type in doc_types:
            cat_label = DOC_TYPE_MAP.get(doc_type, doc_type)
            print(f"  🔍 Searching {doc_type} ({cat_label}) …")
            fetched = await _search_doc_type(page, doc_type, cat_label,
                                             start_date, end_date)
            print(f"     → {len(fetched)} records")
            records.extend(fetched)
            await asyncio.sleep(1)           # polite delay

        await browser.close()

    return records


async def _search_doc_type(page, doc_type: str, cat_label: str,
                            start_date: str, end_date: str) -> list[dict]:
    records: list[dict] = []

    for attempt in range(MAX_RETRIES):
        try:
            # Navigate to search page
            await page.goto(f"{CLERK_BASE}/", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=20_000)

            # ── Try the Advanced / Document Type search form ─────────────────
            # The public search UI varies; we attempt common selectors.
            filled = await _fill_search_form(page, doc_type, start_date, end_date)
            if not filled:
                print(f"     ⚠  Could not fill form for {doc_type}")
                return records

            # Wait for results table
            await page.wait_for_selector("table, .results-list, #resultsGrid",
                                          timeout=20_000)
            await asyncio.sleep(1)

            # Paginate through all pages
            page_num = 1
            while True:
                rows = await _extract_rows(page, doc_type, cat_label)
                records.extend(rows)

                # Try to go to next page
                next_btn = await page.query_selector(
                    "a.next-page, button.next-page, [aria-label='Next page'], "
                    ".pager-next a, #nextPage"
                )
                if not next_btn:
                    break
                is_disabled = await next_btn.get_attribute("disabled")
                if is_disabled:
                    break
                await next_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)
                page_num += 1
                if page_num > 50:   # safety cap
                    break

            return records

        except PWTimeout:
            if attempt < MAX_RETRIES - 1:
                print(f"     ↻ timeout, retry {attempt+1} …")
                await asyncio.sleep(RETRY_DELAY)
            else:
                print(f"     ✗ giving up on {doc_type}")
        except Exception as exc:
            print(f"     ✗ error on {doc_type}: {exc}")
            break

    return records


async def _fill_search_form(page, doc_type: str,
                             start_date: str, end_date: str) -> bool:
    """
    Attempt to fill the Bexar public search form.
    Returns True on success.
    """
    try:
        # Click the "Document Type" or "Advanced Search" tab if present
        for tab_sel in ["text=Advanced", "text=Document Search",
                        "#advancedSearchTab", ".tab-advanced"]:
            tab = await page.query_selector(tab_sel)
            if tab:
                await tab.click()
                await asyncio.sleep(0.5)
                break

        # Document type dropdown / input
        for dt_sel in [
            "select[name='docType']", "select#docType",
            "input[name='docType']", "input#docType",
            "select[id*='type' i]", "input[placeholder*='type' i]",
        ]:
            el = await page.query_selector(dt_sel)
            if el:
                tag = await el.evaluate("e => e.tagName.toLowerCase()")
                if tag == "select":
                    await el.select_option(value=doc_type)
                else:
                    await el.fill(doc_type)
                break

        # Date-from
        for df_sel in [
            "input[name='startDate']", "input#startDate",
            "input[name='dateFrom']", "input[placeholder*='from' i]",
            "input[aria-label*='start' i]",
        ]:
            el = await page.query_selector(df_sel)
            if el:
                await el.fill(start_date)
                break

        # Date-to
        for dt_sel2 in [
            "input[name='endDate']", "input#endDate",
            "input[name='dateTo']", "input[placeholder*='to' i]",
            "input[aria-label*='end' i]",
        ]:
            el = await page.query_selector(dt_sel2)
            if el:
                await el.fill(end_date)
                break

        # Submit
        for sub_sel in [
            "button[type='submit']", "input[type='submit']",
            "button:has-text('Search')", "#searchBtn", ".search-btn",
        ]:
            el = await page.query_selector(sub_sel)
            if el:
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=20_000)
                return True

    except Exception as exc:
        print(f"     ⚠  form fill error: {exc}")

    return False


async def _extract_rows(page, doc_type: str, cat_label: str) -> list[dict]:
    """Parse every result row visible on the current page."""
    records: list[dict] = []
    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    # Try generic table rows first
    tables = soup.find_all("table")
    for tbl in tables:
        headers = [th.get_text(strip=True).lower()
                   for th in tbl.find_all("th")]
        if not headers:
            continue

        def col(row, name_fragments):
            for frag in name_fragments:
                for i, h in enumerate(headers):
                    if frag in h:
                        cells = row.find_all("td")
                        return cells[i].get_text(strip=True) if i < len(cells) else ""
            return ""

        def col_link(row, name_fragments):
            for frag in name_fragments:
                for i, h in enumerate(headers):
                    if frag in h:
                        cells = row.find_all("td")
                        if i < len(cells):
                            a = cells[i].find("a")
                            if a and a.get("href"):
                                href = a["href"]
                                return href if href.startswith("http") \
                                    else CLERK_BASE + href
            return ""

        for tr in tbl.find_all("tr")[1:]:   # skip header row
            cells = tr.find_all("td")
            if len(cells) < 3:
                continue

            # Try to pull a direct URL from any anchor in the row
            doc_url = ""
            for a in tr.find_all("a"):
                href = a.get("href", "")
                if href:
                    doc_url = href if href.startswith("http") \
                        else CLERK_BASE + href
                    break

            rec = {
                "doc_num":    col(tr, ["doc", "instrument", "book"]) or cells[0].get_text(strip=True),
                "doc_type":   doc_type,
                "cat_label":  cat_label,
                "filed":      col(tr, ["date", "filed", "record"]),
                "owner":      col(tr, ["grantor", "owner", "grantee"]),
                "grantee":    col(tr, ["grantee", "trustee", "lender"]),
                "amount":     parse_amount(col(tr, ["amount", "value", "consider"])),
                "legal":      col(tr, ["legal", "description", "property"]),
                "clerk_url":  doc_url,
                # address fields filled later by parcel lookup
                "prop_address": "", "prop_city": "", "prop_state": "TX", "prop_zip": "",
                "mail_address": "", "mail_city": "", "mail_state": "TX", "mail_zip": "",
                "flags": [], "score": 0,
            }
            if rec["doc_num"]:
                records.append(rec)

    # Fallback: look for list/card layouts
    if not records:
        for item in soup.select(".result-item, .record-row, .search-result"):
            def txt(sel):
                el = item.select_one(sel)
                return el.get_text(strip=True) if el else ""

            a_el = item.select_one("a[href]")
            href = a_el["href"] if a_el else ""
            doc_url = href if href.startswith("http") else CLERK_BASE + href if href else ""

            rec = {
                "doc_num":    txt(".doc-num, .instrument-no, .book"),
                "doc_type":   doc_type,
                "cat_label":  cat_label,
                "filed":      txt(".date, .filed-date"),
                "owner":      txt(".grantor, .owner"),
                "grantee":    txt(".grantee"),
                "amount":     parse_amount(txt(".amount, .consideration")),
                "legal":      txt(".legal, .description"),
                "clerk_url":  doc_url,
                "prop_address": "", "prop_city": "", "prop_state": "TX", "prop_zip": "",
                "mail_address": "", "mail_city": "", "mail_state": "TX", "mail_zip": "",
                "flags": [], "score": 0,
            }
            if rec["doc_num"]:
                records.append(rec)

    return records

# ── Main Orchestrator ────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  Bexar County Motivated Seller Lead Scraper")
    print(f"  Run: {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)

    start_date, end_date = date_range_str()
    print(f"\n📅 Date range: {start_date} → {end_date}\n")

    # ── 1. Build parcel index ─────────────────────────────────────────────
    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    # ── 2. Scrape clerk portal ────────────────────────────────────────────
    doc_types = list(DOC_TYPE_MAP.keys())
    print(f"\n🏛  Scraping clerk portal for {len(doc_types)} doc types …")
    records = await scrape_clerk(doc_types, start_date, end_date)
    print(f"\n✅ Raw records collected: {len(records)}")

    # ── 3. Deduplicate ────────────────────────────────────────────────────
    seen: set[str] = set()
    unique: list[dict] = []
    for r in records:
        key = f"{r['doc_num']}|{r['doc_type']}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    print(f"   After dedup: {len(unique)}")

    # ── 4. Enrich with parcel data ────────────────────────────────────────
    print("\n🗺  Enriching with parcel addresses …")
    with_address = 0
    week_cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    for r in unique:
        hit = parcel.lookup(r.get("owner", ""))
        if hit:
            r.update(hit)
            with_address += 1

        flags, score = compute_flags_and_score(r, week_cutoff)
        r["flags"] = flags
        r["score"] = score

    print(f"   Records with address: {with_address}/{len(unique)}")

    # ── 5. Sort by score descending ───────────────────────────────────────
    unique.sort(key=lambda x: x["score"], reverse=True)

    # ── 6. Build output payload ───────────────────────────────────────────
    payload = {
        "fetched_at":    datetime.utcnow().isoformat() + "Z",
        "source":        "Bexar County Clerk / BCAD",
        "data_range":    f"{start_date} to {end_date}",
        "total":         len(unique),
        "with_address":  with_address,
        "records":       unique,
    }

    # ── 7. Save JSON ──────────────────────────────────────────────────────
    for dest in [DASH_DIR / "records.json", DATA_DIR / "records.json"]:
        dest.write_text(json.dumps(payload, indent=2, default=str))
        print(f"💾 Saved → {dest}")

    # ── 8. Export CSV for GHL ─────────────────────────────────────────────
    csv_path = DATA_DIR / "leads.csv"
    _export_csv(unique, csv_path)
    print(f"📊 CSV → {csv_path}")

    print("\n🎉 Done.\n")
    return payload


def _export_csv(records: list[dict], path: Path):
    """GoHighLevel-compatible CSV export."""
    fieldnames = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            owner = r.get("owner", "")
            first, last = _split_name(owner)
            writer.writerow({
                "First Name":            first,
                "Last Name":             last,
                "Mailing Address":       r.get("mail_address", ""),
                "Mailing City":          r.get("mail_city", ""),
                "Mailing State":         r.get("mail_state", "TX"),
                "Mailing Zip":           r.get("mail_zip", ""),
                "Property Address":      r.get("prop_address", ""),
                "Property City":         r.get("prop_city", ""),
                "Property State":        r.get("prop_state", "TX"),
                "Property Zip":          r.get("prop_zip", ""),
                "Lead Type":             r.get("cat_label", ""),
                "Document Type":         r.get("doc_type", ""),
                "Date Filed":            r.get("filed", ""),
                "Document Number":       r.get("doc_num", ""),
                "Amount/Debt Owed":      r.get("amount", ""),
                "Seller Score":          r.get("score", 0),
                "Motivated Seller Flags": " | ".join(r.get("flags", [])),
                "Source":                "Bexar County Clerk",
                "Public Records URL":    r.get("clerk_url", ""),
            })


def _split_name(full: str) -> tuple[str, str]:
    """Best-effort split of 'LAST, FIRST' or 'FIRST LAST' into (first, last)."""
    if not full:
        return "", ""
    if "," in full:
        parts = [p.strip().title() for p in full.split(",", 1)]
        return parts[1], parts[0]
    parts = full.strip().title().split()
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], " ".join(parts[1:])


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())
