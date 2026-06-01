"""
Bexar County Motivated Seller Lead Scraper v6
==============================================
Uses EXACT selectors discovered from live portal inspection:
  - .basicSearchInputBox  — main search input
  - .react-datepicker__input — date from/to fields
  - #date-range-select — recorded date dropdown
  - Advanced Search tab for doc-type filtering
  - Waits for React/Angular rendering before parsing
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

def blank_rec(doc_type):
    return {
        "doc_num":"","doc_type":doc_type,
        "cat_label":DOC_TYPE_MAP.get(doc_type,doc_type),
        "filed":"","owner":"","grantee":"",
        "amount":0.0,"legal":"","clerk_url":CLERK_BASE,
        "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
        "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
        "flags":[],"score":0,
    }

# ── HTTP session ──────────────────────────────────────────────────────────────
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0.0.0 Safari/537.36",
        "Accept":"text/html,*/*;q=0.9",
        "Accept-Language":"en-US,en;q=0.9",
    })
    return s

def safe_get(sess, url, **kw):
    for i in range(MAX_RETRIES):
        try:
            r = sess.get(url, timeout=30, **kw); r.raise_for_status(); return r
        except Exception as e:
            if i < MAX_RETRIES-1: time.sleep(RETRY_DELAY)
            else: print(f"    ✗ {url[:70]}: {e}")
    return None

# ── Parse fully-loaded HTML results ──────────────────────────────────────────
def parse_results(html, doc_type):
    """
    Parse the PublicSearch.us results page.
    The portal renders a React app — results appear in:
      - A <table> with rows
      - OR Angular/React card divs
    """
    cat  = DOC_TYPE_MAP.get(doc_type, doc_type)
    recs = []
    soup = BeautifulSoup(html, "lxml")

    # Skip loading pages
    if re.search(r"loading results|please wait|searching\.\.\.", html, re.I):
        return []

    # ── TABLE rows ────────────────────────────────────────────────────────
    for tbl in soup.find_all("table"):
        hdrs = [th.get_text(" ", strip=True).lower()
                for th in tbl.find_all("th")]
        if len(hdrs) < 2:
            continue
        print(f"    📋 Table headers: {hdrs}")

        def col(tr, *frags):
            cells = tr.find_all("td")
            for frag in frags:
                for i, h in enumerate(hdrs):
                    if frag in h and i < len(cells):
                        t = cells[i].get_text(" ", strip=True)
                        if t and t not in ("-","—"): return t
            return ""

        for tr in tbl.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if len(cells) < 2: continue

            # URL from any link
            doc_url = ""
            for a in tr.find_all("a"):
                h = a.get("href","")
                if h:
                    doc_url = h if h.startswith("http") else CLERK_BASE+h
                    break

            dn = (col(tr,"instrument","doc #","doc#","book","number","docnum") or
                  cells[0].get_text(" ",strip=True))
            if not dn or re.search(r"loading|please|searching",dn,re.I):
                continue

            # Get the doc type from the row if available
            # (Quick Search returns mixed types)
            row_type = col(tr,"type","doc type","document type")
            if row_type:
                # Try to match to our doc type codes
                matched = _match_doc_type(row_type)
                use_type = matched if matched else doc_type
            else:
                use_type = doc_type

            r = blank_rec(use_type)
            r.update({
                "doc_num":   dn,
                "filed":     to_iso(col(tr,"date","filed","recorded","entry")),
                "owner":     col(tr,"grantor","owner","seller","debtor","name"),
                "grantee":   col(tr,"grantee","lender","trustee","plaintiff","creditor"),
                "amount":    parse_amt(col(tr,"amount","consideration","value","debt")),
                "legal":     col(tr,"legal","description","subdivision","property"),
                "clerk_url": doc_url or CLERK_BASE,
            })
            recs.append(r)

    if recs:
        print(f"    ✅ Table parse: {len(recs)} rows")
        return recs

    # ── React/Angular rendered rows ───────────────────────────────────────
    # PublicSearch.us renders rows as <tr> inside a React virtual table
    # Try to find any rows that have document data
    all_rows = soup.find_all("tr")
    print(f"    ℹ  Total <tr> elements: {len(all_rows)}")

    for tr in all_rows:
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue

        texts = [c.get_text(" ", strip=True) for c in cells]
        print(f"    Row texts: {texts[:5]}")  # Debug first 5 cells

        # Skip header-like rows
        if any(t.lower() in ("instrument","grantor","date","type","book")
               for t in texts[:2]):
            continue

        doc_url = ""
        for a in tr.find_all("a"):
            h = a.get("href","")
            if h:
                doc_url = h if h.startswith("http") else CLERK_BASE+h
                break

        # First cell is usually doc number or date
        dn = texts[0] if texts else ""
        if not dn or re.search(r"loading|please|searching",dn,re.I):
            continue

        # Try to identify which cell is which
        # PublicSearch.us typical column order:
        # Instrument# | Type | Grantor | Grantee | Date | Legal | Book/Page
        owner   = texts[2] if len(texts) > 2 else ""
        grantee = texts[3] if len(texts) > 3 else ""
        filed   = to_iso(texts[4]) if len(texts) > 4 else ""
        legal   = texts[5] if len(texts) > 5 else ""

        # Check if col 1 looks like a doc type
        row_type_text = texts[1] if len(texts) > 1 else ""
        matched_type  = _match_doc_type(row_type_text)
        use_type      = matched_type if matched_type else doc_type

        r = blank_rec(use_type)
        r.update({
            "doc_num":   dn,
            "filed":     filed,
            "owner":     owner,
            "grantee":   grantee,
            "legal":     legal,
            "clerk_url": doc_url or CLERK_BASE,
        })
        recs.append(r)

    if recs:
        print(f"    ✅ Row parse: {len(recs)} rows")

    return recs


def _match_doc_type(text):
    """Try to match a text string to a doc type code."""
    if not text: return None
    text_up = text.upper().strip()
    # Direct match
    if text_up in DOC_TYPE_MAP:
        return text_up
    # Partial match against labels
    for code, label in DOC_TYPE_MAP.items():
        if code in text_up or label.upper() in text_up:
            return code
    return None


# ── Playwright scraper ────────────────────────────────────────────────────────
async def playwright_scrape(doc_types, start_mm, end_mm, start_iso):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    all_recs = []
    print("\n🎭 Playwright scrape …")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900},
        )
        page = await ctx.new_page()

        # Capture JSON API calls
        api_hits = []
        async def capture(resp):
            try:
                ct = resp.headers.get("content-type","")
                if "json" in ct and any(
                    k in resp.url for k in
                    ["api","instrument","search","result","record","query","hits"]
                ):
                    body = await resp.json()
                    api_hits.append({"url":resp.url,"body":body})
                    print(f"    📡 {resp.url[:90]}")
            except Exception: pass
        page.on("response", capture)

        try:
            # ── Load portal ───────────────────────────────────────────────
            print("  → Loading portal …")
            await page.goto(CLERK_BASE, timeout=45000)
            await page.wait_for_load_state("networkidle", timeout=25000)
            await asyncio.sleep(3)

            # Dismiss any dialog
            for txt in ["Close","Accept","I Agree","Continue","OK"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(txt, re.I))
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click()
                        await asyncio.sleep(1)
                        print(f"  ✅ Dismissed: {txt}")
                        break
                except Exception: pass

            # ── Set the date range FIRST (applies to all searches) ────────
            # The portal has TWO react-datepicker__input fields
            # First = date from, Second = date to
            print(f"  📅 Setting date range: {start_mm} → {end_mm}")
            date_inputs = await page.query_selector_all(".react-datepicker__input")
            print(f"  Found {len(date_inputs)} date inputs")

            if len(date_inputs) >= 1:
                await _fill_react_date(page, date_inputs[0], start_mm)
                print(f"  ✅ Date FROM set: {start_mm}")
            if len(date_inputs) >= 2:
                await _fill_react_date(page, date_inputs[1], end_mm)
                print(f"  ✅ Date TO set: {end_mm}")

            await asyncio.sleep(1)

            # ── Try to use Advanced Search for doc-type filtering ─────────
            # From log line 50: Advanced Search WAS clicked successfully
            adv_clicked = False
            for sel in [
                "a:has-text('Advanced Search')",
                "text=Advanced Search",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_load_state("networkidle", timeout=10000)
                        await asyncio.sleep(2)
                        adv_clicked = True
                        print(f"  ✅ Advanced Search active")
                        break
                except Exception: pass

            # Print Advanced Search form elements
            html_adv = await page.content()
            soup_adv = BeautifulSoup(html_adv, "lxml")
            adv_inputs = soup_adv.find_all(["input","select","textarea"])
            print(f"\n  Advanced Search form elements ({len(adv_inputs)}):")
            for el in adv_inputs[:25]:
                print(f"    <{el.name}> "
                      f"id='{el.get('id','')}' "
                      f"name='{el.get('name','')}' "
                      f"class='{' '.join(el.get('class',[])[:2])}' "
                      f"type='{el.get('type','')}' "
                      f"placeholder='{el.get('placeholder','')}' "
                      f"value='{el.get('value','')}'")

            # Also print select options to find doc type dropdown
            selects = soup_adv.find_all("select")
            for sel_el in selects:
                opts = [o.get("value","") for o in sel_el.find_all("option")]
                print(f"  SELECT id='{sel_el.get('id','')}' "
                      f"options={opts[:10]}")

            # Check for React-Select (id='react-select-...-input' from log line 39)
            react_selects = soup_adv.find_all(
                "input", id=re.compile(r"react-select")
            )
            print(f"\n  React-Select inputs: {len(react_selects)}")
            for rs in react_selects:
                print(f"    id='{rs.get('id','')}' "
                      f"class='{' '.join(rs.get('class',[])[:2])}'")

            # ── Search each doc type ──────────────────────────────────────
            for doc_type in doc_types:
                cat = DOC_TYPE_MAP.get(doc_type, doc_type)
                print(f"\n  🔍 [{doc_type}] {cat}")
                api_hits.clear()

                recs = await search_one_type(
                    page, doc_type, cat,
                    start_mm, end_mm, start_iso,
                    date_inputs
                )

                # Grab API-captured records
                for hit in api_hits:
                    body  = hit["body"]
                    items = (body.get("hits") or body.get("data") or
                             body.get("results") or body.get("instruments") or
                             body.get("records") or
                             (body if isinstance(body,list) else []))
                    for row in items:
                        if isinstance(row,dict):
                            rec = parse_api_row(row, doc_type, cat)
                            if rec: recs.append(rec)

                print(f"     → {len(recs)} records")
                all_recs.extend(recs)
                await asyncio.sleep(1)

        except Exception as e:
            print(f"\n  ✗ {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return all_recs


async def _fill_react_date(page, el, date_str):
    """
    Fill a React datepicker input.
    These inputs need special handling — click, select all, type.
    """
    try:
        await el.click()
        await asyncio.sleep(0.3)
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Delete")
        # Type each character slowly so React registers it
        await el.type(date_str, delay=80)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.5)
        return True
    except Exception as e:
        print(f"    ⚠  date fill: {e}")
        return False


async def search_one_type(page, doc_type, cat,
                          start_mm, end_mm, start_iso,
                          date_inputs):
    """
    Search for a specific document type.
    Strategy:
    1. Try to use the React-Select doc type dropdown in Advanced Search
    2. Fall back to typing the doc type in the main search box
    """
    from playwright.async_api import TimeoutError as PWTimeout
    recs = []

    try:
        # ── STRATEGY 1: Use Advanced Search doc type selector ─────────────
        # From log line 39: id='react-select-24315-input'
        # This is a React-Select component for filtering by document type
        adv_used = False

        # Try to select doc type from React-Select dropdown
        react_sel_input = await page.query_selector(
            "input[id*='react-select'][id*='input']"
        )
        if react_sel_input and await react_sel_input.is_visible():
            try:
                await react_sel_input.click()
                await asyncio.sleep(0.5)
                await react_sel_input.fill(doc_type)
                await asyncio.sleep(0.8)

                # Look for dropdown option matching doc_type
                label = DOC_TYPE_MAP.get(doc_type, doc_type)
                for opt_sel in [
                    f"[class*='option']:has-text('{doc_type}')",
                    f"[class*='option']:has-text('{label}')",
                    f"[role='option']:has-text('{doc_type}')",
                    f".react-select__option:has-text('{doc_type}')",
                    f"div:has-text('{doc_type}'):not(script):not(style)",
                ]:
                    try:
                        opt = await page.query_selector(opt_sel)
                        if opt and await opt.is_visible():
                            await opt.click()
                            adv_used = True
                            print(f"     ✅ Doc type selected from dropdown")
                            break
                    except Exception: pass

                if not adv_used:
                    # Press Enter to confirm
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(0.5)
                    adv_used = True

            except Exception as e:
                print(f"     ⚠  React-Select: {e}")

        # ── STRATEGY 2: Type doc type in Quick Search box ─────────────────
        if not adv_used:
            search_box = await page.query_selector(".basicSearchInputBox")
            if search_box and await search_box.is_visible():
                await search_box.click()
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Delete")
                await search_box.fill(doc_type)
                print(f"     Typed '{doc_type}' in search box")

        # ── Re-set dates (they may reset between searches) ────────────────
        current_dates = await page.query_selector_all(".react-datepicker__input")
        if len(current_dates) >= 1:
            current_val = await current_dates[0].input_value()
            if current_val != start_mm:
                await _fill_react_date(page, current_dates[0], start_mm)
        if len(current_dates) >= 2:
            current_val = await current_dates[1].input_value()
            if current_val != end_mm:
                await _fill_react_date(page, current_dates[1], end_mm)

        # ── Click Search ──────────────────────────────────────────────────
        for sel in [
            "button[type='submit']",
            "button:has-text('Search')",
            ".css-1ivgvwc",  # from log line 44
        ]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    print(f"     Searched via: {sel}")
                    break
            except Exception: pass

        # ── Wait for results to fully render ──────────────────────────────
        await asyncio.sleep(2)
        print(f"     Waiting for results …")

        loaded = False
        for attempt in range(20):
            html = await page.content()
            if re.search(r"loading results|please wait|searching\.\.\.", html, re.I):
                await asyncio.sleep(1)
                continue
            # Count data rows (not header rows)
            soup = BeautifulSoup(html, "lxml")
            data_rows = [
                tr for tr in soup.find_all("tr")
                if len(tr.find_all("td")) >= 3
            ]
            if data_rows:
                print(f"     ✅ {len(data_rows)} data rows loaded ({attempt+1}s)")
                loaded = True
                break
            await asyncio.sleep(1)

        if not loaded:
            print(f"     ⚠  No data rows found after 20s")

        # ── Parse page ────────────────────────────────────────────────────
        html      = await page.content()
        page_recs = parse_results(html, doc_type)
        recs.extend(page_recs)

        # ── Paginate ──────────────────────────────────────────────────────
        for pg in range(50):
            nxt = None
            for ns in [
                "button[aria-label*='next' i]:not([disabled])",
                "a[aria-label*='next' i]:not([disabled])",
                "li.next:not(.disabled) a",
                "a:has-text('›')", "a:has-text('»')",
                "a:has-text('Next'):not([disabled])",
                "button:has-text('Next'):not([disabled])",
            ]:
                try:
                    el = await page.query_selector(ns)
                    if el and await el.is_visible():
                        cls = await el.get_attribute("class") or ""
                        if "disabled" not in cls:
                            nxt = el; break
                except Exception: pass

            if not nxt: break

            await nxt.click()
            await asyncio.sleep(2)
            for _ in range(10):
                h = await page.content()
                if not re.search(r"loading results|please wait",h,re.I): break
                await asyncio.sleep(1)

            more = parse_results(await page.content(), doc_type)
            if not more: break
            recs.extend(more)
            print(f"     Page {pg+2}: +{len(more)}")

        # Filter to window
        return [r for r in recs
                if not r.get("filed") or r["filed"] >= start_iso]

    except Exception as e:
        print(f"     ✗ {doc_type}: {e}")
        import traceback; traceback.print_exc()
        return recs


def parse_api_row(row, doc_type, cat):
    try:
        doc_num = (safe(row.get("instrumentNumber")) or
                   safe(row.get("docNumber")) or
                   safe(row.get("bookPage")) or
                   safe(row.get("id")) or "")
        if not doc_num: return None
        filed = to_iso(row.get("filedDate") or row.get("recordedDate") or
                       row.get("dateRecorded") or row.get("date") or "")
        def names(k):
            v = row.get(k) or []
            if isinstance(v,list):
                return "; ".join(safe(g.get("name") or g.get("fullName") or str(g)) for g in v if g)
            return safe(v)
        inst = safe(row.get("id") or row.get("instrumentId") or doc_num)
        r = blank_rec(doc_type)
        r.update({
            "doc_num":doc_num,"filed":filed,
            "owner":names("grantors"),"grantee":names("grantees"),
            "amount":parse_amt(row.get("consideration") or row.get("amount") or 0),
            "legal":safe(row.get("legalDescription") or row.get("legal") or ""),
            "clerk_url":f"{CLERK_BASE}/doc/{inst}" if inst else CLERK_BASE,
        })
        return r
    except Exception as e:
        print(f"    ⚠  API row: {e}"); return None

# ── BCAD Parcel Lookup ────────────────────────────────────────────────────────
class ParcelLookup:
    # Try direct BCAD download AND the county open data portal
    URLS = [
        "https://www.bcad.org/clientdb/PropertyExport.zip",
        "https://www.bcad.org/Downloads/PropertyExport.zip",
        # Bexar County open data alternative
        "https://opendata.bcad.org/datasets/bcad::real-property.zip",
    ]

    def __init__(self):
        self.idx = {}
        self._load()

    def _load(self):
        if not HAS_DBF:
            print("  ⚠  dbfread missing"); return
        sess = make_session()
        # Rate limiting hit last time — add delay and retry header
        sess.headers["Cache-Control"] = "no-cache"
        raw = None
        for url in self.URLS:
            print(f"  ↓ BCAD: {url}")
            try:
                time.sleep(2)  # Avoid 429
                r = sess.get(url, timeout=120, stream=True)
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(65536): buf.write(chunk)
                raw = buf.getvalue()
                print(f"  ✅ {len(raw):,} bytes"); break
            except Exception as e:
                print(f"  ✗ {e}")

        if not raw:
            print("  ⚠  BCAD unavailable — no address enrichment"); return

        try:
            zf   = zipfile.ZipFile(io.BytesIO(raw))
            dbfs = sorted(
                [n for n in zf.namelist() if n.lower().endswith(".dbf")],
                key=lambda n: zf.getinfo(n).file_size, reverse=True
            )
            if not dbfs: print("  ⚠  No DBF"); return
            print(f"  📄 Indexing {dbfs[0]} …")
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                self._index(os.path.join(tmp, dbfs[0]))
            print(f"  ✅ Parcel index: {len(self.idx):,} entries")
        except Exception as e:
            print(f"  ✗ ZIP: {e}")

    def _index(self, path):
        try:
            for row in DBF(path, ignore_missing_memofile=True, encoding="latin-1"):
                r = {k.upper(): safe(v) for k,v in row.items()}
                owner = (r.get("OWNER") or r.get("OWN1") or
                         r.get("OWNERNAME") or r.get("NAME") or "").upper().strip()
                if not owner: continue
                p = {
                    "prop_address": r.get("SITE_ADDR")  or r.get("SITEADDR")  or r.get("PROP_ADDR",""),
                    "prop_city":    r.get("SITE_CITY")  or r.get("PROP_CITY",""),
                    "prop_state":   r.get("SITE_STATE","TX"),
                    "prop_zip":     r.get("SITE_ZIP")   or r.get("SITEZIP")   or r.get("PROP_ZIP",""),
                    "mail_address": r.get("ADDR_1")     or r.get("MAILADR1")  or r.get("MAIL_ADDR") or r.get("ADDRESS",""),
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
        n = re.sub(r"\s+"," ",n).strip().upper()
        v = {n}
        if "," in n:
            pts = [p.strip() for p in n.split(",",1)]
            if len(pts)==2:
                v.add(f"{pts[1]} {pts[0]}")
                v.add(pts[0]); v.add(pts[1])
        else:
            pts = n.split()
            if len(pts)>=2:
                v.add(f"{pts[-1]},{' '.join(pts[:-1])}")
                v.add(f"{pts[-1]} {' '.join(pts[:-1])}")
                v.add(pts[0]); v.add(pts[-1])
        return [x for x in v if x]

    def lookup(self, owner):
        if not owner: return {}
        for k in self._variants(re.sub(r"\s+"," ",owner).strip().upper()):
            h = self.idx.get(k)
            if h: return h
        return {}

# ── Scoring ───────────────────────────────────────────────────────────────────
def score_record(r, cutoff):
    flags=[]; sc=30
    dt=r.get("doc_type",""); a=r.get("amount",0.0)
    own=(r.get("owner") or "").upper()
    if dt in ("LP","RELLP"):               flags.append("Lis pendens")
    if dt in ("NOFC","TAXDEED"):           flags.append("Pre-foreclosure")
    if dt in ("JUD","CCJ","DRJUD"):        flags.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED"): flags.append("Tax lien")
    if dt == "LNMECH":                     flags.append("Mechanic lien")
    if dt == "LNHOA":                      flags.append("HOA lien")
    if dt == "PRO":                        flags.append("Probate / estate")
    if any(k in own for k in ("LLC","INC","CORP","LP","LTD","TRUST")):
        flags.append("LLC / corp owner")
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: sc+=20
    if a>100_000: sc+=15
    elif a>50_000: sc+=10
    if r.get("filed","")>=cutoff: flags.append("New this week"); sc+=5
    if r.get("prop_address"): sc+=5
    sc+=10*len([f for f in flags if f!="New this week"])
    return flags, min(sc,100)

# ── CSV Export ────────────────────────────────────────────────────────────────
def export_csv(records, path):
    fields=["First Name","Last Name",
            "Mailing Address","Mailing City","Mailing State","Mailing Zip",
            "Property Address","Property City","Property State","Property Zip",
            "Lead Type","Document Type","Date Filed","Document Number",
            "Amount/Debt Owed","Seller Score","Motivated Seller Flags",
            "Source","Public Records URL"]
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for r in records:
            fn,ln=_split(r.get("owner",""))
            w.writerow({
                "First Name":fn,"Last Name":ln,
                "Mailing Address":r.get("mail_address",""),
                "Mailing City":r.get("mail_city",""),
                "Mailing State":r.get("mail_state","TX"),
                "Mailing Zip":r.get("mail_zip",""),
                "Property Address":r.get("prop_address",""),
                "Property City":r.get("prop_city",""),
                "Property State":r.get("prop_state","TX"),
                "Property Zip":r.get("prop_zip",""),
                "Lead Type":r.get("cat_label",""),
                "Document Type":r.get("doc_type",""),
                "Date Filed":r.get("filed",""),
                "Document Number":r.get("doc_num",""),
                "Amount/Debt Owed":r.get("amount",""),
                "Seller Score":r.get("score",0),
                "Motivated Seller Flags":" | ".join(r.get("flags",[])),
                "Source":"Bexar County Clerk",
                "Public Records URL":r.get("clerk_url",""),
            })

def _split(full):
    if not full: return "",""
    if "," in full:
        p=[x.strip().title() for x in full.split(",",1)]; return p[1],p[0]
    p=full.strip().title().split()
    if len(p)==1: return "",p[0]
    return p[0]," ".join(p[1:])

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print("="*60)
    print("  Bexar County Motivated Seller Scraper v6")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    print(f"\n🏛  Scraping {len(DOC_TYPE_MAP)} document types …")
    all_recs = await playwright_scrape(
        list(DOC_TYPE_MAP.keys()), s_mm, e_mm, s_iso
    )

    # Dedup
    seen=set(); unique=[]
    for r in all_recs:
        k=f"{r.get('doc_num','').strip()}|{r.get('doc_type','')}"
        if k and k!="|" and k not in seen:
            seen.add(k); unique.append(r)

    # Remove garbage records
    unique=[r for r in unique
            if r.get("doc_num") and
            not re.search(r"loading|please wait|searching",
                          r.get("doc_num",""),re.I)]

    # Date filter
    in_window=[r for r in unique
               if not r.get("filed") or r["filed"]>=s_iso]

    print(f"\n✅ Unique: {len(unique)} | In window: {len(in_window)}")
    unique=in_window

    # Enrich + score
    with_addr=0
    for r in unique:
        hit=parcel.lookup(r.get("owner",""))
        if hit: r.update(hit); with_addr+=1
        r["flags"],r["score"]=score_record(r,s_iso)

    unique.sort(key=lambda x:x["score"],reverse=True)
    print(f"   With address: {with_addr}")

    payload={
        "fetched_at":datetime.utcnow().isoformat()+"Z",
        "source":"Bexar County Clerk / BCAD",
        "data_range":f"{s_iso} to {e_iso}",
        "total":len(unique),"with_address":with_addr,
        "records":unique,
    }

    for dest in [DASH_DIR/"records.json",DATA_DIR/"records.json"]:
        dest.write_text(json.dumps(payload,indent=2,default=str))
        print(f"💾 {dest}")

    export_csv(unique,DATA_DIR/"leads.csv")
    print(f"📊 {DATA_DIR/'leads.csv'}")
    print(f"\n🎉 Done — {len(unique)} leads | {with_addr} with address.\n")

if __name__=="__main__":
    asyncio.run(main())
