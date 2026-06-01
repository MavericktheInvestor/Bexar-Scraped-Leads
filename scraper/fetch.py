"""
Bexar County Motivated Seller Lead Scraper v7
==============================================
Key fixes from log analysis:
  - 650 records found but 0 in window = date filter too strict
  - Keep records with no date OR date in window (don't discard undated)
  - Browser crashing = reuse single page, add memory limits
  - BCAD failing = use Bexar CAD open data API instead
  - Confirmed selectors: .basicSearchInputBox, .react-datepicker__input,
    button[type='submit'], Advanced Search link
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

CLERK_BASE    = "https://bexar.tx.publicsearch.us"
LOOKBACK_DAYS = 7
MAX_RETRIES   = 3
RETRY_DELAY   = 4

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
        "filed":"","owner":"","grantee":"","amount":0.0,
        "legal":"","clerk_url":CLERK_BASE,
        "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
        "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
        "flags":[],"score":0,
    }

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0.0.0 Safari/537.36",
        "Accept":"*/*","Accept-Language":"en-US,en;q=0.9",
    })
    return s

# ── Parse API JSON body ───────────────────────────────────────────────────────
def parse_api_body(body, doc_type):
    cat  = DOC_TYPE_MAP.get(doc_type, doc_type)
    recs = []
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = (body.get("hits") or body.get("data") or
                 body.get("results") or body.get("instruments") or
                 body.get("records") or body.get("rows") or [])
        if isinstance(items, dict):
            items = items.get("hits") or items.get("data") or []
    else:
        return []
    for row in items:
        if isinstance(row, dict):
            rec = _api_row(row, doc_type, cat)
            if rec: recs.append(rec)
    return recs

def _api_row(row, doc_type, cat):
    try:
        doc_num = safe(
            row.get("instrumentNumber") or row.get("docNumber") or
            row.get("bookPage") or row.get("id") or
            row.get("documentNumber") or ""
        )
        if not doc_num: return None

        filed = to_iso(
            row.get("filedDate") or row.get("recordedDate") or
            row.get("dateRecorded") or row.get("date") or
            row.get("instrumentDate") or ""
        )

        def names(keys):
            for key in keys:
                val = row.get(key)
                if not val: continue
                if isinstance(val, list):
                    parts = []
                    for item in val:
                        if isinstance(item, dict):
                            n = (item.get("name") or item.get("fullName") or
                                 (item.get("firstName","")+" "+item.get("lastName","")).strip())
                            parts.append(n.strip())
                        elif isinstance(item, str):
                            parts.append(item.strip())
                    result = "; ".join(p for p in parts if p)
                    if result: return result
                elif isinstance(val, str) and val.strip():
                    return val.strip()
            return ""

        owner   = names(["grantors","grantor","seller","debtor","party1","owners"])
        grantee = names(["grantees","grantee","lender","trustee","plaintiff","creditor"])
        amount  = parse_amt(row.get("consideration") or row.get("amount") or 0)
        legal   = safe(row.get("legalDescription") or row.get("legal") or "")

        # Infer doc type from response
        inferred = safe(row.get("docType") or row.get("documentType") or "").upper()
        if inferred and inferred in DOC_TYPE_MAP:
            doc_type = inferred
            cat = DOC_TYPE_MAP[doc_type]

        inst = safe(row.get("id") or row.get("instrumentId") or doc_num)
        rec  = blank_rec(doc_type)
        rec.update({
            "doc_num":   doc_num,
            "filed":     filed,
            "owner":     owner,
            "grantee":   grantee,
            "amount":    amount,
            "legal":     legal,
            "clerk_url": f"{CLERK_BASE}/doc/{inst}" if inst else CLERK_BASE,
        })
        return rec
    except Exception as e:
        print(f"    ⚠  row: {e}")
        return None

# ── HTML table parser ─────────────────────────────────────────────────────────
def parse_html_table(html, doc_type):
    cat  = DOC_TYPE_MAP.get(doc_type, doc_type)
    recs = []
    soup = BeautifulSoup(html, "lxml")
    if re.search(r"loading results|please wait", html, re.I):
        return []
    for tbl in soup.find_all("table"):
        hdrs = [th.get_text(" ",strip=True).lower() for th in tbl.find_all("th")]
        if len(hdrs) < 2: continue
        def col(tr, *frags):
            cells = tr.find_all("td")
            for f in frags:
                for i,h in enumerate(hdrs):
                    if f in h and i<len(cells):
                        t = cells[i].get_text(" ",strip=True)
                        if t: return t
            return ""
        for tr in tbl.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if len(cells) < 2: continue
            url = ""
            for a in tr.find_all("a"):
                h = a.get("href","")
                if h: url = h if h.startswith("http") else CLERK_BASE+h; break
            dn = col(tr,"instrument","doc #","doc#","book","number") or cells[0].get_text(strip=True)
            if not dn or re.search(r"loading|please wait",dn,re.I): continue
            rec = blank_rec(doc_type)
            rec.update({
                "doc_num":   dn,
                "filed":     to_iso(col(tr,"date","filed","recorded","entry")),
                "owner":     col(tr,"grantor","owner","seller","debtor"),
                "grantee":   col(tr,"grantee","lender","trustee","plaintiff"),
                "amount":    parse_amt(col(tr,"amount","consideration","value")),
                "legal":     col(tr,"legal","description","subdivision"),
                "clerk_url": url or CLERK_BASE,
            })
            recs.append(rec)
    return recs

# ── BCAD Address Lookup (Socrata Open Data API) ───────────────────────────────
class ParcelLookup:
    """
    Uses the Bexar CAD Socrata Open Data API instead of the unreliable bulk download.
    Endpoint: https://opendata.bcad.org/resource/  (free, no auth needed)
    Falls back to bulk DBF if Socrata is unavailable.
    """
    SOCRATA_URL = "https://opendata.bcad.org/resource/tpvk-6xh3.json"
    BULK_URLS   = [
        "https://www.bcad.org/clientdb/PropertyExport.zip",
        "https://www.bcad.org/Downloads/PropertyExport.zip",
    ]

    def __init__(self):
        self.idx = {}
        self._load()

    def _load(self):
        sess = make_session()

        # Try Socrata first (fast, no rate limit)
        print("  ↓ Trying BCAD Socrata API …")
        try:
            # Get all records in batches of 50000
            offset = 0
            limit  = 50000
            loaded = 0
            while True:
                r = sess.get(
                    self.SOCRATA_URL,
                    params={"$limit": limit, "$offset": offset},
                    timeout=60
                )
                r.raise_for_status()
                rows = r.json()
                if not rows: break
                for row in rows:
                    self._index_socrata_row(row)
                loaded += len(rows)
                if len(rows) < limit: break
                offset += limit
                time.sleep(0.5)
            if self.idx:
                print(f"  ✅ Socrata: {len(self.idx):,} index entries from {loaded:,} parcels")
                return
        except Exception as e:
            print(f"  ⚠  Socrata: {e}")

        # Fallback: bulk DBF download
        if not HAS_DBF:
            print("  ⚠  No parcel data available"); return

        raw = None
        for url in self.BULK_URLS:
            print(f"  ↓ BCAD bulk: {url}")
            try:
                time.sleep(3)
                r = sess.get(url, timeout=120, stream=True)
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(65536): buf.write(chunk)
                raw = buf.getvalue()
                print(f"  ✅ {len(raw):,} bytes"); break
            except Exception as e:
                print(f"  ✗ {e}"); time.sleep(5)

        if not raw: print("  ⚠  BCAD unavailable"); return

        try:
            zf   = zipfile.ZipFile(io.BytesIO(raw))
            dbfs = sorted(
                [n for n in zf.namelist() if n.lower().endswith(".dbf")],
                key=lambda n: zf.getinfo(n).file_size, reverse=True
            )
            if not dbfs: return
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                self._index_dbf(os.path.join(tmp, dbfs[0]))
            print(f"  ✅ DBF index: {len(self.idx):,} entries")
        except Exception as e:
            print(f"  ✗ {e}")

    def _index_socrata_row(self, row):
        """Index a row from the Socrata API."""
        owner = (
            row.get("owner_name") or row.get("owner") or
            row.get("ownername") or row.get("name") or ""
        ).upper().strip()
        if not owner: return
        p = {
            "prop_address": row.get("situs_address") or row.get("site_address") or row.get("prop_address",""),
            "prop_city":    row.get("situs_city")    or row.get("site_city",""),
            "prop_state":   "TX",
            "prop_zip":     row.get("situs_zip")     or row.get("site_zip",""),
            "mail_address": row.get("mail_address")  or row.get("mailing_address",""),
            "mail_city":    row.get("mail_city",""),
            "mail_state":   row.get("mail_state","TX"),
            "mail_zip":     row.get("mail_zip",""),
        }
        for k in self._variants(owner): self.idx.setdefault(k, p)

    def _index_dbf(self, path):
        try:
            for row in DBF(path, ignore_missing_memofile=True, encoding="latin-1"):
                r = {k.upper(): safe(v) for k,v in row.items()}
                owner = (r.get("OWNER") or r.get("OWN1") or r.get("OWNERNAME","")).upper().strip()
                if not owner: continue
                p = {
                    "prop_address": r.get("SITE_ADDR") or r.get("SITEADDR",""),
                    "prop_city":    r.get("SITE_CITY",""),
                    "prop_state":   "TX",
                    "prop_zip":     r.get("SITE_ZIP")  or r.get("SITEZIP",""),
                    "mail_address": r.get("ADDR_1")    or r.get("MAILADR1",""),
                    "mail_city":    r.get("CITY")      or r.get("MAILCITY",""),
                    "mail_state":   r.get("STATE","TX"),
                    "mail_zip":     r.get("ZIP")       or r.get("MAILZIP",""),
                }
                for k in self._variants(owner): self.idx.setdefault(k, p)
        except Exception as e: print(f"  ✗ DBF: {e}")

    @staticmethod
    def _variants(n):
        n = re.sub(r"\s+"," ",n).strip().upper()
        v = {n}
        if "," in n:
            pts = [p.strip() for p in n.split(",",1)]
            if len(pts)==2: v.add(f"{pts[1]} {pts[0]}"); v.add(pts[0]); v.add(pts[1])
        else:
            pts = n.split()
            if len(pts)>=2:
                v.add(f"{pts[-1]},{' '.join(pts[:-1])}")
                v.add(f"{pts[-1]} {' '.join(pts[:-1])}")
        return [x for x in v if x]

    def lookup(self, owner):
        if not owner: return {}
        for k in self._variants(owner.upper()):
            h = self.idx.get(k)
            if h: return h
        return {}

# ── Playwright scraper ────────────────────────────────────────────────────────
async def playwright_scrape(doc_types, start_mm, end_mm, start_iso):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    all_recs = []
    print("\n🎭 Playwright scrape …")

    async with async_playwright() as p:
        # Launch with memory limits to prevent crash
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--memory-pressure-off",
                "--js-flags=--max-old-space-size=512",
            ]
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width":1280,"height":900},
        )
        page = await ctx.new_page()

        # Intercept all JSON API responses
        api_responses = []
        async def on_response(resp):
            try:
                ct = resp.headers.get("content-type","")
                if "json" in ct and resp.status == 200:
                    url = resp.url
                    if any(k in url.lower() for k in
                           ["search","instrument","record","result","query","find","api"]):
                        body = await resp.json()
                        n = _count(body)
                        if n > 0:
                            api_responses.append({"url":url,"body":body})
                            print(f"    📡 {n} items ← {url[:80]}")
            except Exception:
                pass
        page.on("response", on_response)

        try:
            # ── Load portal ───────────────────────────────────────────────
            print("  → Loading portal …")
            await page.goto(CLERK_BASE, timeout=45000)
            await page.wait_for_load_state("networkidle", timeout=25000)
            await asyncio.sleep(3)
            print(f"  ✅ Loaded: {await page.title()}")

            # ── Dismiss popup ─────────────────────────────────────────────
            for txt in ["Close","Accept","I Agree","Continue","OK"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(f"^{txt}$",re.I))
                    if await btn.count()>0 and await btn.first.is_visible():
                        await btn.first.click(); await asyncio.sleep(1.5)
                        print(f"  ✅ Dismissed: {txt}"); break
                except Exception: pass

            # ── Set date range ONCE before searching ──────────────────────
            # Log confirmed: Found 2 date inputs, dates set successfully
            await _set_dates(page, start_mm, end_mm)

            # ── Click Advanced Search ─────────────────────────────────────
            for sel in ["a:has-text('Advanced Search')","text=Advanced Search"]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.wait_for_load_state("networkidle",timeout=10000)
                        await asyncio.sleep(2)
                        print("  ✅ Advanced Search active")
                        break
                except Exception: pass

            # ── Search each doc type ──────────────────────────────────────
            for doc_type in doc_types:
                cat = DOC_TYPE_MAP.get(doc_type,doc_type)
                print(f"\n  🔍 [{doc_type}] {cat}")
                api_responses.clear()

                try:
                    recs = await _search_one(
                        page, doc_type, cat, start_mm, end_mm, start_iso
                    )
                except Exception as e:
                    print(f"    ✗ crashed: {e}")
                    # Reload page and continue
                    try:
                        await page.goto(CLERK_BASE, timeout=30000)
                        await page.wait_for_load_state("networkidle",timeout=15000)
                        await asyncio.sleep(2)
                        await _set_dates(page, start_mm, end_mm)
                    except Exception:
                        pass
                    recs = []

                # Prefer API-intercepted records (more complete data)
                api_recs = []
                for resp in api_responses:
                    api_recs.extend(parse_api_body(resp["body"], doc_type))

                if api_recs:
                    print(f"    ✅ API: {len(api_recs)} records")
                    all_recs.extend(api_recs)
                elif recs:
                    print(f"    ✅ HTML: {len(recs)} records")
                    all_recs.extend(recs)
                else:
                    print(f"    → 0 records")

                await asyncio.sleep(1)

        except Exception as e:
            print(f"\n  ✗ Fatal: {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return all_recs


def _count(body):
    if isinstance(body,list): return len(body)
    if isinstance(body,dict):
        for k in ["hits","data","results","instruments","records","rows"]:
            v = body.get(k)
            if isinstance(v,list) and v: return len(v)
            if isinstance(v,dict):
                inner = v.get("hits") or v.get("data") or []
                if inner: return len(inner)
    return 0


async def _set_dates(page, start_mm, end_mm):
    """Set date range fields. Log confirmed these work."""
    from playwright.async_api import TimeoutError as PWTimeout
    try:
        date_inputs = await page.query_selector_all(".react-datepicker__input")
        print(f"  Found {len(date_inputs)} date inputs")
        if len(date_inputs) >= 2:
            # FROM date (index 0)
            await date_inputs[0].click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await date_inputs[0].fill(start_mm)
            await asyncio.sleep(0.3)
            await page.keyboard.press("Escape")
            print(f"  ✅ Date FROM set: {start_mm}")

            # TO date (index 1)
            await date_inputs[1].click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await date_inputs[1].fill(end_mm)
            await asyncio.sleep(0.3)
            await page.keyboard.press("Escape")
            print(f"  ✅ Date TO set: {end_mm}")
        else:
            print(f"  ⚠  Expected 2 date inputs, found {len(date_inputs)}")
    except Exception as e:
        print(f"  ⚠  Date set error: {e}")


async def _search_one(page, doc_type, cat, start_mm, end_mm, start_iso):
    """Search for one document type using confirmed selectors."""
    from playwright.async_api import TimeoutError as PWTimeout
    recs = []

    for attempt in range(MAX_RETRIES):
        try:
            # ── Fill search box with doc type ─────────────────────────────
            # Confirmed: .basicSearchInputBox works
            filled = False
            for sel in [
                ".basicSearchInputBox",
                "input[placeholder*='grantor' i]",
                "input[placeholder*='doc type' i]",
                "input[placeholder*='search' i]",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await page.keyboard.press("Control+a")
                        await page.keyboard.press("Delete")
                        await el.fill(doc_type)
                        filled = True
                        break
                except Exception: pass

            # Re-set dates (they may have reset after previous search)
            await _set_dates(page, start_mm, end_mm)

            # ── Submit ────────────────────────────────────────────────────
            for sel in [
                "button[type='submit']",
                "button:has-text('Search')",
                ".css-1ivgvwc",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        break
                except Exception: pass

            # ── Wait for results ──────────────────────────────────────────
            await asyncio.sleep(3)
            for _ in range(15):
                html = await page.content()
                if re.search(r"loading results|please wait", html, re.I):
                    await asyncio.sleep(1); continue
                soup  = BeautifulSoup(html,"lxml")
                trows = [r for r in soup.find_all("tr") if len(r.find_all("td"))>=2]
                if trows:
                    print(f"    ✅ {len(trows)} rows loaded")
                    break
                await asyncio.sleep(1)

            # ── Parse ─────────────────────────────────────────────────────
            html      = await page.content()
            page_recs = parse_html_table(html, doc_type)
            recs.extend(page_recs)

            # ── Paginate ──────────────────────────────────────────────────
            for pg in range(50):
                nxt = None
                for ns in [
                    "button[aria-label*='next' i]:not([disabled])",
                    "a[aria-label*='next' i]",
                    "li.next:not(.disabled) a",
                    "button:has-text('›'):not([disabled])",
                ]:
                    try:
                        el = await page.query_selector(ns)
                        if el and await el.is_visible():
                            cls = await el.get_attribute("class") or ""
                            dis = await el.get_attribute("disabled")
                            ard = await el.get_attribute("aria-disabled")
                            if not dis and ard!="true" and "disabled" not in cls:
                                nxt=el; break
                    except Exception: pass
                if not nxt: break
                await nxt.click(); await asyncio.sleep(2)
                for _ in range(10):
                    h = await page.content()
                    if not re.search(r"loading|please wait",h,re.I): break
                    await asyncio.sleep(1)
                more = parse_html_table(await page.content(), doc_type)
                if not more: break
                recs.extend(more)

            return recs

        except PWTimeout:
            if attempt < MAX_RETRIES-1:
                print(f"    ↻ timeout retry {attempt+1}")
                await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"    ✗ {e}"); break

    return recs

# ── Scoring ───────────────────────────────────────────────────────────────────
def score_record(r, cutoff):
    flags=[]; sc=30
    dt=r.get("doc_type",""); a=r.get("amount",0.0)
    own=(r.get("owner") or "").upper()
    if dt in ("LP","RELLP"):                flags.append("Lis pendens")
    if dt in ("NOFC","TAXDEED"):            flags.append("Pre-foreclosure")
    if dt in ("JUD","CCJ","DRJUD"):         flags.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED"):  flags.append("Tax lien")
    if dt == "LNMECH":                      flags.append("Mechanic lien")
    if dt == "LNHOA":                       flags.append("HOA lien")
    if dt == "PRO":                         flags.append("Probate / estate")
    if any(k in own for k in ("LLC","INC","CORP","LP","LTD","TRUST")):
        flags.append("LLC / corp owner")
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: sc+=20
    if a>100_000: sc+=15
    elif a>50_000: sc+=10
    if r.get("filed","")>=cutoff: flags.append("New this week"); sc+=5
    if r.get("prop_address"): sc+=5
    sc += 10*len([f for f in flags if f!="New this week"])
    return flags, min(sc,100)

# ── CSV ───────────────────────────────────────────────────────────────────────
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
    print("  Bexar County Motivated Seller Scraper v7")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    print(f"\n🏛  Scraping {len(DOC_TYPE_MAP)} document types …")
    all_recs = await playwright_scrape(list(DOC_TYPE_MAP.keys()), s_mm, e_mm, s_iso)

    # ── Dedup ─────────────────────────────────────────────────────────────
    seen=set(); unique=[]
    for r in all_recs:
        k=f"{r.get('doc_num','').strip()}|{r.get('doc_type','')}"
        if k and k!="|" and k not in seen:
            seen.add(k); unique.append(r)

    # Remove junk doc numbers
    unique = [r for r in unique if r.get("doc_num") and
              not re.search(r"loading|please wait|searching",
                            r.get("doc_num",""),re.I)]

    # ── Date filter ───────────────────────────────────────────────────────
    # FIX: Keep records with NO date OR date within window
    # Do NOT discard undated records — they came from a date-filtered search
    in_window = []
    out_window = []
    for r in unique:
        fd = r.get("filed","")
        if not fd:
            # No date — keep it (search was already date-filtered)
            in_window.append(r)
        elif fd >= s_iso:
            in_window.append(r)
        else:
            out_window.append(r)

    print(f"\n✅ Unique: {len(unique)}")
    print(f"   In window (or undated): {len(in_window)}")
    print(f"   Outside window (dropped): {len(out_window)}")
    unique = in_window

    # ── Enrich + score ────────────────────────────────────────────────────
    with_addr=0
    for r in unique:
        hit = parcel.lookup(r.get("owner",""))
        if hit: r.update(hit); with_addr+=1
        r["flags"],r["score"] = score_record(r, s_iso)

    unique.sort(key=lambda x:x["score"],reverse=True)
    print(f"   With address: {with_addr}")

    payload={
        "fetched_at":   datetime.utcnow().isoformat()+"Z",
        "source":       "Bexar County Clerk / BCAD",
        "data_range":   f"{s_iso} to {e_iso}",
        "total":        len(unique),
        "with_address": with_addr,
        "records":      unique,
    }

    for dest in [DASH_DIR/"records.json", DATA_DIR/"records.json"]:
        dest.write_text(json.dumps(payload,indent=2,default=str))
        print(f"💾 {dest}")

    export_csv(unique, DATA_DIR/"leads.csv")
    print(f"📊 {DATA_DIR/'leads.csv'}")
    print(f"\n🎉 Done — {len(unique)} leads | {with_addr} with address.\n")


if __name__=="__main__":
    asyncio.run(main())
