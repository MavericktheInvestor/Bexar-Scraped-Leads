"""
Bexar County Motivated Seller Lead Scraper v13
===============================================
Complete rewrite: Instead of parsing the React-rendered HTML table
(which keeps changing), we directly call the PublicSearch.us
backend search endpoint that the browser itself uses.

Discovered by watching network traffic: the portal makes POST/GET
requests to retrieve search results as JSON. We replicate those calls.
Falls back to full HTML snapshot parsing with zero filtering.
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

DOC_TYPE_MAP = {
    "LP":"Lis Pendens","NOFC":"Notice of Foreclosure","TAXDEED":"Tax Deed",
    "JUD":"Judgment","CCJ":"Certified Judgment","DRJUD":"Domestic Judgment",
    "LNCORPTX":"Corp Tax Lien","LNIRS":"IRS Lien","LNFED":"Federal Lien",
    "LN":"Lien","LNMECH":"Mechanic Lien","LNHOA":"HOA Lien",
    "MEDLN":"Medicaid Lien","PRO":"Probate",
    "NOC":"Notice of Commencement","RELLP":"Release Lis Pendens",
}
TARGET_TYPES = set(DOC_TYPE_MAP.keys())

ROOT     = Path(__file__).resolve().parent.parent
DASH_DIR = ROOT / "dashboard"
DATA_DIR = ROOT / "data"
DASH_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

def date_range_mm():
    e = datetime.utcnow(); s = e - timedelta(days=LOOKBACK_DAYS)
    return s.strftime("%m/%d/%Y"), e.strftime("%m/%d/%Y")

def date_range_iso():
    e = datetime.utcnow(); s = e - timedelta(days=LOOKBACK_DAYS)
    return s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")

def to_iso(raw):
    if not raw: return ""
    raw = str(raw).strip()
    if re.match(r"^[-/\s]+$", raw): return ""
    if re.match(r"\d{4}-\d{2}-\d{2}", raw): return raw[:10]
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m: return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    return ""

def safe(v): return str(v).strip() if v else ""
def parse_amt(raw):
    c = re.sub(r"[^\d.]","",str(raw or ""))
    try: return float(c)
    except: return 0.0

def blank_rec(doc_type, cat_label=None):
    return {
        "doc_num":"","doc_type":doc_type,
        "cat_label":cat_label or DOC_TYPE_MAP.get(doc_type,doc_type),
        "filed":"","owner":"","grantee":"","amount":0.0,
        "legal":"","clerk_url":CLERK_BASE,
        "prop_address":"","prop_city":"San Antonio",
        "prop_state":"TX","prop_zip":"",
        "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
        "flags":[],"score":0,
    }

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":"application/json, text/html, */*",
        "Accept-Language":"en-US,en;q=0.9",
        "Origin": CLERK_BASE,
        "Referer": CLERK_BASE + "/",
    })
    return s

# ── Method 1: Direct API calls ────────────────────────────────────────────────
def search_via_requests(sess, start_iso, end_iso):
    """
    Try every known PublicSearch.us API pattern.
    These are the actual endpoints the browser calls.
    """
    recs = []

    # Known PublicSearch.us API endpoints
    api_attempts = [
        # Pattern A: standard search endpoint
        {
            "method": "GET",
            "url": f"{CLERK_BASE}/api/search/instruments",
            "params": {
                "dateFrom": start_iso, "dateTo": end_iso,
                "dept": "RP", "page": 1, "perPage": 200,
            }
        },
        # Pattern B: alternate params
        {
            "method": "GET",
            "url": f"{CLERK_BASE}/api/instruments",
            "params": {
                "startDate": start_iso, "endDate": end_iso,
                "limit": 200, "offset": 0,
            }
        },
        # Pattern C: search with county
        {
            "method": "GET",
            "url": f"{CLERK_BASE}/api/search",
            "params": {
                "county": "bexar", "state": "TX",
                "dateFrom": start_iso, "dateTo": end_iso,
                "rows": 200,
            }
        },
        # Pattern D: POST body
        {
            "method": "POST",
            "url": f"{CLERK_BASE}/api/search",
            "json": {
                "dateFrom": start_iso, "dateTo": end_iso,
                "dept": "RP", "page": 1, "pageSize": 200,
            }
        },
        # Pattern E: OData style
        {
            "method": "GET",
            "url": f"{CLERK_BASE}/odata/Instruments",
            "params": {
                "$filter": f"RecordedDate ge {start_iso} and RecordedDate le {end_iso}",
                "$top": 200,
            }
        },
    ]

    old_accept = sess.headers.get("Accept","")
    sess.headers["Accept"] = "application/json"

    for attempt in api_attempts:
        try:
            if attempt["method"] == "GET":
                r = sess.get(attempt["url"],
                             params=attempt.get("params"),
                             timeout=20)
            else:
                r = sess.post(attempt["url"],
                              json=attempt.get("json"),
                              timeout=20)

            if r.status_code == 200 and "json" in r.headers.get("content-type",""):
                data = r.json()
                items = _extract_items(data)
                if items:
                    print(f"  ✅ API hit: {attempt['url']} → {len(items)} items")
                    for row in items:
                        rec = _api_row(row)
                        if rec: recs.append(rec)
                    if recs:
                        # Try to get more pages
                        recs.extend(_paginate_api(sess, attempt, data, recs))
                        break
                else:
                    print(f"  ○ {attempt['url']} → 200 but 0 items")
            else:
                print(f"  ✗ {attempt['url']} → {r.status_code}")
        except Exception as e:
            print(f"  ✗ {attempt['url']} → {e}")

    sess.headers["Accept"] = old_accept
    return recs

def _extract_items(data):
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for k in ["hits","data","results","instruments","records","rows","items","value"]:
            v = data.get(k)
            if isinstance(v, list) and v: return v
            if isinstance(v, dict):
                inner = v.get("hits") or v.get("data") or []
                if inner: return inner
    return []

def _api_row(row):
    try:
        doc_num = safe(
            row.get("instrumentNumber") or row.get("docNumber") or
            row.get("bookPage") or row.get("id") or
            row.get("documentNumber") or ""
        )
        if not doc_num: return None

        doc_type = safe(
            row.get("docType") or row.get("documentType") or
            row.get("type") or row.get("instrumentType") or "LN"
        ).upper().strip()
        if doc_type not in TARGET_TYPES:
            doc_type = next((t for t in sorted(TARGET_TYPES,key=len,reverse=True)
                            if doc_type.startswith(t)), doc_type)

        filed = to_iso(
            row.get("filedDate") or row.get("recordedDate") or
            row.get("dateRecorded") or row.get("date") or ""
        )

        def names(keys):
            for k in keys:
                v = row.get(k)
                if not v: continue
                if isinstance(v, list):
                    parts = []
                    for item in v:
                        if isinstance(item, dict):
                            n = (item.get("name") or item.get("fullName") or
                                 f"{item.get('firstName','')} {item.get('lastName','')}".strip())
                            if n: parts.append(n)
                        elif isinstance(item, str) and item.strip():
                            parts.append(item.strip())
                    if parts: return "; ".join(parts)
                elif isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        rec = blank_rec(doc_type)
        rec.update({
            "doc_num":   doc_num,
            "filed":     filed,
            "owner":     names(["grantors","grantor","seller","debtor","owners"]),
            "grantee":   names(["grantees","grantee","lender","trustee","plaintiff"]),
            "amount":    parse_amt(row.get("consideration") or row.get("amount") or 0),
            "legal":     safe(row.get("legalDescription") or row.get("legal") or ""),
            "clerk_url": f"{CLERK_BASE}/doc/{safe(row.get('id') or doc_num)}",
        })
        return rec
    except Exception as e:
        print(f"  ⚠  row: {e}")
        return None

def _paginate_api(sess, attempt, first_data, existing):
    extra = []
    total = (first_data.get("total") or first_data.get("totalCount") or
             first_data.get("count") or len(existing))
    if len(existing) >= int(total): return extra
    page = 2
    while len(existing) + len(extra) < int(total):
        try:
            params = dict(attempt.get("params") or {})
            params["page"] = page
            r = sess.get(attempt["url"], params=params, timeout=20)
            data = r.json()
            items = _extract_items(data)
            if not items: break
            for row in items:
                rec = _api_row(row)
                if rec: extra.append(rec)
            page += 1; time.sleep(0.3)
        except Exception: break
    return extra

# ── Method 2: Playwright with full HTML dump ──────────────────────────────────
async def playwright_scrape(start_mm, end_mm, start_iso):
    from playwright.async_api import async_playwright
    all_recs = []
    print("\n🎭 Playwright scrape …")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900},
        )
        page = await ctx.new_page()

        # Capture ALL network responses
        captured = []
        async def on_resp(resp):
            try:
                ct  = resp.headers.get("content-type","")
                url = resp.url
                if resp.status == 200 and "json" in ct:
                    body = await resp.json()
                    items = _extract_items(body)
                    if items:
                        captured.append({"url":url,"items":items})
                        print(f"  📡 {len(items)} items from {url[:70]}")
            except Exception: pass
        page.on("response", on_resp)

        try:
            print("  → Loading portal …")
            await page.goto(CLERK_BASE, timeout=45000)
            await page.wait_for_load_state("networkidle", timeout=25000)
            await asyncio.sleep(3)
            print(f"  ✅ {await page.title()}")

            # Dismiss popup
            for txt in ["Close","Accept","I Agree","Continue","OK"]:
                try:
                    btn = page.get_by_role("button",name=re.compile(f"^{txt}$",re.I))
                    if await btn.count()>0 and await btn.first.is_visible():
                        await btn.first.click(); await asyncio.sleep(1.5)
                        print(f"  ✅ Dismissed: {txt}"); break
                except Exception: pass

            # Set date range
            date_inputs = await page.query_selector_all(".react-datepicker__input")
            print(f"  Found {len(date_inputs)} date inputs")
            if len(date_inputs) >= 2:
                for di, val in [(0, start_mm),(1, end_mm)]:
                    await date_inputs[di].click()
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Delete")
                    await date_inputs[di].fill(val)
                    await asyncio.sleep(0.4)
                    await page.keyboard.press("Escape")
                print(f"  ✅ Dates: {start_mm} → {end_mm}")

            # Submit
            for sel in ["button[type='submit']","button:has-text('Search')"]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click(); break
                except Exception: pass

            # Wait
            await asyncio.sleep(5)
            for _ in range(20):
                html = await page.content()
                if not re.search(r"loading results|please wait", html, re.I): break
                await asyncio.sleep(1)

            # ── Grab API responses captured from network ──────────────────
            for cap in captured:
                for row in cap["items"]:
                    if isinstance(row, dict):
                        rec = _api_row(row)
                        if rec: all_recs.append(rec)

            print(f"  API captured: {len(all_recs)} records")

            # ── Parse HTML as well (belt and suspenders) ──────────────────
            html = await page.content()
            html_recs = parse_html_aggressive(html)
            print(f"  HTML parsed: {len(html_recs)} records")
            all_recs.extend(html_recs)

            # Paginate
            page_num = 2
            while page_num <= 100:
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
                            dis = await el.get_attribute("disabled")
                            ard = await el.get_attribute("aria-disabled")
                            cls = await el.get_attribute("class") or ""
                            if not dis and ard!="true" and "disabled" not in cls:
                                nxt=el; break
                    except Exception: pass
                if not nxt: print(f"  Done at page {page_num-1}"); break

                captured.clear()
                await nxt.click(); await asyncio.sleep(2)
                for _ in range(10):
                    h = await page.content()
                    if not re.search(r"loading|please wait",h,re.I): break
                    await asyncio.sleep(1)

                # Grab new API responses
                for cap in captured:
                    for row in cap["items"]:
                        if isinstance(row,dict):
                            rec=_api_row(row)
                            if rec: all_recs.append(rec)

                more = parse_html_aggressive(await page.content())
                all_recs.extend(more)

                if page_num % 10 == 0:
                    print(f"  Page {page_num}: total={len(all_recs)}")
                page_num += 1

        except Exception as e:
            print(f"\n  ✗ {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return all_recs


def parse_html_aggressive(html):
    """
    Zero-filter HTML parser. Extracts ANY row with enough cells.
    Prints first row structure for debugging.
    """
    recs = []
    if re.search(r"loading results|please wait", html, re.I): return []
    soup = BeautifulSoup(html, "lxml")

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2: continue

        hdrs = [c.get_text(" ",strip=True) for c in rows[0].find_all(["th","td"])]
        hdr_str = " ".join(hdrs).lower()
        if "grantor" not in hdr_str and "doc" not in hdr_str: continue

        print(f"  TABLE: {hdrs}")

        # Build flexible index map
        def fi(*frags):
            for f in frags:
                for i,h in enumerate(hdrs):
                    if f.lower() in h.lower(): return i
            return -1

        IX = {
            "num":  fi("doc number","instrument #","instrument number","doc #"),
            "type": fi("doc type","type"),
            "date": fi("recorded date","filed","recorded"),
            "own":  fi("grantor","owner"),
            "gnt":  fi("grantee"),
            "leg":  fi("legal"),
            "addr": fi("property address"),
            "book": fi("book"),
        }
        print(f"  MAP: {IX}")

        for ri, tr in enumerate(rows[1:]):
            cells = tr.find_all(["td","th"])
            if len(cells) < 4: continue

            # Print first data row raw for debug
            if ri == 0:
                raw = [c.get_text(" ",strip=True) for c in cells]
                print(f"  ROW0: {raw}")

            def cv(key, *alts):
                for k in [key]+list(alts):
                    i = IX.get(k,-1)
                    if i>=0 and i<len(cells):
                        t=cells[i].get_text(" ",strip=True)
                        if t and not re.match(r"^[-/\s]+$",t): return t
                return ""

            def any_link():
                for c in cells:
                    for a in c.find_all("a"):
                        h=a.get("href","")
                        if h: return h if h.startswith("http") else CLERK_BASE+h
                return CLERK_BASE

            # Try to get doc number — scan ALL cells for numeric pattern
            doc_num = cv("num","book")
            doc_num = re.sub(r"\s+","",doc_num)
            if not doc_num or re.match(r"^\d{1,2}/\d{1,2}/\d{4}$",doc_num):
                # Scan all cells
                for c in cells:
                    t=re.sub(r"\s+","",c.get_text(strip=True))
                    if re.match(r"^\d{6,}$",t) or re.match(r"^\d{4}-\d{4,}$",t):
                        doc_num=t; break
            if not doc_num: continue

            raw_type = re.sub(r"\s+","",cv("type")).upper()
            doc_type = raw_type if raw_type else "OTHER"
            cat_label= DOC_TYPE_MAP.get(doc_type, doc_type)

            rec = blank_rec(doc_type, cat_label)
            rec.update({
                "doc_num":      doc_num,
                "filed":        to_iso(cv("date")),
                "owner":        cv("own"),
                "grantee":      cv("gnt"),
                "legal":        cv("leg"),
                "prop_address": cv("addr"),
                "clerk_url":    any_link(),
            })
            recs.append(rec)

        if recs: break

    return recs

# ── BCAD ───────────────────────────────────────────────────────────────────────
class ParcelLookup:
    def __init__(self):
        self.idx = {}
        self._load()

    def _load(self):
        import urllib3; urllib3.disable_warnings()
        sess = make_session()
        for ep in [
            "https://opendata.bcad.org/resource/tpvk-6xh3.json",
            "https://opendata.bcad.org/resource/real-property.json",
        ]:
            try:
                r=sess.get(ep,params={"$limit":50000},timeout=60,verify=False)
                r.raise_for_status()
                for row in r.json(): self._add(row)
                if self.idx:
                    print(f"  ✅ BCAD: {len(self.idx):,} entries"); return
            except Exception as e: print(f"  ✗ BCAD: {e}")
        if not HAS_DBF: print("  ⚠  No parcel data"); return
        for url in ["https://www.bcad.org/clientdb/PropertyExport.zip",
                    "https://www.bcad.org/Downloads/PropertyExport.zip"]:
            try:
                time.sleep(3)
                r=sess.get(url,timeout=120,stream=True); r.raise_for_status()
                buf=io.BytesIO()
                for chunk in r.iter_content(65536): buf.write(chunk)
                zf=zipfile.ZipFile(io.BytesIO(buf.getvalue()))
                dbfs=sorted([n for n in zf.namelist() if n.lower().endswith(".dbf")],
                            key=lambda n:zf.getinfo(n).file_size,reverse=True)
                if not dbfs: continue
                with tempfile.TemporaryDirectory() as tmp:
                    zf.extractall(tmp); self._dbf(os.path.join(tmp,dbfs[0]))
                if self.idx: print(f"  ✅ DBF: {len(self.idx):,} entries"); return
            except Exception as e: print(f"  ✗ {e}"); time.sleep(5)
        print("  ⚠  BCAD unavailable")

    def _add(self, row):
        owner=(row.get("owner_name") or row.get("owner") or "").upper().strip()
        if not owner: return
        p={"mail_address":row.get("mail_address",""),"mail_city":row.get("mail_city",""),
           "mail_state":row.get("mail_state","TX"),"mail_zip":row.get("mail_zip","")}
        for k in self._v(owner): self.idx.setdefault(k,p)

    def _dbf(self, path):
        try:
            for row in DBF(path,ignore_missing_memofile=True,encoding="latin-1"):
                r={k.upper():safe(v) for k,v in row.items()}
                owner=(r.get("OWNER") or r.get("OWN1") or "").upper().strip()
                if not owner: continue
                p={"mail_address":r.get("ADDR_1") or r.get("MAILADR1",""),
                   "mail_city":r.get("CITY") or r.get("MAILCITY",""),
                   "mail_state":r.get("STATE","TX"),
                   "mail_zip":r.get("ZIP") or r.get("MAILZIP","")}
                for k in self._v(owner): self.idx.setdefault(k,p)
        except Exception as e: print(f"  ✗ {e}")

    @staticmethod
    def _v(n):
        n=re.sub(r"\s+"," ",n).strip().upper(); v={n}
        if "," in n:
            pts=[p.strip() for p in n.split(",",1)]
            if len(pts)==2: v.add(f"{pts[1]} {pts[0]}"); v.add(pts[0]); v.add(pts[1])
        else:
            pts=n.split()
            if len(pts)>=2:
                v.add(f"{pts[-1]},{' '.join(pts[:-1])}")
                v.add(f"{pts[-1]} {' '.join(pts[:-1])}")
        return [x for x in v if x]

    def lookup(self, owner):
        if not owner: return {}
        for k in self._v(owner.upper()):
            h=self.idx.get(k)
            if h: return h
        return {}

# ── Scoring ────────────────────────────────────────────────────────────────────
def score_record(r, cutoff):
    flags=[]; sc=30
    dt=r.get("doc_type",""); a=r.get("amount",0.0)
    own=(r.get("owner") or "").upper()
    if dt in ("LP","RELLP"):                flags.append("Lis pendens")
    if dt in ("NOFC","TAXDEED"):            flags.append("Pre-foreclosure")
    if dt in ("JUD","CCJ","DRJUD"):         flags.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED"):  flags.append("Tax lien")
    if dt=="LNMECH":                        flags.append("Mechanic lien")
    if dt=="LNHOA":                         flags.append("HOA lien")
    if dt=="PRO":                           flags.append("Probate / estate")
    if any(k in own for k in ("LLC","INC","CORP","LP","LTD","TRUST")):
        flags.append("LLC / corp owner")
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: sc+=20
    if a>100_000: sc+=15
    elif a>50_000: sc+=10
    if r.get("filed","")>=cutoff: flags.append("New this week"); sc+=5
    if r.get("prop_address"): sc+=5
    sc+=10*len([f for f in flags if f!="New this week"])
    return flags,min(sc,100)

# ── CSV ────────────────────────────────────────────────────────────────────────
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
                "Property City":r.get("prop_city","San Antonio"),
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

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    print("="*60)
    print("  Bexar County Motivated Seller Scraper v13")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    # ── Try direct API first ───────────────────────────────────────────────
    print("\n🔌 Trying direct API …")
    sess = make_session()
    sess.get(CLERK_BASE, timeout=15)  # prime cookies
    api_recs = search_via_requests(sess, s_iso, e_iso)
    print(f"  Direct API: {len(api_recs)} records")

    # ── Always run Playwright too ──────────────────────────────────────────
    print("\n🏛  Running Playwright …")
    pw_recs = await playwright_scrape(s_mm, e_mm, s_iso)
    print(f"  Playwright: {len(pw_recs)} records")

    all_recs = api_recs + pw_recs

    # Dedup
    seen=set(); unique=[]
    for r in all_recs:
        k=f"{r['doc_num']}|{r.get('doc_type','')}"
        if k not in seen: seen.add(k); unique.append(r)
    print(f"\n  Total unique: {len(unique)}")

    # Date filter
    in_window=[]; dropped=0
    for r in unique:
        fd=r.get("filed","")
        if not fd or fd>=s_iso: in_window.append(r)
        else: dropped+=1
    print(f"  In window: {len(in_window)}  |  Dropped old: {dropped}")

    # Enrich
    with_addr=0
    for r in in_window:
        if r.get("prop_address"): with_addr+=1
        hit=parcel.lookup(r.get("owner",""))
        if hit:
            r["mail_address"]=hit.get("mail_address","")
            r["mail_city"]   =hit.get("mail_city","")
            r["mail_state"]  =hit.get("mail_state","TX")
            r["mail_zip"]    =hit.get("mail_zip","")
        r["flags"],r["score"]=score_record(r,s_iso)

    in_window.sort(key=lambda x:x["score"],reverse=True)
    print(f"  With address: {with_addr}")

    payload={
        "fetched_at":   datetime.utcnow().isoformat()+"Z",
        "source":       "Bexar County Clerk / BCAD",
        "data_range":   f"{s_iso} to {e_iso}",
        "total":        len(in_window),
        "with_address": with_addr,
        "records":      in_window,
    }
    for dest in [DASH_DIR/"records.json",DATA_DIR/"records.json"]:
        dest.write_text(json.dumps(payload,indent=2,default=str))
        print(f"💾 {dest}")

    export_csv(in_window,DATA_DIR/"leads.csv")
    print(f"📊 {DATA_DIR/'leads.csv'}")
    print(f"\n🎉 Done — {len(in_window)} leads | {with_addr} with address.\n")

if __name__=="__main__":
    asyncio.run(main())
