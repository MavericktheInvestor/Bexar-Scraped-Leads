"""
Bexar County Motivated Seller Lead Scraper v5
==============================================
Fixes:
  - Wait for results to fully load before scraping
  - Correct owner/grantor name extraction
  - Fix document number parsing
  - Prevent duplicate records across doc types
  - Better parcel address matching
"""

import asyncio, json, re, csv, time, io, zipfile, tempfile, os
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from dbfread import DBF
    HAS_DBF = True
except ImportError:
    HAS_DBF = False
    print("⚠  dbfread not installed")

# ── Config ────────────────────────────────────────────────────────────────────
CLERK_BASE    = "https://bexar.tx.publicsearch.us"
LOOKBACK_DAYS = 7
MAX_RETRIES   = 3
RETRY_DELAY   = 3

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

ROOT     = Path(__file__).resolve().parent.parent
DASH_DIR = ROOT / "dashboard"
DATA_DIR = ROOT / "data"
DASH_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# ── Date helpers ──────────────────────────────────────────────────────────────
def date_range_mm():
    e = datetime.utcnow()
    s = e - timedelta(days=LOOKBACK_DAYS)
    return s.strftime("%m/%d/%Y"), e.strftime("%m/%d/%Y")

def date_range_iso():
    e = datetime.utcnow()
    s = e - timedelta(days=LOOKBACK_DAYS)
    return s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")

def to_iso(raw):
    if not raw: return ""
    raw = str(raw).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw): return raw[:10]
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m: return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    m = re.match(r"(\d{4}-\d{2}-\d{2})T", raw)
    if m: return m.group(1)
    return raw

def safe(v): return str(v).strip() if v else ""

def parse_amt(raw):
    c = re.sub(r"[^\d.]", "", str(raw or ""))
    try: return float(c)
    except: return 0.0

def blank_record(doc_type):
    return {
        "doc_num": "", "doc_type": doc_type,
        "cat_label": DOC_TYPE_MAP.get(doc_type, doc_type),
        "filed": "", "owner": "", "grantee": "",
        "amount": 0.0, "legal": "", "clerk_url": CLERK_BASE,
        "prop_address": "", "prop_city": "", "prop_state": "TX", "prop_zip": "",
        "mail_address": "", "mail_city": "", "mail_state": "TX", "mail_zip": "",
        "flags": [], "score": 0,
    }

# ── HTTP Session ──────────────────────────────────────────────────────────────
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": CLERK_BASE + "/",
    })
    return s

