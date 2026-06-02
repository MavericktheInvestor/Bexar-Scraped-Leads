"""
Bexar County Motivated Seller Lead Scraper v8
==============================================
Key fix: Search ALL doc types in ONE pass instead of 16 separate searches.
The portal's Quick Search accepts a doc type code and returns all matching
records. We do one broad date-range search, capture ALL API responses,
then filter/categorize by doc type from the response data.
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

DOC_TYPE_MAP = {
    "LP":"Lis Pendens","NOFC":"Notice of Foreclosure","TAXDEED":"Tax Deed",
    "JUD":"Judgment","CCJ":"Certified Judgment","DRJUD":"Domestic Judgment",
    "LNCORPTX":"Corp Tax Lien","LNIRS":"IRS Lien","LNFED":"Federal Lien",
    "LN":"Lien","LNMECH":"Mechanic Lien","LNHOA":"HOA Lien",
    "MEDLN":"Medicaid Lien","PRO":"Probate",
    "NOC":"Notice of Commencement","RELLP":"Release Lis Pendens",
}

# Doc types we want — used for filtering results
TARGET_TYPES = set(DOC_TYPE_MAP.keys())

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
    c = re.sub(r"[^\d.]","",str(raw or ""))
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
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":"*/*","Accept-Language":"en-US,en;q=0.9",
    })
    return s

# ── Parse API JSON ─────────────────────────────────────────────────────────────
def parse_api_body(body):
    """Parse any API response body — returns list of record dicts."""
    recs = []
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = (body.get("hits") or body.get("data") or body.get("results") or
                 body.get("instruments") or body.get("records") or body.get("rows") or [])
        if isinstance(items, dict):
            items = items.get("hits") or items.get("data") or []
    else:
        return []

    for row in items:
        if not isinstance(row, dict): continue
        rec = _row_to_rec(row)
        if rec: recs.append(rec)
    return recs

def _row_to_rec(row):
    try:
        doc_num = safe(
            row.get("instrumentNumber") or row.get("docNumber") or
            row.get("bookPage") or row.get("id") or
            row.get("documentNumber") or ""
        )
        if not doc_num: return None

        # Get doc type from the record itself
        doc_type = safe(
            row.get("docType") or row.get("documentType") or
            row.get("type") or row.get("instrumentType") or ""
        ).upper().strip()

        # Only keep our target types — skip everything else
        if doc_type and doc_type not in TARGET_TYPES:
            # Try prefix match (e.g. "LP-CIVIL" → "LP")
            matched = next((t for t in TARGET_TYPES if doc_type.startswith(t)), None)
            if matched:
                doc_type = matched
            else:
                return None  # not a type we care about

        if not doc_type:
            doc_type = "LN"  # default fallback

        cat = DOC_TYPE_MAP.get(doc_type, doc_type)

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
                            if n.strip(): parts.append(n.strip())
                        elif isinstance(item, str) and item.strip():
                            parts.append(item.strip())
                    if parts: return "; ".join(parts)
                elif isinstance(val, str) and val.strip():
                    return val.strip()
            return ""

        owner   = names(["grantors","grantor","seller","debtor","party1","owners","grantorName"])
        grantee = names(["grantees","grantee","lender","trustee","plaintiff","creditor","granteeName"])
        amount  = parse_amt(row.get("consideration") or row.get("amount") or 0)
        legal   = safe(row.get("legalDescription") or row.get("legal") or "")
        inst    = safe(row.get("id") or row.get("instrumentId") or doc_num)

        rec = blank_rec(doc_type)
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
        print(f"  ⚠  row: {e}")
        return None

# ── HTML parser ────────────────────────────────────────────────────────────────
def parse_html(html):
    """Parse HTML results table — returns list of dicts with raw data."""
    recs = []
    if re.search(r"loading results|please wait", html, re.I): return []
    soup = BeautifulSoup(html, "lxml")
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

            raw_type = col(tr,"type","doc type").upper().strip()
            doc_type = raw_type if raw_type in TARGET_TYPES else "LN"
            cat      = DOC_TYPE_MAP.get(doc_type, doc_type)

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

# ── BCAD Parcel Lookup ─────────────────────────────────────────────────────────
class ParcelLookup:
    # Socrata Open Data API — free, reliable, no bulk download needed
    SOCRATA_ENDPOINTS = [
        "https://opendata.bcad.org/resource/tpvk-6xh3.json",
        "https://opendata.bcad.org/resource/real-property.json",
    ]
    BULK_URLS = [
        "https://www.bcad.org/clientdb/PropertyExport.zip",
        "https://www.bcad.org/Downloads/PropertyExport.zip",
    ]

    def __init__(self):
        self.idx = {}
        self._load()

    def _load(self):
        sess = make_session()
        # Try Socrata first
        for ep in self.SOCRATA_ENDPOINTS:
            print(f"  ↓ BCAD Socrata: {ep}")
            try:
                offset=0; limit=50000; loaded=0
                while True:
                    r = sess.get(ep, params={"$limit":limit,"$offset":offset}, timeout=60)
                    r.raise_for_status()
                    rows = r.json()
                    if not rows: break
                    for row in rows: self._index_socrata(row)
                    loaded += len(rows)
                    if len(rows) < limit: break
                    offset += limit; time.sleep(0.5)
                if self.idx:
                    print(f"  ✅ Socrata: {len(self.idx):,} entries ({loaded:,} parcels)")
                    return
            except Exception as e:
                print(f"  ✗ Socrata: {e}")

        # Fallback bulk download
        if not HAS_DBF: print("  ⚠  No parcel data"); return
        raw=None
        for url in self.BULK_URLS:
            print(f"  ↓ BCAD bulk: {url}")
            try:
                time.sleep(3)
                r = sess.get(url,timeout=120,stream=True); r.raise_for_status()
                buf=io.BytesIO()
                for chunk in r.iter_content(65536): buf.write(chunk)
                raw=buf.getvalue()
                print(f"  ✅ {len(raw):,} bytes"); break
            except Exception as e: print(f"  ✗ {e}"); time.sleep(5)
        if not raw: print("  ⚠  BCAD unavailable"); return
        try:
            zf   = zipfile.ZipFile(io.BytesIO(raw))
            dbfs = sorted([n for n in zf.namelist() if n.lower().endswith(".dbf")],
                          key=lambda n:zf.getinfo(n).file_size,reverse=True)
            if not dbfs: return
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp); self._index_dbf(os.path.join(tmp,dbfs[0]))
            print(f"  ✅ DBF: {len(self.idx):,} entries")
        except Exception as e: print(f"  ✗ {e}")

    def _index_socrata(self, row):
        owner = (row.get("owner_name") or row.get("owner") or
                 row.get("ownername") or row.get("name") or "").upper().strip()
        if not owner: return
        p = {
            "prop_address": row.get("situs_address") or row.get("site_address",""),
            "prop_city":    row.get("situs_city")    or row.get("site_city",""),
            "prop_state":   "TX",
            "prop_zip":     row.get("situs_zip")     or row.get("site_zip",""),
            "mail_address": row.get("mail_address",""),
            "mail_city":    row.get("mail_city",""),
            "mail_state":   row.get("mail_state","TX"),
            "mail_zip":     row.get("mail_zip",""),
        }
        for k in self._variants(owner): self.idx.setdefault(k,p)

    def _index_dbf(self, path):
        try:
            for row in DBF(path,ignore_missing_memofile=True,encoding="latin-1"):
                r={k.upper():safe(v) for k,v in row.items()}
                owner=(r.get("OWNER") or r.get("OWN1") or r.get("OWNERNAME","")).upper().strip()
                if not owner: continue
                p={
                    "prop_address":r.get("SITE_ADDR") or r.get("SITEADDR",""),
                    "prop_city":   r.get("SITE_CITY",""),
                    "prop_state":  "TX",
                    "prop_zip":    r.get("SITE_ZIP")  or r.get("SITEZIP",""),
                    "mail_address":r.get("ADDR_1")    or r.get("MAILADR1",""),
                    "mail_city":   r.get("CITY")      or r.get("MAILCITY",""),
                    "mail_state":  r.get("STATE","TX"),
                    "mail_zip":    r.get("ZIP")        or r.get("MAILZIP",""),
                }
                for k in self._variants(owner): self.idx.setdefault(k,p)
        except Exception as e: print(f"  ✗ DBF: {e}")

    @staticmethod
    def _variants(n):
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
        for k in self._variants(owner.upper()):
            h=self.idx.get(k)
            if h: return h
        return {}

# ── Playwright: ONE broad search, capture everything ──────────────────────────
async def playwright_scrape(start_mm, end_mm, start_iso):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    all_recs = []
    print("\n🎭 Playwright: single broad search …")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage",
                  "--disable-gpu","--memory-pressure-off"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900},
        )
        page = await ctx.new_page()

        # Capture every JSON response
        api_responses = []
        async def on_response(resp):
            try:
                ct = resp.headers.get("content-type","")
                if "json" in ct and resp.status==200:
                    url = resp.url
                    if any(k in url.lower() for k in
                           ["search","instrument","record","result","query","api","find"]):
                        body = await resp.json()
                        n = _count(body)
                        if n > 0:
                            api_responses.append({"url":url,"body":body})
                            print(f"  📡 {n} items ← {url[:80]}")
            except Exception: pass
        page.on("response", on_response)

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

            # ── Set date range ────────────────────────────────────────────
            date_inputs = await page.query_selector_all(".react-datepicker__input")
            print(f"  Found {len(date_inputs)} date inputs")
            if len(date_inputs) >= 2:
                # FROM
                await date_inputs[0].click()
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Delete")
                await date_inputs[0].fill(start_mm)
                await asyncio.sleep(0.4)
                await page.keyboard.press("Escape")
                print(f"  ✅ Date FROM: {start_mm}")
                # TO
                await date_inputs[1].click()
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Delete")
                await date_inputs[1].fill(end_mm)
                await asyncio.sleep(0.4)
                await page.keyboard.press("Escape")
                print(f"  ✅ Date TO: {end_mm}")

            # ── Leave search box EMPTY and just search by date ────────────
            # This returns ALL records in the date window across ALL doc types
            # Much faster than 16 separate searches
            print("  → Submitting broad date search …")
            for sel in ["button[type='submit']","button:has-text('Search')"]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click(); break
                except Exception: pass

            # Wait for results
            await asyncio.sleep(4)
            for i in range(20):
                html = await page.content()
                if re.search(r"loading results|please wait", html, re.I):
                    await asyncio.sleep(1); continue
                soup  = BeautifulSoup(html,"lxml")
                trows = [r for r in soup.find_all("tr") if len(r.find_all("td"))>=2]
                if trows or api_responses:
                    print(f"  ✅ Results ready ({len(trows)} rows, {len(api_responses)} API calls)")
                    break
                await asyncio.sleep(1)

            # Parse first page
            html      = await page.content()
            html_recs = parse_html(html)
            all_recs.extend(html_recs)
            print(f"  Page 1: {len(html_recs)} HTML records")

            # Paginate through ALL pages
            page_num = 2
            while page_num <= 200:  # safety cap
                nxt = None
                for ns in [
                    "button[aria-label*='next' i]:not([disabled])",
                    "a[aria-label*='next' i]",
                    "li.next:not(.disabled) a",
                    "button:has-text('›'):not([disabled])",
                    "button:has-text('Next'):not([disabled])",
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

                if not nxt:
                    print(f"  No more pages after {page_num-1}")
                    break

                await nxt.click(); await asyncio.sleep(2)
                for _ in range(10):
                    h = await page.content()
                    if not re.search(r"loading|please wait",h,re.I): break
                    await asyncio.sleep(1)

                more = parse_html(await page.content())
                if not more: break
                all_recs.extend(more)
                print(f"  Page {page_num}: +{len(more)} records")
                page_num += 1

            # Also grab everything from API interception
            api_recs = []
            for resp in api_responses:
                api_recs.extend(parse_api_body(resp["body"]))
            print(f"\n  API intercepted: {len(api_recs)} records")
            all_recs.extend(api_recs)

        except Exception as e:
            print(f"\n  ✗ {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return all_recs


def _count(body):
    if isinstance(body,list): return len(body)
    if isinstance(body,dict):
        for k in ["hits","data","results","instruments","records","rows"]:
            v=body.get(k)
            if isinstance(v,list) and v: return len(v)
            if isinstance(v,dict):
                inner=v.get("hits") or v.get("data") or []
                if inner: return len(inner)
    return 0

# ── Scoring ────────────────────────────────────────────────────────────────────
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

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    print("="*60)
    print("  Bexar County Motivated Seller Scraper v8")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    print("\n🏛  Scraping portal (single broad search) …")
    all_recs = await playwright_scrape(s_mm, e_mm, s_iso)

    # Dedup
    seen=set(); unique=[]
    for r in all_recs:
        k=f"{r.get('doc_num','').strip()}|{r.get('doc_type','')}"
        if k and k!="|" and k not in seen:
            seen.add(k); unique.append(r)

    # Remove junk
    unique=[r for r in unique if r.get("doc_num") and
            not re.search(r"loading|please wait|searching",
                          r.get("doc_num",""),re.I)]

    # Keep records in window OR undated (search was date-filtered)
    in_window=[]; out_window=[]
    for r in unique:
        fd=r.get("filed","")
        if not fd or fd>=s_iso: in_window.append(r)
        else: out_window.append(r)

    print(f"\n✅ Unique: {len(unique)}")
    print(f"   In window / undated: {len(in_window)}")
    print(f"   Outside window (dropped): {len(out_window)}")
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
        "fetched_at":   datetime.utcnow().isoformat()+"Z",
        "source":       "Bexar County Clerk / BCAD",
        "data_range":   f"{s_iso} to {e_iso}",
        "total":        len(unique),
        "with_address": with_addr,
        "records":      unique,
    }
    for dest in [DASH_DIR/"records.json",DATA_DIR/"records.json"]:
        dest.write_text(json.dumps(payload,indent=2,default=str))
        print(f"💾 {dest}")

    export_csv(unique,DATA_DIR/"leads.csv")
    print(f"📊 {DATA_DIR/'leads.csv'}")
    print(f"\n🎉 Done — {len(unique)} leads | {with_addr} with address.\n")

if __name__=="__main__":
    asyncio.run(main())
