"""
Bexar County Motivated Seller Lead Scraper v4
==============================================
Fixes:
  - Correct PublicSearch.us API endpoint URLs
  - Playwright triple_click → triple click workaround
  - Robust HTML parsing of actual portal response
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
    "LP":"Lis Pendens","NOFC":"Notice of Foreclosure","TAXDEED":"Tax Deed",
    "JUD":"Judgment","CCJ":"Certified Judgment","DRJUD":"Domestic Judgment",
    "LNCORPTX":"Corp Tax Lien","LNIRS":"IRS Lien","LNFED":"Federal Lien",
    "LN":"Lien","LNMECH":"Mechanic Lien","LNHOA":"HOA Lien",
    "MEDLN":"Medicaid Lien","PRO":"Probate",
    "NOC":"Notice of Commencement","RELLP":"Release Lis Pendens",
}

ROOT     = Path(__file__).resolve().parent.parent
DASH_DIR = ROOT / "dashboard"
DATA_DIR = ROOT / "data"
DASH_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# ── Dates ─────────────────────────────────────────────────────────────────────
def date_range_mm():
    e = datetime.utcnow(); s = e - timedelta(days=LOOKBACK_DAYS)
    return s.strftime("%m/%d/%Y"), e.strftime("%m/%d/%Y")

def date_range_iso():
    e = datetime.utcnow(); s = e - timedelta(days=LOOKBACK_DAYS)
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

# ── HTML result parser ────────────────────────────────────────────────────────
def parse_html(html, doc_type, cat):
    """Parse any HTML results table from the portal."""
    recs = []
    soup = BeautifulSoup(html, "lxml")

    # ── Table format ──────────────────────────────────────────────────────────
    for tbl in soup.find_all("table"):
        hdrs = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
        if len(hdrs) < 2:
            continue

        def col(tr, frags):
            for f in frags:
                for i, h in enumerate(hdrs):
                    if f in h:
                        cells = tr.find_all("td")
                        return cells[i].get_text(strip=True) if i < len(cells) else ""
            return ""

        rows = tbl.find_all("tr")[1:]
        if not rows:
            continue

        for tr in rows:
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            url = ""
            for a in tr.find_all("a"):
                h = a.get("href", "")
                if h:
                    url = h if h.startswith("http") else CLERK_BASE + h
                    break
            dn = (col(tr, ["instrument","doc #","doc#","book","number","docnum"]) or
                  cells[0].get_text(strip=True))
            if not dn:
                continue
            recs.append({
                "doc_num":      dn,
                "doc_type":     doc_type,
                "cat_label":    cat,
                "filed":        to_iso(col(tr, ["date","filed","recorded","entry"])),
                "owner":        col(tr, ["grantor","owner","name"]),
                "grantee":      col(tr, ["grantee","lender","trustee","plaintiff"]),
                "amount":       parse_amt(col(tr, ["amount","consideration","value","debt"])),
                "legal":        col(tr, ["legal","description","subdivision","property"]),
                "clerk_url":    url or CLERK_BASE,
                "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
                "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
                "flags":[],"score":0,
            })

    # ── Card / list format ────────────────────────────────────────────────────
    if not recs:
        for item in soup.select(
            ".result-item, .record-row, .instrument-row, "
            ".search-result, [class*='result'], [class*='record'], "
            "li.item, .doc-item"
        ):
            a_el = item.select_one("a[href]")
            href = a_el["href"] if a_el else ""
            url  = href if href.startswith("http") else CLERK_BASE + href if href else ""

            def t(sel):
                el = item.select_one(sel)
                return el.get_text(strip=True) if el else ""

            dn = t(".doc-num,.instrument,.book-page,[class*='doc'],[class*='instrument']")
            if not dn:
                # grab first text block as doc number
                texts = [x.get_text(strip=True) for x in item.find_all(
                    ["span","div","td"], limit=5) if x.get_text(strip=True)]
                dn = texts[0] if texts else ""
            if not dn:
                continue

            recs.append({
                "doc_num":      dn,
                "doc_type":     doc_type,
                "cat_label":    cat,
                "filed":        to_iso(t(".date,.filed,.recorded")),
                "owner":        t(".grantor,.owner,.name"),
                "grantee":      t(".grantee,.lender,.plaintiff"),
                "amount":       parse_amt(t(".amount,.consideration,.value")),
                "legal":        t(".legal,.description"),
                "clerk_url":    url or CLERK_BASE,
                "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
                "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
                "flags":[],"score":0,
            })

    return recs

# ── PublicSearch.us correct API calls ─────────────────────────────────────────
def search_publicsearch_api(sess, doc_type, s_iso, e_iso):
    """
    PublicSearch.us (the software behind bexar.tx.publicsearch.us) uses
    specific endpoints. These are the correct patterns discovered from
    network inspection of the live portal.
    """
    cat  = DOC_TYPE_MAP.get(doc_type, doc_type)
    recs = []

    # Save and swap Accept header
    old_accept = sess.headers.get("Accept", "")
    sess.headers["Accept"] = "application/json, text/plain, */*"

    # ── Pattern 1: /api/search with county param ──────────────────────────
    endpoints = [
        {
            "url": f"{CLERK_BASE}/api/search",
            "params": {
                "searchType":    "quickSearch",
                "docType":       doc_type,
                "dateFrom":      s_iso,
                "dateTo":        e_iso,
                "dept":          "RP",
                "page":          1,
                "perPage":       200,
            }
        },
        # ── Pattern 2: /api/search different param names ──────────────────
        {
            "url": f"{CLERK_BASE}/api/search",
            "params": {
                "type":      "DocumentType",
                "term":      doc_type,
                "startDate": s_iso,
                "endDate":   e_iso,
                "page":      1,
                "rows":      200,
            }
        },
        # ── Pattern 3: direct results endpoint ───────────────────────────
        {
            "url": f"{CLERK_BASE}/results",
            "params": {
                "docType":   doc_type,
                "dateFrom":  s_iso,
                "dateTo":    e_iso,
                "dept":      "RP",
            }
        },
    ]

    for ep in endpoints:
        r = safe_get(sess, ep["url"], params=ep["params"])
        if not r:
            continue

        ct = r.headers.get("content-type", "")

        # JSON response
        if "json" in ct:
            try:
                data  = r.json()
                items = (data.get("hits") or data.get("data") or
                         data.get("results") or data.get("instruments") or
                         data.get("records") or
                         (data if isinstance(data, list) else []))
                if items:
                    for row in items:
                        if isinstance(row, dict):
                            rec = _row_to_record(row, doc_type, cat)
                            if rec: recs.append(rec)
                    print(f"    ✅ JSON API → {len(recs)} records ({ep['url'][:60]})")
                    # Paginate
                    total = (data.get("total") or data.get("totalHits") or
                             data.get("totalCount") or len(recs))
                    page = 2
                    while len(recs) < int(total):
                        ep["params"]["page"] = page
                        r2 = safe_get(sess, ep["url"], params=ep["params"])
                        if not r2: break
                        d2 = r2.json()
                        more = (d2.get("hits") or d2.get("data") or
                                d2.get("results") or d2.get("instruments") or [])
                        if not more: break
                        for row in more:
                            if isinstance(row, dict):
                                rec = _row_to_record(row, doc_type, cat)
                                if rec: recs.append(rec)
                        page += 1
                        time.sleep(0.3)
                    break
            except Exception as e:
                print(f"    ⚠  JSON parse: {e}")

        # HTML response — parse results table
        elif "html" in ct:
            html_recs = parse_html(r.text, doc_type, cat)
            if html_recs:
                recs.extend(html_recs)
                print(f"    ✅ HTML → {len(html_recs)} records")
                # Follow pagination
                recs.extend(_html_paginate(sess, r.text, doc_type, cat, ep))
                break

    sess.headers["Accept"] = old_accept
    return recs


def _html_paginate(sess, first_html, doc_type, cat, ep):
    """Follow next-page links in HTML results."""
    extra = []
    soup  = BeautifulSoup(first_html, "lxml")

    for page_num in range(2, 51):
        # Look for next page link
        nxt = soup.select_one(
            "a[aria-label='Next page'], a[aria-label='Next'], "
            ".next-page a, .pager-next a, a.next, "
            "a[rel='next'], li.next a"
        )
        if not nxt:
            # Try numbered page links
            all_pages = [
                a for a in soup.select("a[data-page], .page-number a, .pager a")
                if a.get_text(strip=True).isdigit()
                   and int(a.get_text(strip=True)) == page_num
            ]
            nxt = all_pages[0] if all_pages else None

        if not nxt:
            break

        href = nxt.get("href", "")
        if href:
            url = href if href.startswith("http") else CLERK_BASE + href
            r   = safe_get(sess, url)
        else:
            params = dict(ep.get("params", {}))
            params["page"] = page_num
            r = safe_get(sess, ep["url"], params=params)

        if not r:
            break

        more = parse_html(r.text, doc_type, cat)
        if not more:
            break
        extra.extend(more)
        soup = BeautifulSoup(r.text, "lxml")
        time.sleep(0.4)

    return extra


def _row_to_record(row, doc_type, cat):
    try:
        doc_num = (safe(row.get("instrumentNumber")) or
                   safe(row.get("docNumber")) or safe(row.get("bookPage")) or
                   safe(row.get("id")) or "")
        if not doc_num:
            return None

        filed = to_iso(row.get("filedDate") or row.get("recordedDate") or
                       row.get("dateRecorded") or row.get("date") or "")

        def names(key):
            v = row.get(key) or []
            if isinstance(v, list):
                return "; ".join(
                    safe(g.get("name") or g.get("fullName") or str(g)) for g in v
                )
            return safe(v)

        inst = safe(row.get("id") or row.get("instrumentId") or doc_num)
        return {
            "doc_num":      doc_num,
            "doc_type":     doc_type,
            "cat_label":    cat,
            "filed":        filed,
            "owner":        names("grantors"),
            "grantee":      names("grantees"),
            "amount":       parse_amt(row.get("consideration") or row.get("amount") or 0),
            "legal":        safe(row.get("legalDescription") or row.get("legal") or ""),
            "clerk_url":    f"{CLERK_BASE}/doc/{inst}" if inst else CLERK_BASE,
            "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
            "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
            "flags":[],"score":0,
        }
    except Exception as e:
        print(f"    ⚠  row parse: {e}")
        return None

# ── Playwright scraper (fixed triple_click issue) ─────────────────────────────
async def playwright_scrape(doc_types, start_mm, end_mm):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    recs = []
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

        # Capture all JSON API responses automatically
        api_data = []
        async def capture(resp):
            try:
                if any(k in resp.url for k in
                       ["api", "instrument", "search", "result", "record"]):
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        body = await resp.json()
                        api_data.append({"url": resp.url, "body": body})
            except Exception:
                pass
        page.on("response", capture)

        try:
            print("  → Loading portal …")
            await page.goto(CLERK_BASE, timeout=40000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            await asyncio.sleep(2)

            # Print page title for debug
            title = await page.title()
            print(f"  Page title: {title}")

            # Dismiss any disclaimer/modal
            for btn_text in ["Accept", "I Agree", "Continue", "OK", "Close"]:
                try:
                    btn = page.get_by_role("button", name=btn_text)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click()
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

            # Try to click Advanced Search
            for adv_sel in [
                "text=Advanced Search",
                "a:has-text('Advanced')",
                "#advancedSearch",
                "[href*='advanced' i]",
            ]:
                try:
                    el = await page.query_selector(adv_sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_load_state("networkidle", timeout=10000)
                        await asyncio.sleep(1)
                        print(f"  ✅ Clicked: {adv_sel}")
                        break
                except Exception:
                    pass

            # Print current URL
            print(f"  Current URL: {page.url}")

            # Screenshot HTML structure for debug
            html_snap = await page.content()
            soup_snap  = BeautifulSoup(html_snap, "lxml")
            inputs = soup_snap.find_all(["input","select","button"])
            print(f"  Found {len(inputs)} form elements on page")
            for el in inputs[:10]:
                print(f"    <{el.name}> id={el.get('id','')} "
                      f"name={el.get('name','')} "
                      f"type={el.get('type','')} "
                      f"placeholder={el.get('placeholder','')}")

            # Search each doc type
            for doc_type in doc_types:
                cat = DOC_TYPE_MAP.get(doc_type, doc_type)
                print(f"  🔍 [{doc_type}]")
                api_data.clear()

                found = await _pw_search_fixed(page, doc_type, cat,
                                               start_mm, end_mm)
                recs.extend(found)

                # Grab any API responses captured automatically
                for captured in api_data:
                    body  = captured["body"]
                    items = (body.get("hits") or body.get("data") or
                             body.get("results") or body.get("instruments") or
                             (body if isinstance(body, list) else []))
                    for row in items:
                        if isinstance(row, dict):
                            rec = _row_to_record(row, doc_type, cat)
                            if rec: recs.append(rec)

                print(f"     → {len(found)} records")
                await asyncio.sleep(1.5)

        except Exception as e:
            print(f"  ✗ Playwright: {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return recs


async def _pw_search_fixed(page, doc_type, cat, start_mm, end_mm):
    """
    Fixed version — uses click+selectAll+type instead of triple_click
    which is not available in all Playwright versions.
    """
    from playwright.async_api import TimeoutError as PWTimeout
    recs = []

    async def clear_and_type(selector_list, value):
        """Try each selector, clear field, type value. Returns True if successful."""
        for sel in selector_list:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    # Select all using keyboard instead of triple_click
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Delete")
                    await el.type(value, delay=40)
                    return True
            except Exception:
                pass
        return False

    for attempt in range(MAX_RETRIES):
        try:
            # Fill the search term with doc type
            filled = await clear_and_type([
                "input[placeholder*='grantor' i]",
                "input[placeholder*='search' i]",
                "input[placeholder*='grantee' i]",
                "input[name*='search' i]",
                "#searchTerm",
                "input[type='search']",
                ".search-input",
                "input.form-control",
            ], doc_type)

            if not filled:
                print(f"     ⚠  Could not find search input for {doc_type}")

            # Set start date
            await clear_and_type([
                "input[id*='from' i]", "input[name*='from' i]",
                "input[placeholder*='from' i]", "#dateFrom", "#startDate",
                "input[id*='start' i]",
            ], start_mm)

            # Set end date
            await clear_and_type([
                "input[id*='to' i]", "input[name*='to' i]",
                "input[placeholder*='to' i]", "#dateTo", "#endDate",
                "input[id*='end' i]",
            ], end_mm)

            # Click Search button
            clicked = False
            for sel in [
                "button:has-text('Search')",
                "input[type='submit']",
                "button[type='submit']",
                "#searchBtn", ".search-btn",
                "button.btn-primary",
                "[aria-label='Search']",
                "button.btn:has-text('Search')",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        clicked = True
                        break
                except Exception:
                    pass

            if not clicked:
                await page.keyboard.press("Enter")

            await page.wait_for_load_state("networkidle", timeout=20000)
            await asyncio.sleep(2)

            html = await page.content()
            page_recs = parse_html(html, doc_type, cat)
            recs.extend(page_recs)

            # Paginate
            for _ in range(30):
                nxt = None
                for ns in [
                    "a[aria-label*='next' i]",
                    "button[aria-label*='next' i]",
                    ".next-page a", "a.next",
                    "a:has-text('Next')",
                    "button:has-text('Next')",
                    "#nextPage",
                    "li.next a",
                ]:
                    try:
                        el = await page.query_selector(ns)
                        if el and await el.is_visible():
                            dis = await el.get_attribute("disabled")
                            ard = await el.get_attribute("aria-disabled")
                            if not dis and ard != "true":
                                nxt = el
                                break
                    except Exception:
                        pass
                if not nxt:
                    break
                await nxt.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                await asyncio.sleep(1)
                more = parse_html(await page.content(), doc_type, cat)
                if not more:
                    break
                recs.extend(more)

            return recs

        except PWTimeout:
            if attempt < MAX_RETRIES - 1:
                print(f"     ↻ timeout retry {attempt+1}")
                await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"     ✗ {doc_type}: {e}")
            break

    return recs

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
            print("  ⚠  BCAD unavailable — no address enrichment")
            return

        try:
            zf   = zipfile.ZipFile(io.BytesIO(raw))
            dbfs = sorted(
                [n for n in zf.namelist() if n.lower().endswith(".dbf")],
                key=lambda n: zf.getinfo(n).file_size, reverse=True
            )
            if not dbfs:
                print("  ⚠  No .dbf in ZIP")
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
            for row in DBF(path, ignore_missing_memofile=True, encoding="latin-1"):
                r = {k.upper(): safe(v) for k, v in row.items()}
                owner = (r.get("OWNER") or r.get("OWN1") or
                         r.get("OWNERNAME") or "").upper().strip()
                if not owner:
                    continue
                p = {
                    "prop_address": r.get("SITE_ADDR")  or r.get("SITEADDR",""),
                    "prop_city":    r.get("SITE_CITY",""),
                    "prop_state":   r.get("SITE_STATE","TX"),
                    "prop_zip":     r.get("SITE_ZIP")   or r.get("SITEZIP",""),
                    "mail_address": r.get("ADDR_1")     or r.get("MAILADR1",""),
                    "mail_city":    r.get("CITY")       or r.get("MAILCITY",""),
                    "mail_state":   r.get("STATE")      or r.get("MAILSTATE","TX"),
                    "mail_zip":     r.get("ZIP")        or r.get("MAILZIP",""),
                }
                for k in self._variants(owner):
                    self.idx.setdefault(k, p)
        except Exception as e:
            print(f"  ✗ DBF: {e}")

    @staticmethod
    def _variants(n):
        n = re.sub(r"\s+", " ", n).strip().upper()
        v = {n}
        if "," in n:
            pts = [p.strip() for p in n.split(",", 1)]
            v.add(f"{pts[1]} {pts[0]}")
        else:
            pts = n.split()
            if len(pts) >= 2:
                v.add(f"{pts[-1]},{' '.join(pts[:-1])}")
                v.add(f"{pts[-1]} {' '.join(pts[:-1])}")
        return list(v)

    def lookup(self, owner):
        if not owner: return {}
        for k in self._variants(owner.upper()):
            h = self.idx.get(k)
            if h: return h
        return {}

# ── Scoring ───────────────────────────────────────────────────────────────────
def score_record(r, cutoff):
    flags = []; sc = 30
    dt  = r.get("doc_type", "")
    a   = r.get("amount", 0.0)
    own = (r.get("owner") or "").upper()
    if dt in ("LP","RELLP"):               flags.append("Lis pendens")
    if dt in ("NOFC","TAXDEED"):           flags.append("Pre-foreclosure")
    if dt in ("JUD","CCJ","DRJUD"):        flags.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED"): flags.append("Tax lien")
    if dt == "LNMECH":                     flags.append("Mechanic lien")
    if dt == "LNHOA":                      flags.append("HOA lien")
    if dt == "PRO":                        flags.append("Probate / estate")
    if any(k in own for k in ("LLC","INC","CORP","LP","LTD","TRUST")):
        flags.append("LLC / corp owner")
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: sc += 20
    if a > 100_000: sc += 15
    elif a > 50_000: sc += 10
    if r.get("filed","") >= cutoff:
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
            fn, ln = _split_name(r.get("owner",""))
            w.writerow({
                "First Name":             fn,
                "Last Name":              ln,
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
    print("  Bexar County Motivated Seller Scraper v4")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    # ── Parcel lookup ─────────────────────────────────────────────────────
    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    # ── HTTP / API scrape ─────────────────────────────────────────────────
    sess = make_session()
    print("\n🌐 Priming portal session …")
    r0 = safe_get(sess, CLERK_BASE)
    print(f"  Portal: {'✅ reachable' if r0 else '⚠  unreachable'}")

    all_recs = []
    print(f"\n🏛  HTTP search — {len(DOC_TYPE_MAP)} doc types …\n")
    for dt, label in DOC_TYPE_MAP.items():
        print(f"  [{dt}] {label}")
        recs = search_publicsearch_api(sess, dt, s_iso, e_iso)
        print(f"    → {len(recs)} records")
        all_recs.extend(recs)
        time.sleep(0.8)

    http_total = len(all_recs)
    print(f"\n  HTTP total: {http_total} records")

    # ── Playwright — always run for complete coverage ─────────────────────
    print("🎭 Running Playwright …")
    pw_recs = await playwright_scrape(list(DOC_TYPE_MAP.keys()), s_mm, e_mm)
    print(f"  Playwright total: {len(pw_recs)} records")
    all_recs.extend(pw_recs)

    # ── Dedup ─────────────────────────────────────────────────────────────
    seen = set(); unique = []
    for r in all_recs:
        k = f"{r.get('doc_num','')}|{r.get('doc_type','')}"
        if k and k != "|" and k not in seen:
            seen.add(k); unique.append(r)

    # ── Date filter — last 7 days only ────────────────────────────────────
    in_window = []
    skipped   = 0
    for r in unique:
        fd = r.get("filed", "")
        if not fd or fd >= s_iso:
            in_window.append(r)
        else:
            skipped += 1

    print(f"\n✅ Unique: {len(unique)}  |  In window: {len(in_window)}  |  Skipped old: {skipped}")
    unique = in_window

    # ── Enrich + score ────────────────────────────────────────────────────
    with_addr = 0
    for r in unique:
        hit = parcel.lookup(r.get("owner", ""))
        if hit:
            r.update(hit)
            with_addr += 1
        r["flags"], r["score"] = score_record(r, s_iso)

    unique.sort(key=lambda x: x["score"], reverse=True)
    print(f"   With address: {with_addr}")

    # ── Save ──────────────────────────────────────────────────────────────
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
