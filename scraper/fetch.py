"""
Bexar County Motivated Seller Lead Scraper v2
==============================================
Uses the PublicSearch.us REST API that powers the Bexar County clerk portal.
Falls back to direct HTML scraping if API changes.
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
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── optional dbfread ─────────────────────────────────────────────────────────
try:
    from dbfread import DBF
    HAS_DBF = True
except ImportError:
    HAS_DBF = False
    print("⚠  dbfread not installed – parcel enrichment skipped")

# ── Config ────────────────────────────────────────────────────────────────────
CLERK_BASE    = "https://bexar.tx.publicsearch.us"
API_BASE      = "https://bexar.tx.publicsearch.us/api"
LOOKBACK_DAYS = 7
MAX_RETRIES   = 3
RETRY_DELAY   = 4

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

# Paths
ROOT     = Path(__file__).resolve().parent.parent
DASH_DIR = ROOT / "dashboard"
DATA_DIR = ROOT / "data"
DASH_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# ── Helpers ───────────────────────────────────────────────────────────────────

def date_range():
    end   = datetime.utcnow()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")

def date_range_iso():
    end   = datetime.utcnow()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def safe(v):
    return str(v).strip() if v else ""

def parse_amount(raw):
    if not raw:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": CLERK_BASE + "/",
    })
    return s

def retry_get(session, url, **kwargs):
    for i in range(MAX_RETRIES):
        try:
            r = session.get(url, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            if i < MAX_RETRIES - 1:
                print(f"    ↻ retry {i+1} – {e}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    ✗ failed: {e}")
    return None

def retry_post(session, url, **kwargs):
    for i in range(MAX_RETRIES):
        try:
            r = session.post(url, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            if i < MAX_RETRIES - 1:
                print(f"    ↻ retry {i+1} – {e}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    ✗ failed: {e}")
    return None

# ── API Search (primary method) ───────────────────────────────────────────────

def search_via_api(session, doc_type, start_date, end_date):
    """
    Call the PublicSearch.us REST API directly.
    This is the same API the browser calls when you search on the portal.
    """
    records = []

    # First hit the main page to get any session cookies / CSRF tokens
    retry_get(session, CLERK_BASE + "/")
    time.sleep(1)

    page = 1
    per_page = 50

    while True:
        # Try the standard PublicSearch API endpoint
        params = {
            "docTypes":  doc_type,
            "dateFrom":  start_date,
            "dateTo":    end_date,
            "page":      page,
            "perPage":   per_page,
            "county":    "bexar",
        }

        # Try JSON API first
        r = retry_get(session, API_BASE + "/search/instruments", params=params)
        if r and r.headers.get("content-type", "").startswith("application/json"):
            try:
                data = r.json()
                hits = (data.get("data") or data.get("results") or
                        data.get("instruments") or data.get("hits") or [])
                if not hits:
                    break
                for row in hits:
                    rec = parse_api_row(row, doc_type)
                    if rec:
                        records.append(rec)
                # Check pagination
                total = (data.get("total") or data.get("totalCount") or
                         data.get("count") or 0)
                if page * per_page >= total or len(hits) < per_page:
                    break
                page += 1
                time.sleep(0.5)
                continue
            except Exception as e:
                print(f"    ⚠  JSON parse error: {e}")

        # If JSON API didn't work, try the HTML search form
        html_records = search_via_html(session, doc_type, start_date, end_date)
        records.extend(html_records)
        break

    return records


def parse_api_row(row, doc_type):
    """Parse a single API result row into our standard record format."""
    try:
        cat_label = DOC_TYPE_MAP.get(doc_type, doc_type)

        # Handle various field name conventions used by PublicSearch.us
        doc_num = (safe(row.get("instrumentNumber")) or
                   safe(row.get("docNumber")) or
                   safe(row.get("bookPage")) or
                   safe(row.get("id")) or "")

        filed = (safe(row.get("filedDate")) or
                 safe(row.get("recordedDate")) or
                 safe(row.get("dateRecorded")) or "")
        # Normalise date to YYYY-MM-DD
        filed = normalise_date(filed)

        # Grantor = owner/seller
        grantors = row.get("grantors") or row.get("grantor") or []
        if isinstance(grantors, list):
            owner = "; ".join(
                safe(g.get("name") or g.get("fullName") or g) for g in grantors
            )
        else:
            owner = safe(grantors)

        # Grantee = lender/court
        grantees = row.get("grantees") or row.get("grantee") or []
        if isinstance(grantees, list):
            grantee = "; ".join(
                safe(g.get("name") or g.get("fullName") or g) for g in grantees
            )
        else:
            grantee = safe(grantees)

        amount  = parse_amount(row.get("consideration") or row.get("amount") or 0)
        legal   = safe(row.get("legalDescription") or row.get("legal") or "")

        # Direct URL to this document
        inst_id = safe(row.get("id") or row.get("instrumentId") or doc_num)
        clerk_url = f"{CLERK_BASE}/doc/{inst_id}" if inst_id else CLERK_BASE

        return {
            "doc_num":      doc_num,
            "doc_type":     doc_type,
            "cat_label":    cat_label,
            "filed":        filed,
            "owner":        owner,
            "grantee":      grantee,
            "amount":       amount,
            "legal":        legal,
            "clerk_url":    clerk_url,
            "prop_address": "", "prop_city": "", "prop_state": "TX", "prop_zip": "",
            "mail_address": "", "mail_city": "", "mail_state": "TX", "mail_zip": "",
            "flags": [], "score": 0,
        }
    except Exception as e:
        print(f"    ⚠  row parse error: {e}")
        return None


def normalise_date(raw):
    """Convert various date formats to YYYY-MM-DD."""
    if not raw:
        return ""
    raw = str(raw).strip()
    # Already ISO
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    # MM/DD/YYYY
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    # Timestamp with T
    m = re.match(r"(\d{4}-\d{2}-\d{2})T", raw)
    if m:
        return m.group(1)
    return raw

# ── HTML fallback search ──────────────────────────────────────────────────────

def search_via_html(session, doc_type, start_date, end_date):
    """
    Fallback: submit the search form and parse HTML results.
    Handles multiple possible URL patterns for PublicSearch.us.
    """
    records = []
    cat_label = DOC_TYPE_MAP.get(doc_type, doc_type)

    # Try several known URL patterns
    search_urls = [
        f"{CLERK_BASE}/results",
        f"{CLERK_BASE}/search",
        f"{CLERK_BASE}/",
    ]

    search_params = [
        # Pattern 1: query params
        {
            "docType": doc_type,
            "startDate": start_date,
            "endDate": end_date,
            "searchType": "document",
        },
        # Pattern 2: alternate param names
        {
            "type": doc_type,
            "dateFrom": start_date,
            "dateTo": end_date,
        },
    ]

    for url in search_urls:
        for params in search_params:
            r = retry_get(session, url, params=params)
            if not r:
                continue
            rows = parse_html_results(r.text, doc_type, cat_label)
            if rows:
                print(f"    ✅ HTML scrape found {len(rows)} rows at {url}")
                records.extend(rows)
                return records
            time.sleep(0.5)

    # Last resort: try the OData / publicsearch specific endpoint
    odata_records = search_via_odata(session, doc_type, start_date, end_date)
    if odata_records:
        return odata_records

    print(f"    ⚠  No results found for {doc_type} via any method")
    return records


def search_via_odata(session, doc_type, start_date, end_date):
    """Try the OData endpoint that some PublicSearch.us deployments expose."""
    records = []
    cat_label = DOC_TYPE_MAP.get(doc_type, doc_type)

    # Convert dates for OData filter
    try:
        s = datetime.strptime(start_date, "%m/%d/%Y").strftime("%Y-%m-%d")
        e = datetime.strptime(end_date,   "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        s, e = start_date, end_date

    endpoints = [
        f"{API_BASE}/instruments",
        f"{CLERK_BASE}/api/instruments",
        f"{CLERK_BASE}/odata/instruments",
    ]

    filter_str = (f"documentType eq '{doc_type}' and "
                  f"filedDate ge {s} and filedDate le {e}")

    for ep in endpoints:
        r = retry_get(session, ep, params={
            "$filter": filter_str,
            "$top": 500,
            "$orderby": "filedDate desc",
        })
        if not r:
            continue
        try:
            data = r.json()
            items = data.get("value") or data.get("data") or []
            for row in items:
                rec = parse_api_row(row, doc_type)
                if rec:
                    records.append(rec)
            if records:
                print(f"    ✅ OData found {len(records)} records")
                return records
        except Exception:
            pass

    return records


def parse_html_results(html, doc_type, cat_label):
    """Parse HTML table/list results into records."""
    records = []
    soup = BeautifulSoup(html, "lxml")

    tables = soup.find_all("table")
    for tbl in tables:
        headers = [th.get_text(strip=True).lower()
                   for th in tbl.find_all("th")]
        if len(headers) < 3:
            continue

        def col(row, frags):
            for frag in frags:
                for i, h in enumerate(headers):
                    if frag in h:
                        cells = row.find_all("td")
                        return cells[i].get_text(strip=True) if i < len(cells) else ""
            return ""

        for tr in tbl.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if len(cells) < 3:
                continue

            # Get URL from any anchor
            doc_url = ""
            for a in tr.find_all("a"):
                href = a.get("href", "")
                if href:
                    doc_url = href if href.startswith("http") else CLERK_BASE + href
                    break

            doc_num = (col(tr, ["instrument","doc","book","number"]) or
                       cells[0].get_text(strip=True))
            if not doc_num:
                continue

            records.append({
                "doc_num":      doc_num,
                "doc_type":     doc_type,
                "cat_label":    cat_label,
                "filed":        normalise_date(col(tr, ["date","filed","recorded"])),
                "owner":        col(tr, ["grantor","owner","name"]),
                "grantee":      col(tr, ["grantee","lender","trustee"]),
                "amount":       parse_amount(col(tr, ["amount","consider","value"])),
                "legal":        col(tr, ["legal","description","property"]),
                "clerk_url":    doc_url or CLERK_BASE,
                "prop_address": "", "prop_city": "", "prop_state": "TX", "prop_zip": "",
                "mail_address": "", "mail_city": "", "mail_state": "TX", "mail_zip": "",
                "flags": [], "score": 0,
            })

    return records

# ── Playwright deep scraper (last resort) ─────────────────────────────────────

async def scrape_with_playwright(doc_types, start_date, end_date):
    """
    Full browser scrape using Playwright.
    Used when the API and HTML methods return nothing.
    """
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    records = []
    print("\n🎭 Launching Playwright browser scraper …")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        # Load main page first
        try:
            await page.goto(CLERK_BASE, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            print(f"  ✅ Portal loaded: {page.url}")

            # Log all network requests to find the API
            api_calls = []
            page.on("request", lambda req: api_calls.append(req.url)
                    if "api" in req.url or "search" in req.url or
                       "instrument" in req.url.lower() else None)

            for doc_type in doc_types:
                cat_label = DOC_TYPE_MAP.get(doc_type, doc_type)
                print(f"  🔍 Playwright: searching {doc_type} …")
                recs = await _playwright_search(page, doc_type, cat_label,
                                                 start_date, end_date)
                print(f"     → {len(recs)} records")
                records.extend(recs)
                await asyncio.sleep(2)

            # Print discovered API calls for debugging
            if api_calls:
                print("\n  📡 Discovered API endpoints:")
                for url in set(api_calls)[:10]:
                    print(f"     {url}")

        except Exception as e:
            print(f"  ✗ Playwright error: {e}")
        finally:
            await browser.close()

    return records


async def _playwright_search(page, doc_type, cat_label, start_date, end_date):
    from playwright.async_api import TimeoutError as PWTimeout
    records = []

    for attempt in range(MAX_RETRIES):
        try:
            # Navigate back to home
            await page.goto(CLERK_BASE, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            # Take a snapshot of what's on screen for debugging
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")

            # Find all input fields and selects
            inputs  = soup.find_all(["input", "select", "textarea"])
            forms   = soup.find_all("form")
            print(f"     Found {len(inputs)} inputs, {len(forms)} forms")

            # Try to interact with search form
            filled = await _try_fill_form(page, doc_type, start_date, end_date)

            if filled:
                await page.wait_for_load_state("networkidle", timeout=20000)
                await asyncio.sleep(2)
                html = await page.content()
                recs = parse_html_results(html, doc_type, cat_label)
                if recs:
                    return recs

                # Try paginating
                page_count = 0
                while page_count < 20:
                    next_sel = await page.query_selector(
                        "a[aria-label='Next'], button[aria-label='Next'], "
                        ".next a, #nextPage, [data-page='next'], "
                        "a:has-text('Next'), button:has-text('Next')"
                    )
                    if not next_sel:
                        break
                    disabled = await next_sel.get_attribute("disabled")
                    aria_disabled = await next_sel.get_attribute("aria-disabled")
                    if disabled or aria_disabled == "true":
                        break
                    await next_sel.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await asyncio.sleep(1)
                    more = parse_html_results(await page.content(), doc_type, cat_label)
                    records.extend(more)
                    page_count += 1

            return records

        except PWTimeout:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"     ✗ {e}")
            break

    return records


async def _try_fill_form(page, doc_type, start_date, end_date):
    """Try every known way to fill the search form."""
    try:
        # Click advanced search if available
        for sel in ["text=Advanced Search", "text=Advanced", "#advSearch",
                    "[data-tab='advanced']", ".advanced-search-tab"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        # Fill document type
        for sel in [
            f"select option[value='{doc_type}']",
            f"select[name*='type' i]", f"select[id*='type' i]",
            f"select[name*='doc' i]", "select.document-type",
            "#docType", "#documentType",
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    tag = (await el.evaluate("e => e.tagName")).lower()
                    if tag == "select":
                        await el.select_option(value=doc_type)
                    break
            except Exception:
                pass

        # Fill start date
        for sel in ["input[name*='start' i]", "input[name*='from' i]",
                    "input[id*='start' i]", "input[id*='from' i]",
                    "input[placeholder*='from' i]", "input[placeholder*='start' i]",
                    "#startDate", "#dateFrom"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.triple_click()
                    await el.type(start_date)
                    break
            except Exception:
                pass

        # Fill end date
        for sel in ["input[name*='end' i]", "input[name*='to' i]",
                    "input[id*='end' i]", "input[id*='to' i]",
                    "input[placeholder*='to' i]", "input[placeholder*='end' i]",
                    "#endDate", "#dateTo"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.triple_click()
                    await el.type(end_date)
                    break
            except Exception:
                pass

        # Submit
        for sel in ["button[type='submit']", "input[type='submit']",
                    "button:has-text('Search')", "#searchBtn",
                    ".search-button", "button.btn-primary"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    return True
            except Exception:
                pass

    except Exception as e:
        print(f"     ⚠  form fill: {e}")

    return False

# ── BCAD Parcel Lookup ─────────────────────────────────────────────────────────

class ParcelLookup:
    BCAD_URLS = [
        "https://www.bcad.org/clientdb/PropertyExport.zip",
        "https://www.bcad.org/Downloads/PropertyExport.zip",
    ]

    def __init__(self):
        self.index = {}
        self._load()

    def _load(self):
        if not HAS_DBF:
            return
        session = make_session()
        raw = None
        for url in self.BCAD_URLS:
            print(f"  ↓ Trying BCAD parcel data: {url}")
            try:
                r = session.get(url, timeout=120, stream=True)
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(65536):
                    buf.write(chunk)
                raw = buf.getvalue()
                print(f"  ✅ Downloaded {len(raw):,} bytes")
                break
            except Exception as e:
                print(f"  ✗ {e}")

        if not raw:
            print("  ⚠  BCAD download failed – no address enrichment")
            return

        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            dbf_files = sorted(
                [n for n in zf.namelist() if n.lower().endswith(".dbf")],
                key=lambda n: zf.getinfo(n).file_size, reverse=True
            )
            if not dbf_files:
                print("  ⚠  No DBF in ZIP")
                return
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                self._index_dbf(os.path.join(tmp, dbf_files[0]))
            print(f"  ✅ Parcel index: {len(self.index):,} entries")
        except Exception as e:
            print(f"  ✗ Parcel ZIP error: {e}")

    def _index_dbf(self, path):
        try:
            tbl = DBF(path, ignore_missing_memofile=True, encoding="latin-1")
            for row in tbl:
                r = {k.upper(): safe(v) for k, v in row.items()}
                owner = (r.get("OWNER") or r.get("OWN1") or
                         r.get("OWNERNAME") or "").upper().strip()
                if not owner:
                    continue
                parcel = {
                    "prop_address": r.get("SITE_ADDR") or r.get("SITEADDR") or "",
                    "prop_city":    r.get("SITE_CITY") or "",
                    "prop_state":   r.get("SITE_STATE") or "TX",
                    "prop_zip":     r.get("SITE_ZIP") or r.get("SITEZIP") or "",
                    "mail_address": r.get("ADDR_1") or r.get("MAILADR1") or "",
                    "mail_city":    r.get("CITY") or r.get("MAILCITY") or "",
                    "mail_state":   r.get("STATE") or r.get("MAILSTATE") or "TX",
                    "mail_zip":     r.get("ZIP") or r.get("MAILZIP") or "",
                }
                for key in self._variants(owner):
                    self.index.setdefault(key, parcel)
        except Exception as e:
            print(f"  ✗ DBF read: {e}")

    @staticmethod
    def _variants(name):
        name = re.sub(r"\s+", " ", name).strip().upper()
        v = {name}
        if "," in name:
            parts = [p.strip() for p in name.split(",", 1)]
            v.add(f"{parts[1]} {parts[0]}")
        else:
            parts = name.split()
            if len(parts) >= 2:
                v.add(f"{parts[-1]},{' '.join(parts[:-1])}")
                v.add(f"{parts[-1]} {' '.join(parts[:-1])}")
        return list(v)

    def lookup(self, owner):
        if not owner:
            return {}
        for key in self._variants(owner.upper()):
            hit = self.index.get(key)
            if hit:
                return hit
        return {}

# ── Scoring ───────────────────────────────────────────────────────────────────

def score_record(r, week_cutoff):
    flags = []
    score = 30
    dt    = r.get("doc_type", "")
    amt   = r.get("amount", 0.0)
    owner = (r.get("owner") or "").upper()

    if dt in ("LP", "RELLP"):           flags.append("Lis pendens")
    if dt in ("NOFC", "TAXDEED"):       flags.append("Pre-foreclosure")
    if dt in ("JUD", "CCJ", "DRJUD"):   flags.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED"): flags.append("Tax lien")
    if dt == "LNMECH":                  flags.append("Mechanic lien")
    if dt == "LNHOA":                   flags.append("HOA lien")
    if dt == "PRO":                     flags.append("Probate / estate")
    if any(k in owner for k in ("LLC","INC","CORP","LP","LTD","TRUST")):
        flags.append("LLC / corp owner")
    if "Lis pendens" in flags and "Pre-foreclosure" in flags:
        score += 20
    if amt > 100_000:   score += 15
    elif amt > 50_000:  score += 10
    if r.get("filed", "") >= week_cutoff:
        flags.append("New this week")
        score += 5
    if r.get("prop_address"):
        score += 5
    score += 10 * len([f for f in flags if f != "New this week"])
    return flags, min(score, 100)

# ── CSV export ────────────────────────────────────────────────────────────────

def export_csv(records, path):
    fields = [
        "First Name","Last Name",
        "Mailing Address","Mailing City","Mailing State","Mailing Zip",
        "Property Address","Property City","Property State","Property Zip",
        "Lead Type","Document Type","Date Filed","Document Number",
        "Amount/Debt Owed","Seller Score","Motivated Seller Flags",
        "Source","Public Records URL",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            first, last = split_name(r.get("owner",""))
            w.writerow({
                "First Name":             first,
                "Last Name":              last,
                "Mailing Address":        r.get("mail_address",""),
                "Mailing City":           r.get("mail_city",""),
                "Mailing State":          r.get("mail_state","TX"),
                "Mailing Zip":            r.get("mail_zip",""),
                "Property Address":       r.get("prop_address",""),
                "Property City":          r.get("prop_city",""),
                "Property State":         r.get("prop_state","TX"),
                "Property Zip":           r.get("prop_zip",""),
                "Lead Type":              r.get("cat_label",""),
                "Document Type":          r.get("doc_type",""),
                "Date Filed":             r.get("filed",""),
                "Document Number":        r.get("doc_num",""),
                "Amount/Debt Owed":       r.get("amount",""),
                "Seller Score":           r.get("score",0),
                "Motivated Seller Flags": " | ".join(r.get("flags",[])),
                "Source":                 "Bexar County Clerk",
                "Public Records URL":     r.get("clerk_url",""),
            })

def split_name(full):
    if not full:
        return "", ""
    if "," in full:
        parts = [p.strip().title() for p in full.split(",",1)]
        return parts[1], parts[0]
    parts = full.strip().title().split()
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], " ".join(parts[1:])

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  Bexar County Motivated Seller Lead Scraper v2")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)

    start_mm, end_mm   = date_range()       # MM/DD/YYYY for forms
    start_iso, end_iso = date_range_iso()   # YYYY-MM-DD for output
    print(f"\n📅 Date range: {start_iso} → {end_iso}\n")

    # ── Parcel data ───────────────────────────────────────────────────────
    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    # ── Scrape ────────────────────────────────────────────────────────────
    session  = make_session()
    all_recs = []

    print(f"\n🏛  Scraping {len(DOC_TYPE_MAP)} document types …\n")

    for doc_type, label in DOC_TYPE_MAP.items():
        print(f"  [{doc_type}] {label}")
        recs = search_via_api(session, doc_type, start_mm, end_mm)
        print(f"    → {len(recs)} records (API/HTML)")

        if not recs:
            # Try Playwright for this doc type
            try:
                recs = await scrape_with_playwright([doc_type], start_mm, end_mm)
                print(f"    → {len(recs)} records (Playwright)")
            except Exception as e:
                print(f"    ✗ Playwright failed: {e}")

        all_recs.extend(recs)
        time.sleep(1)

    # ── Dedup ─────────────────────────────────────────────────────────────
    seen   = set()
    unique = []
    for r in all_recs:
        key = f"{r['doc_num']}|{r['doc_type']}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    print(f"\n✅ Total unique records: {len(unique)}")

    # ── Enrich + score ────────────────────────────────────────────────────
    with_address = 0
    week_cutoff  = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    for r in unique:
        hit = parcel.lookup(r.get("owner",""))
        if hit:
            r.update(hit)
            with_address += 1
        r["flags"], r["score"] = score_record(r, week_cutoff)

    unique.sort(key=lambda x: x["score"], reverse=True)
    print(f"   With address: {with_address}")

    # ── Save ──────────────────────────────────────────────────────────────
    payload = {
        "fetched_at":   datetime.utcnow().isoformat() + "Z",
        "source":       "Bexar County Clerk / BCAD",
        "data_range":   f"{start_iso} to {end_iso}",
        "total":        len(unique),
        "with_address": with_address,
        "records":      unique,
    }

    for dest in [DASH_DIR / "records.json", DATA_DIR / "records.json"]:
        dest.write_text(json.dumps(payload, indent=2, default=str))
        print(f"💾 {dest}")

    export_csv(unique, DATA_DIR / "leads.csv")
    print(f"📊 {DATA_DIR / 'leads.csv'}")
    print(f"\n🎉 Done. {len(unique)} leads saved.\n")


if __name__ == "__main__":
    asyncio.run(main())