def safe_get(sess, url, **kw):
    for i in range(MAX_RETRIES):
        try:
            r = sess.get(url, timeout=30, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            if i < MAX_RETRIES - 1: time.sleep(RETRY_DELAY)
            else: print(f"    ✗ GET {url[:80]} → {e}")
    return None

# ── Parse a fully-loaded results page ────────────────────────────────────────
def parse_results_html(html, doc_type):
    """
    Parse the PublicSearch.us results page HTML.
    Handles both table format and card/list format.
    Returns list of record dicts.
    """
    cat  = DOC_TYPE_MAP.get(doc_type, doc_type)
    recs = []
    soup = BeautifulSoup(html, "lxml")

    # ── Skip pages that haven't loaded yet ───────────────────────────────
    loading_indicators = soup.find_all(
        string=re.compile(r"loading results|please wait|searching", re.I)
    )
    if loading_indicators:
        print(f"    ⚠  Page still loading — will retry")
        return []

    # ── Check for no results ─────────────────────────────────────────────
    no_results = soup.find_all(
        string=re.compile(r"no results|no records found|0 results", re.I)
    )
    if no_results:
        return []

    # ── TABLE FORMAT ─────────────────────────────────────────────────────
    for tbl in soup.find_all("table"):
        hdrs = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
        if len(hdrs) < 2:
            continue

        # Map header fragments to column indices
        def find_col(tr, *frags):
            cells = tr.find_all("td")
            for frag in frags:
                for i, h in enumerate(hdrs):
                    if frag in h and i < len(cells):
                        txt = cells[i].get_text(" ", strip=True)
                        if txt: return txt
            return ""

        for tr in tbl.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue

            # Get doc URL from any link in the row
            doc_url = ""
            for a in tr.find_all("a"):
                h = a.get("href", "")
                if h and ("doc" in h or "instrument" in h or "detail" in h):
                    doc_url = h if h.startswith("http") else CLERK_BASE + h
                    break
            if not doc_url:
                for a in tr.find_all("a"):
                    h = a.get("href", "")
                    if h:
                        doc_url = h if h.startswith("http") else CLERK_BASE + h
                        break

            # Extract fields
            doc_num = find_col(tr,
                "instrument", "doc #", "doc#", "book", "number", "docnum", "ref")
            if not doc_num:
                doc_num = cells[0].get_text(strip=True)

            # Skip obvious non-records
            if not doc_num or doc_num.lower() in ("loading", "please", "searching"):
                continue

            filed   = to_iso(find_col(tr, "date", "filed", "recorded", "entry"))
            owner   = find_col(tr, "grantor", "owner", "seller", "debtor")
            grantee = find_col(tr, "grantee", "lender", "trustee", "plaintiff", "creditor")
            amount  = parse_amt(find_col(tr, "amount", "consideration", "value", "debt"))
            legal   = find_col(tr, "legal", "description", "subdivision", "property")

            rec = blank_record(doc_type)
            rec.update({
                "doc_num":   doc_num,
                "filed":     filed,
                "owner":     owner,
                "grantee":   grantee,
                "amount":    amount,
                "legal":     legal,
                "clerk_url": doc_url or CLERK_BASE,
            })
            recs.append(rec)

    if recs:
        return recs

    # ── CARD / LIST FORMAT (PublicSearch.us uses div-based cards) ─────────
    # Try to find result cards — PublicSearch renders results as divs/rows
    card_selectors = [
        ".instrument-row", ".result-row", ".search-result",
        ".record-item", ".instrument-item", "tr.result",
        "[data-instrument]", "[data-id]",
        ".ng-scope tr",  # Angular-based results
    ]

    cards = []
    for sel in card_selectors:
        found = soup.select(sel)
        if found:
            cards = found
            print(f"    ℹ  Found {len(found)} cards with selector: {sel}")
            break

    for card in cards:
        text_blocks = [
            el.get_text(strip=True)
            for el in card.find_all(["td", "span", "div", "p"])
            if el.get_text(strip=True)
        ]
        if not text_blocks:
            continue

        a_el    = card.select_one("a[href]")
        href    = a_el["href"] if a_el else ""
        doc_url = href if href.startswith("http") else CLERK_BASE + href if href else ""

        # Try to extract structured data from data attributes
        doc_num = (card.get("data-instrument") or card.get("data-id") or
                   card.get("data-doc") or "")

        # If no data attributes, try label-based extraction
        def find_after_label(labels):
            for i, txt in enumerate(text_blocks):
                if any(lbl.lower() in txt.lower() for lbl in labels):
                    if i + 1 < len(text_blocks):
                        return text_blocks[i + 1]
            return ""

        if not doc_num:
            doc_num = find_after_label(
                ["instrument", "doc #", "doc number", "book/page"]
            )

        owner   = find_after_label(["grantor", "owner", "seller", "debtor"])
        grantee = find_after_label(["grantee", "lender", "trustee", "plaintiff"])
        filed   = to_iso(find_after_label(["date", "filed", "recorded"]))
        amount  = parse_amt(find_after_label(["amount", "consideration"]))
        legal   = find_after_label(["legal", "description", "subdivision"])

        if not doc_num:
            continue

        rec = blank_record(doc_type)
        rec.update({
            "doc_num":   doc_num,
            "filed":     filed,
            "owner":     owner,
            "grantee":   grantee,
            "amount":    amount,
            "legal":     legal,
            "clerk_url": doc_url or CLERK_BASE,
        })
        recs.append(rec)

    return recs


# ── Playwright scraper ────────────────────────────────────────────────────────
async def playwright_scrape(doc_types, start_mm, end_mm, start_iso):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    all_recs = []
    print("\n🎭 Playwright browser scrape …")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        # ── Intercept JSON API calls made by the portal ───────────────────
        api_hits = []
        async def capture_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "json" in ct and any(
                    k in resp.url for k in
                    ["api", "instrument", "search", "result", "record", "query"]
                ):
                    body = await resp.json()
                    api_hits.append({"url": resp.url, "body": body})
                    print(f"    📡 API call: {resp.url[:80]}")
            except Exception:
                pass
        page.on("response", capture_response)

        try:
            # ── Load portal ───────────────────────────────────────────────
            print("  → Loading portal …")
            await page.goto(CLERK_BASE, timeout=45000)
            await page.wait_for_load_state("networkidle", timeout=25000)
            await asyncio.sleep(3)

            title = await page.title()
            print(f"  Page: {title} | URL: {page.url}")

            # ── Dismiss disclaimer ────────────────────────────────────────
            for txt in ["Accept", "I Agree", "Continue", "OK", "Close", "Agree"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(txt, re.I))
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click()
                        await asyncio.sleep(1.5)
                        print(f"  ✅ Dismissed: {txt}")
                        break
                except Exception:
                    pass

            # ── Print ALL form elements for debugging ─────────────────────
            html_snap = await page.content()
            soup_snap = BeautifulSoup(html_snap, "lxml")
            inputs    = soup_snap.find_all(["input", "select", "textarea", "button"])
            print(f"\n  Form elements found ({len(inputs)}):")
            for el in inputs[:20]:
                print(f"    <{el.name}> "
                      f"id='{el.get('id','')}' "
                      f"name='{el.get('name','')}' "
                      f"class='{' '.join(el.get('class',[]))}' "
                      f"type='{el.get('type','')}' "
                      f"placeholder='{el.get('placeholder','')}' "
                      f"text='{el.get_text(strip=True)[:30]}'")

            # ── Try to click Advanced Search ──────────────────────────────
            adv_clicked = False
            for sel in [
                "a:has-text('Advanced Search')",
                "text=Advanced Search",
                "a:has-text('Advanced')",
                "[href*='advanced' i]",
                "#advancedSearchTab",
                ".advanced-search",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_load_state("networkidle", timeout=10000)
                        await asyncio.sleep(2)
                        adv_clicked = True
                        print(f"  ✅ Advanced Search clicked via: {sel}")
                        break
                except Exception:
                    pass

            if not adv_clicked:
                print("  ℹ  No Advanced Search tab found — using Quick Search")

            # ── Search each document type ─────────────────────────────────
            for doc_type in doc_types:
                cat = DOC_TYPE_MAP.get(doc_type, doc_type)
                print(f"\n  🔍 Searching [{doc_type}] {cat}")
                api_hits.clear()

                recs = await search_doc_type(
                    page, doc_type, cat, start_mm, end_mm, start_iso
                )

                # Also grab any API responses captured from network
                for hit in api_hits:
                    body  = hit["body"]
                    items = (
                        body.get("hits") or body.get("data") or
                        body.get("results") or body.get("instruments") or
                        body.get("records") or
                        (body if isinstance(body, list) else [])
                    )
                    for row in items:
                        if isinstance(row, dict):
                            rec = parse_api_row(row, doc_type, cat)
                            if rec: recs.append(rec)

                print(f"     → {len(recs)} records for {doc_type}")
                all_recs.extend(recs)
                await asyncio.sleep(1)

        except Exception as e:
            print(f"\n  ✗ Playwright error: {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return all_recs


async def search_doc_type(page, doc_type, cat, start_mm, end_mm, start_iso):
    """Search for one document type and return records."""
    from playwright.async_api import TimeoutError as PWTimeout
    recs = []

    async def clear_fill(selectors, value):
        """Clear a field and type a value. Returns True on success."""
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Delete")
                    await el.fill(value)
                    return True
            except Exception:
                pass
        return False

    for attempt in range(MAX_RETRIES):
        try:
            # ── Fill the search / doc type field ─────────────────────────
            type_filled = await clear_fill([
                "input[placeholder*='grantor' i]",
                "input[placeholder*='grantee' i]",
                "input[placeholder*='search' i]",
                "input[placeholder*='doc type' i]",
                "input[placeholder*='type' i]",
                "#searchTerm",
                "input[name='searchTerm']",
                "input[name='term']",
                "input[type='search']",
                "input.search-input",
                "input.form-control[type='text']",
            ], doc_type)

            print(f"     Search field filled: {type_filled}")

            # ── Set date FROM ─────────────────────────────────────────────
            date_from_filled = await clear_fill([
                "input[id*='from' i]",
                "input[name*='from' i]",
                "input[placeholder*='from' i]",
                "input[id*='start' i]",
                "input[name*='start' i]",
                "#dateFrom", "#startDate", "#fromDate",
            ], start_mm)

            # ── Set date TO ───────────────────────────────────────────────
            date_to_filled = await clear_fill([
                "input[id*='to' i]",
                "input[name*='to' i]",
                "input[placeholder*='to' i]",
                "input[id*='end' i]",
                "input[name*='end' i]",
                "#dateTo", "#endDate", "#toDate",
            ], end_mm)

            print(f"     Dates filled — from: {date_from_filled} to: {date_to_filled}")

            # ── Submit the search ─────────────────────────────────────────
            submitted = False
            for sel in [
                "button:has-text('Search')",
                "button[type='submit']",
                "input[type='submit']",
                "#searchBtn", "#btnSearch",
                ".search-btn", ".btn-search",
                "button.btn-primary",
                "[aria-label='Search']",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        submitted = True
                        print(f"     Submitted via: {sel}")
                        break
                except Exception:
                    pass

            if not submitted:
                # Try pressing Enter in the search field
                await page.keyboard.press("Enter")
                submitted = True
                print("     Submitted via Enter key")

            # ── Wait for results to FULLY load ────────────────────────────
            # This is the critical fix — wait for loading spinner to disappear
            # AND wait for actual result rows to appear
            await asyncio.sleep(2)

            # Wait up to 15 seconds for results to appear
            for wait_attempt in range(15):
                html = await page.content()

                # Check if still loading
                if re.search(r"loading results|please wait|searching\.\.\.", html, re.I):
                    print(f"     ⏳ Still loading … ({wait_attempt+1}s)")
                    await asyncio.sleep(1)
                    continue

                # Try to detect results are present
                soup = BeautifulSoup(html, "lxml")
                rows = soup.find_all("tr")
                cards = soup.select(
                    ".instrument-row, .result-row, .search-result, "
                    ".record-item, [data-instrument], [data-id]"
                )
                if len(rows) > 2 or cards:
                    print(f"     ✅ Results loaded ({len(rows)} rows, {len(cards)} cards)")
                    break
                await asyncio.sleep(1)

            # ── Also wait for any Angular/React rendering ─────────────────
            try:
                await page.wait_for_selector(
                    "table tr:nth-child(2), .instrument-row, "
                    ".result-row, .search-result, [data-instrument]",
                    timeout=10000
                )
            except PWTimeout:
                print("     ⚠  No result elements found via wait_for_selector")

            await asyncio.sleep(1)

            # ── Parse the loaded page ─────────────────────────────────────
            html = await page.content()
            page_recs = parse_results_html(html, doc_type)
            recs.extend(page_recs)
            print(f"     Parsed {len(page_recs)} from page 1")

            # ── Paginate ──────────────────────────────────────────────────
            for pg in range(30):
                nxt = None
                for ns in [
                    "a[aria-label*='next' i]",
                    "button[aria-label*='next' i]",
                    "li.next:not(.disabled) a",
                    "a.next:not(.disabled)",
                    "a:has-text('Next'):not([disabled])",
                    "button:has-text('Next'):not([disabled])",
                    "[data-testid='pagination-next']",
                    "#nextPage:not([disabled])",
                ]:
                    try:
                        el = await page.query_selector(ns)
                        if el and await el.is_visible():
                            dis = await el.get_attribute("disabled")
                            ard = await el.get_attribute("aria-disabled")
                            cls = await el.get_attribute("class") or ""
                            if not dis and ard != "true" and "disabled" not in cls:
                                nxt = el
                                break
                    except Exception:
                        pass

                if not nxt:
                    break

                await nxt.click()
                await asyncio.sleep(2)

                # Wait for next page to load
                for _ in range(10):
                    h = await page.content()
                    if not re.search(r"loading results|please wait", h, re.I):
                        break
                    await asyncio.sleep(1)

                more = parse_results_html(await page.content(), doc_type)
                if not more:
                    break
                recs.extend(more)
                print(f"     Page {pg+2}: +{len(more)} records")

            # Filter to date window
            in_window = [r for r in recs
                         if not r.get("filed") or r["filed"] >= start_iso]
            return in_window

        except PWTimeout:
            if attempt < MAX_RETRIES - 1:
                print(f"     ↻ Timeout, retry {attempt+1}")
                await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"     ✗ {doc_type}: {e}")
            import traceback; traceback.print_exc()
            break

    return recs


def parse_api_row(row, doc_type, cat):
    """Parse a JSON API row into a record dict."""
    try:
        doc_num = (safe(row.get("instrumentNumber")) or
                   safe(row.get("docNumber")) or
                   safe(row.get("bookPage")) or
                   safe(row.get("id")) or "")
        if not doc_num:
            return None

        filed = to_iso(
            row.get("filedDate") or row.get("recordedDate") or
            row.get("dateRecorded") or row.get("date") or ""
        )

        def names(key):
            v = row.get(key) or []
            if isinstance(v, list):
                return "; ".join(
                    safe(g.get("name") or g.get("fullName") or str(g))
                    for g in v if g
                )
            return safe(v)

        inst = safe(row.get("id") or row.get("instrumentId") or doc_num)
        rec  = blank_record(doc_type)
        rec.update({
            "doc_num":   doc_num,
            "filed":     filed,
            "owner":     names("grantors"),
            "grantee":   names("grantees"),
            "amount":    parse_amt(row.get("consideration") or row.get("amount") or 0),
            "legal":     safe(row.get("legalDescription") or row.get("legal") or ""),
            "clerk_url": f"{CLERK_BASE}/doc/{inst}" if inst else CLERK_BASE,
        })
        return rec
    except Exception as e:
        print(f"    ⚠  API row: {e}")
        return None

# ── BCAD Parcel Lookup ────────────────────────────────────────────────────────
class ParcelLookup:
    URLS = [
        "https://www.bcad.org/clientdb/PropertyExport.zip",
        "https://www.bcad.org/Downloads/PropertyExport.zip",
    ]

    def __init__(self):
        self.idx = {}
        self._load()

    def _load(self):
        if not HAS_DBF:
            print("  ⚠  dbfread missing — skipping parcel lookup")
            return
        sess = make_session()
        raw  = None
        for url in self.URLS:
            print(f"  ↓ BCAD: {url}")
            try:
                r = sess.get(url, timeout=120, stream=True)
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
            print("  ⚠  BCAD unavailable")
            return

        try:
            zf   = zipfile.ZipFile(io.BytesIO(raw))
            dbfs = sorted(
                [n for n in zf.namelist() if n.lower().endswith(".dbf")],
                key=lambda n: zf.getinfo(n).file_size, reverse=True
            )
            if not dbfs:
                print("  ⚠  No DBF in ZIP")
                return
            print(f"  📄 Indexing {dbfs[0]} …")
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                self._index(os.path.join(tmp, dbfs[0]))
            print(f"  ✅ Parcel index: {len(self.idx):,} entries")
        except Exception as e:
            print(f"  ✗ Parcel ZIP: {e}")

    def _index(self, path):
        try:
            count = 0
            for row in DBF(path, ignore_missing_memofile=True, encoding="latin-1"):
                r = {k.upper(): safe(v) for k, v in row.items()}
                owner = (
                    r.get("OWNER") or r.get("OWN1") or
                    r.get("OWNERNAME") or r.get("NAME") or ""
                ).upper().strip()
                if not owner:
                    continue
                p = {
                    "prop_address": r.get("SITE_ADDR")  or r.get("SITEADDR")  or r.get("PROP_ADDR") or "",
                    "prop_city":    r.get("SITE_CITY")  or r.get("PROP_CITY") or "",
                    "prop_state":   r.get("SITE_STATE") or "TX",
                    "prop_zip":     r.get("SITE_ZIP")   or r.get("SITEZIP")   or r.get("PROP_ZIP") or "",
                    "mail_address": r.get("ADDR_1")     or r.get("MAILADR1")  or r.get("MAIL_ADDR") or r.get("ADDRESS") or "",
                    "mail_city":    r.get("CITY")       or r.get("MAILCITY")  or "",
                    "mail_state":   r.get("STATE")      or r.get("MAILSTATE") or "TX",
                    "mail_zip":     r.get("ZIP")        or r.get("MAILZIP")   or "",
                }
                for k in self._variants(owner):
                    self.idx.setdefault(k, p)
                count += 1
        except Exception as e:
            print(f"  ✗ DBF index: {e}")

    @staticmethod
    def _variants(n):
        n = re.sub(r"\s+", " ", n).strip().upper()
        v = {n}
        if "," in n:
            pts = [p.strip() for p in n.split(",", 1)]
            if len(pts) == 2:
                v.add(f"{pts[1]} {pts[0]}")
                v.add(pts[0])
                v.add(pts[1])
        else:
            pts = n.split()
            if len(pts) >= 2:
                v.add(f"{pts[-1]},{' '.join(pts[:-1])}")
                v.add(f"{pts[-1]} {' '.join(pts[:-1])}")
                v.add(pts[0])
                v.add(pts[-1])
        return [x for x in v if x]

    def lookup(self, owner):
        if not owner:
            return {}
        owner = re.sub(r"\s+", " ", owner).strip()
        for k in self._variants(owner.upper()):
            h = self.idx.get(k)
            if h:
                return h
        return {}

# ── Scoring ───────────────────────────────────────────────────────────────────
def score_record(r, cutoff):
    flags = []; sc = 30
    dt  = r.get("doc_type", "")
    a   = r.get("amount", 0.0)
    own = (r.get("owner") or "").upper()

    if dt in ("LP", "RELLP"):                flags.append("Lis pendens")
    if dt in ("NOFC", "TAXDEED"):            flags.append("Pre-foreclosure")
    if dt in ("JUD", "CCJ", "DRJUD"):        flags.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED"):   flags.append("Tax lien")
    if dt == "LNMECH":                        flags.append("Mechanic lien")
    if dt == "LNHOA":                         flags.append("HOA lien")
    if dt == "PRO":                           flags.append("Probate / estate")
    if any(k in own for k in ("LLC","INC","CORP","LP","LTD","TRUST")):
        flags.append("LLC / corp owner")
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: sc += 20
    if a > 100_000: sc += 15
    elif a > 50_000: sc += 10
    if r.get("filed", "") >= cutoff:
        flags.append("New this week"); sc += 5
    if r.get("prop_address"): sc += 5
    sc += 10 * len([f for f in flags if f != "New this week"])
    return flags, min(sc, 100)

# ── CSV Export ────────────────────────────────────────────────────────────────
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
            fn, ln = _split_name(r.get("owner", ""))
            w.writerow({
                "First Name":             fn,
                "Last Name":              ln,
                "Mailing Address":        r.get("mail_address", ""),
                "Mailing City":           r.get("mail_city", ""),
                "Mailing State":          r.get("mail_state", "TX"),
                "Mailing Zip":            r.get("mail_zip", ""),
                "Property Address":       r.get("prop_address", ""),
                "Property City":          r.get("prop_city", ""),
                "Property State":         r.get("prop_state", "TX"),
                "Property Zip":           r.get("prop_zip", ""),
                "Lead Type":              r.get("cat_label", ""),
                "Document Type":          r.get("doc_type", ""),
                "Date Filed":             r.get("filed", ""),
                "Document Number":        r.get("doc_num", ""),
                "Amount/Debt Owed":       r.get("amount", ""),
                "Seller Score":           r.get("score", 0),
                "Motivated Seller Flags": " | ".join(r.get("flags", [])),
                "Source":                 "Bexar County Clerk",
                "Public Records URL":     r.get("clerk_url", ""),
            })

def _split_name(full):
    if not full: return "", ""
    if "," in full:
        p = [x.strip().title() for x in full.split(",", 1)]
        return p[1], p[0]
    p = full.strip().title().split()
    if len(p) == 1: return "", p[0]
    return p[0], " ".join(p[1:])

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("  Bexar County Motivated Seller Scraper v5")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    # ── Parcel lookup ─────────────────────────────────────────────────────
    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    # ── Playwright scrape ─────────────────────────────────────────────────
    print(f"\n🏛  Scraping {len(DOC_TYPE_MAP)} document types …")
    all_recs = await playwright_scrape(
        list(DOC_TYPE_MAP.keys()), s_mm, e_mm, s_iso
    )

    # ── Dedup ─────────────────────────────────────────────────────────────
    seen = set(); unique = []
    for r in all_recs:
        # Key on doc_num + doc_type to prevent cross-type duplicates
        k = f"{r.get('doc_num','').strip()}|{r.get('doc_type','')}"
        if k and k != "|" and k not in seen:
            seen.add(k); unique.append(r)

    # Remove any record where doc_num looks like page text
    unique = [
        r for r in unique
        if r.get("doc_num") and
        not re.search(r"loading|please wait|searching|results for",
                      r.get("doc_num",""), re.I)
    ]

    # ── Date filter ───────────────────────────────────────────────────────
    in_window = []
    for r in unique:
        fd = r.get("filed", "")
        if not fd or fd >= s_iso:
            in_window.append(r)

    print(f"\n✅ Unique: {len(unique)}  |  In window: {len(in_window)}")
    unique = in_window

    # ── Enrich with parcel addresses ──────────────────────────────────────
    with_addr = 0
    for r in unique:
        owner = r.get("owner", "")
        hit   = parcel.lookup(owner) if owner else {}
        if hit:
            r.update(hit)
            with_addr += 1
        r["flags"], r["score"] = score_record(r, s_iso)

    unique.sort(key=lambda x: x["score"], reverse=True)
    print(f"   With address: {with_addr}")

    # ── Save JSON ─────────────────────────────────────────────────────────
    payload = {
        "fetched_at":   datetime.utcnow().isoformat() + "Z",
        "source":       "Bexar County Clerk / BCAD",
        "data_range":   f"{s_iso} to {e_iso}",
        "total":        len(unique),
        "with_address": with_addr,
        "records":      unique,
    }

    for dest in [DASH_DIR / "records.json", DATA_DIR / "records.json"]:
        dest.write_text(json.dumps(payload, indent=2, default=str))
        print(f"💾 {dest}")

    export_csv(unique, DATA_DIR / "leads.csv")
    print(f"📊 {DATA_DIR / 'leads.csv'}")
    print(f"\n🎉 Done — {len(unique)} leads | {with_addr} with address.\n")


if __name__ == "__main__":
    asyncio.run(main())
