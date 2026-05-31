"""
Bexar County Motivated Seller Lead Scraper v3
==============================================
- Tries requests/API first, then Playwright browser
- Strictly filters to last 7 days only
- Parses the exact UI seen at bexar.tx.publicsearch.us
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

def amt(raw):
    c = re.sub(r"[^\d.]","",str(raw or ""))
    try: return float(c)
    except: return 0.0

# ── Session ───────────────────────────────────────────────────────────────────
def session():
    s = requests.Session()
    s.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0.0.0 Safari/537.36",
        "Accept":"text/html,application/xhtml+xml,*/*",
        "Accept-Language":"en-US,en;q=0.9",
    })
    return s

def get(sess, url, **kw):
    for i in range(MAX_RETRIES):
        try:
            r = sess.get(url, timeout=30, **kw); r.raise_for_status(); return r
        except Exception as e:
            if i < MAX_RETRIES-1: time.sleep(RETRY_DELAY)
            else: print(f"    ✗ {e}")
    return None

# ── API search ────────────────────────────────────────────────────────────────
def api_search(sess, doc_type, s_iso, e_iso):
    cat = DOC_TYPE_MAP.get(doc_type, doc_type)
    recs = []
    sess.headers["Accept"] = "application/json"
    for ep, params in [
        (f"{CLERK_BASE}/api/instruments",
         {"docType":doc_type,"dateFrom":s_iso,"dateTo":e_iso,"page":1,"perPage":200}),
        (f"{CLERK_BASE}/api/search",
         {"type":doc_type,"start":s_iso,"end":e_iso,"page":1,"size":200}),
        (f"{CLERK_BASE}/api/records",
         {"documentType":doc_type,"startDate":s_iso,"endDate":e_iso}),
    ]:
        r = get(sess, ep, params=params)
        if not r or "json" not in r.headers.get("content-type",""):
            continue
        try:
            data  = r.json()
            items = (data.get("data") or data.get("results") or
                     data.get("instruments") or data.get("hits") or
                     data.get("records") or (data if isinstance(data,list) else []))
            for row in items:
                rec = _parse_api_row(row, doc_type, cat)
                if rec: recs.append(rec)
            if recs:
                break
        except Exception as e:
            print(f"    ⚠  {e}")
    sess.headers["Accept"] = "text/html,*/*"
    return recs

def _parse_api_row(row, doc_type, cat):
    try:
        doc_num = (safe(row.get("instrumentNumber")) or safe(row.get("docNumber")) or
                   safe(row.get("bookPage")) or safe(row.get("id")) or "")
        if not doc_num: return None
        filed = to_iso(row.get("filedDate") or row.get("recordedDate") or
                       row.get("dateRecorded") or row.get("date",""))
        def names(key):
            v = row.get(key) or []
            if isinstance(v,list):
                return "; ".join(safe(g.get("name") or g.get("fullName") or str(g)) for g in v)
            return safe(v)
        inst_id = safe(row.get("id") or row.get("instrumentId") or doc_num)
        return {
            "doc_num":doc_num,"doc_type":doc_type,"cat_label":cat,
            "filed":filed,"owner":names("grantors"),"grantee":names("grantees"),
            "amount":amt(row.get("consideration") or row.get("amount",0)),
            "legal":safe(row.get("legalDescription") or row.get("legal","")),
            "clerk_url":f"{CLERK_BASE}/doc/{inst_id}" if inst_id else CLERK_BASE,
            "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
            "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
            "flags":[],"score":0,
        }
    except: return None

# ── HTML parse ────────────────────────────────────────────────────────────────
def parse_html(html, doc_type, cat):
    recs = []; soup = BeautifulSoup(html,"lxml")
    for tbl in soup.find_all("table"):
        hdrs = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
        if len(hdrs) < 3: continue
        def col(tr, frags):
            for f in frags:
                for i,h in enumerate(hdrs):
                    if f in h:
                        cells = tr.find_all("td")
                        return cells[i].get_text(strip=True) if i<len(cells) else ""
            return ""
        for tr in tbl.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if len(cells) < 3: continue
            url = ""
            for a in tr.find_all("a"):
                href=a.get("href","")
                if href: url=href if href.startswith("http") else CLERK_BASE+href; break
            dn = col(tr,["instrument","doc #","doc#","book","number"]) or cells[0].get_text(strip=True)
            if not dn: continue
            recs.append({
                "doc_num":dn,"doc_type":doc_type,"cat_label":cat,
                "filed":to_iso(col(tr,["date","filed","recorded"])),
                "owner":col(tr,["grantor","owner"]),
                "grantee":col(tr,["grantee","lender","trustee"]),
                "amount":amt(col(tr,["amount","consideration","value"])),
                "legal":col(tr,["legal","description","subdivision"]),
                "clerk_url":url or CLERK_BASE,
                "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
                "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
                "flags":[],"score":0,
            })
    return recs

# ── Playwright ────────────────────────────────────────────────────────────────
async def playwright_scrape(doc_types, start_mm, end_mm):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    recs = []
    print("\n🎭 Playwright browser scrape …")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900},
        )
        page = await ctx.new_page()

        # Capture JSON API responses automatically
        api_data = []
        async def on_resp(resp):
            if any(k in resp.url for k in ["api","instrument","search","result"]):
                try:
                    if "json" in resp.headers.get("content-type",""):
                        api_data.append(await resp.json())
                except: pass
        page.on("response", on_resp)

        try:
            await page.goto(CLERK_BASE, timeout=40000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            await asyncio.sleep(2)

            # Dismiss disclaimer if present
            for txt in ["Accept","I Agree","Continue","OK"]:
                try:
                    btn = page.get_by_role("button", name=txt)
                    if await btn.count() and await btn.first.is_visible():
                        await btn.first.click(); await asyncio.sleep(1); break
                except: pass

            # Try Advanced Search tab (visible in screenshot)
            try:
                adv = page.get_by_text("Advanced Search", exact=True)
                if await adv.count():
                    await adv.first.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await asyncio.sleep(1)
                    print("  ✅ Advanced Search tab active")
            except: pass

            for doc_type in doc_types:
                cat = DOC_TYPE_MAP.get(doc_type, doc_type)
                print(f"  🔍 [{doc_type}]")
                api_data.clear()

                found = await _pw_search(page, doc_type, cat, start_mm, end_mm)
                recs.extend(found)

                # Also grab anything captured from the network
                for body in api_data:
                    items = (body.get("data") or body.get("results") or
                             body.get("instruments") or body.get("hits") or
                             (body if isinstance(body,list) else []))
                    for row in items:
                        if isinstance(row,dict):
                            rec = _parse_api_row(row, doc_type, cat)
                            if rec: recs.append(rec)

                print(f"     → {len(found)} records")
                await asyncio.sleep(1.5)

        except Exception as e:
            print(f"  ✗ {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return recs


async def _pw_search(page, doc_type, cat, start_mm, end_mm):
    from playwright.async_api import TimeoutError as PWTimeout
    recs = []

    for attempt in range(MAX_RETRIES):
        try:
            # Fill doc type in search box (Quick Search mode)
            for sel in [
                "input[placeholder*='grantor' i]",
                "input[placeholder*='search' i]",
                "input[name*='search' i]",
                "#searchTerm","input[type='search']",
            ]:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.triple_click(); await el.fill(doc_type); break

            # Try to set the date range
            # The portal shows "Date Range: 1/1/1800 → 5/27/2026"
            # We need to change this to our window
            for sel in [
                "input[id*='from' i]","input[name*='from' i]",
                "input[placeholder*='from' i]","#dateFrom","#startDate",
                ".date-range input:first-child",
            ]:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.triple_click(); await el.fill(""); await el.type(start_mm, delay=40); break

            for sel in [
                "input[id*='to' i]","input[name*='to' i]",
                "input[placeholder*='to' i]","#dateTo","#endDate",
                ".date-range input:last-child",
            ]:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.triple_click(); await el.fill(""); await el.type(end_mm, delay=40); break

            # Submit
            for sel in [
                "button:has-text('Search')","input[type='submit']",
                "button[type='submit']","#searchBtn",".search-btn",
            ]:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click(); break
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_load_state("networkidle", timeout=20000)
            await asyncio.sleep(2)

            html = await page.content()
            recs.extend(parse_html(html, doc_type, cat))

            # Paginate
            for _ in range(30):
                nxt = None
                for ns in [
                    "a[aria-label*='next' i]","button[aria-label*='next' i]",
                    ".next-page a","a:has-text('Next')","button:has-text('Next')",
                    "#nextPage","[data-testid='next-page']",
                ]:
                    el = await page.query_selector(ns)
                    if el and await el.is_visible():
                        dis = await el.get_attribute("disabled")
                        ard = await el.get_attribute("aria-disabled")
                        if not dis and ard != "true":
                            nxt = el; break
                if not nxt: break
                await nxt.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                await asyncio.sleep(1)
                more = parse_html(await page.content(), doc_type, cat)
                if not more: break
                recs.extend(more)

            return recs

        except PWTimeout:
            if attempt < MAX_RETRIES-1: await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"     ✗ {e}"); break

    return recs

# ── Parcel lookup ─────────────────────────────────────────────────────────────
class ParcelLookup:
    URLS = [
        "https://www.bcad.org/clientdb/PropertyExport.zip",
        "https://www.bcad.org/Downloads/PropertyExport.zip",
    ]
    def __init__(self):
        self.idx = {}
        self._load()

    def _load(self):
        if not HAS_DBF: return
        sess = session()
        raw  = None
        for url in self.URLS:
            print(f"  ↓ BCAD: {url}")
            try:
                r = sess.get(url, timeout=120, stream=True); r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(65536): buf.write(chunk)
                raw = buf.getvalue()
                print(f"  ✅ {len(raw):,} bytes"); break
            except Exception as e: print(f"  ✗ {e}")
        if not raw: print("  ⚠  BCAD unavailable"); return
        try:
            zf   = zipfile.ZipFile(io.BytesIO(raw))
            dbfs = sorted([n for n in zf.namelist() if n.lower().endswith(".dbf")],
                          key=lambda n: zf.getinfo(n).file_size, reverse=True)
            if not dbfs: return
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                self._index(os.path.join(tmp, dbfs[0]))
            print(f"  ✅ Parcel index: {len(self.idx):,} entries")
        except Exception as e: print(f"  ✗ {e}")

    def _index(self, path):
        try:
            for row in DBF(path, ignore_missing_memofile=True, encoding="latin-1"):
                r = {k.upper(): safe(v) for k,v in row.items()}
                owner = (r.get("OWNER") or r.get("OWN1") or r.get("OWNERNAME","")).upper().strip()
                if not owner: continue
                p = {
                    "prop_address": r.get("SITE_ADDR") or r.get("SITEADDR",""),
                    "prop_city":    r.get("SITE_CITY",""),
                    "prop_state":   r.get("SITE_STATE","TX"),
                    "prop_zip":     r.get("SITE_ZIP") or r.get("SITEZIP",""),
                    "mail_address": r.get("ADDR_1") or r.get("MAILADR1",""),
                    "mail_city":    r.get("CITY") or r.get("MAILCITY",""),
                    "mail_state":   r.get("STATE") or r.get("MAILSTATE","TX"),
                    "mail_zip":     r.get("ZIP") or r.get("MAILZIP",""),
                }
                for k in self._variants(owner): self.idx.setdefault(k, p)
        except Exception as e: print(f"  ✗ DBF: {e}")

    @staticmethod
    def _variants(n):
        n = re.sub(r"\s+"," ",n).strip().upper(); v = {n}
        if "," in n:
            parts = [p.strip() for p in n.split(",",1)]; v.add(f"{parts[1]} {parts[0]}")
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

# ── Score ─────────────────────────────────────────────────────────────────────
def score(r, cutoff):
    flags=[]; sc=30
    dt=r.get("doc_type",""); a=r.get("amount",0.0); own=(r.get("owner") or "").upper()
    if dt in ("LP","RELLP"):              flags.append("Lis pendens")
    if dt in ("NOFC","TAXDEED"):          flags.append("Pre-foreclosure")
    if dt in ("JUD","CCJ","DRJUD"):       flags.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED"):flags.append("Tax lien")
    if dt == "LNMECH":                    flags.append("Mechanic lien")
    if dt == "LNHOA":                     flags.append("HOA lien")
    if dt == "PRO":                       flags.append("Probate / estate")
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
    print("  Bexar County Motivated Seller Scraper v3")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    print("📦 BCAD parcel data …")
    parcel = ParcelLookup()

    sess = session()
    print("\n🌐 Priming portal session …")
    r0 = get(sess, CLERK_BASE)
    print(f"  Portal: {'✅ reachable' if r0 else '⚠  unreachable'}")

    all_recs = []
    print(f"\n🏛  API/HTML search — {len(DOC_TYPE_MAP)} doc types …\n")
    for dt, label in DOC_TYPE_MAP.items():
        print(f"  [{dt}] {label}")
        recs = api_search(sess, dt, s_iso, e_iso)
        if not recs:
            # HTML fallback
            for params in [
                {"docType":dt,"dateFrom":s_mm,"dateTo":e_mm,"dept":"RP"},
                {"searchTerm":dt,"dateFrom":s_mm,"dateTo":e_mm},
            ]:
                r2 = get(sess, CLERK_BASE+"/results", params=params)
                if r2:
                    recs = parse_html(r2.text, dt, DOC_TYPE_MAP[dt])
                    if recs: break
        print(f"    → {len(recs)} records")
        all_recs.extend(recs)
        time.sleep(0.8)

    print(f"\n  Requests total: {len(all_recs)}")
    print("🎭 Running Playwright for full coverage …")
    pw = await playwright_scrape(list(DOC_TYPE_MAP.keys()), s_mm, e_mm)
    print(f"  Playwright total: {len(pw)}")
    all_recs.extend(pw)

    # Dedup
    seen=set(); unique=[]
    for r in all_recs:
        k=f"{r.get('doc_num','')}|{r.get('doc_type','')}"
        if k and k!="|" and k not in seen:
            seen.add(k); unique.append(r)

    # Filter to date window — keep records in window OR with no date
    in_window=[]
    for r in unique:
        fd=r.get("filed","")
        if not fd or fd>=s_iso: in_window.append(r)

    print(f"\n✅ Unique: {len(unique)}  |  In window: {len(in_window)}")
    unique=in_window

    # Enrich
    with_addr=0
    for r in unique:
        hit=parcel.lookup(r.get("owner",""))
        if hit: r.update(hit); with_addr+=1
        r["flags"],r["score"]=score(r, s_iso)

    unique.sort(key=lambda x:x["score"],reverse=True)
    print(f"   With address: {with_addr}")

    payload={
        "fetched_at":datetime.utcnow().isoformat()+"Z",
        "source":"Bexar County Clerk / BCAD",
        "data_range":f"{s_iso} to {e_iso}",
        "total":len(unique),"with_address":with_addr,
        "records":unique,
    }

    for dest in [DASH_DIR/"records.json", DATA_DIR/"records.json"]:
        dest.write_text(json.dumps(payload,indent=2,default=str))
        print(f"💾 {dest}")

    export_csv(unique, DATA_DIR/"leads.csv")
    print(f"📊 {DATA_DIR/'leads.csv'}")
    print(f"\n🎉 Done — {len(unique)} leads | {with_addr} with address.\n")

if __name__=="__main__":
    asyncio.run(main())
