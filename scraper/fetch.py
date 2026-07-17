"""
Bexar County Motivated Seller Lead Scraper - Final Version
===========================================================
Uses Webshare residential proxies to bypass the portal's
cloud-server block. Runs on GitHub Actions daily.
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

# Proxy config — loaded from environment (GitHub Secrets)
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")

# Webshare US proxies — we try each until one works
PROXY_LIST = [
    ("31.56.127.193",  "7684"),   # US Seattle
    ("198.23.243.226", "6361"),   # US Los Angeles
    ("38.154.185.97",  "6370"),   # US Piscataway
    ("191.96.254.138", "6185"),   # US Los Angeles
]

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

# ── Proxy helpers ─────────────────────────────────────────────────────────────
def get_proxy_url(host, port):
    if PROXY_USER and PROXY_PASS:
        return f"http://{PROXY_USER}:{PROXY_PASS}@{host}:{port}"
    return f"http://{host}:{port}"

def test_proxy(host, port):
    """Test if a proxy can reach the Bexar portal."""
    proxy_url = get_proxy_url(host, port)
    proxies   = {"http": proxy_url, "https": proxy_url}
    try:
        r = requests.get(
            CLERK_BASE, proxies=proxies,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"},
        )
        if r.status_code == 200 and len(r.text) > 100:
            print(f"  ✅ Proxy working: {host}:{port} ({len(r.text)} bytes)")
            return True
        else:
            print(f"  ✗ Proxy {host}:{port} → HTTP {r.status_code}")
            return False
    except Exception as e:
        print(f"  ✗ Proxy {host}:{port} → {e}")
        return False

def find_working_proxy():
    """Try each proxy until one works."""
    print("🔌 Testing proxies …")
    for host, port in PROXY_LIST:
        if test_proxy(host, port):
            return host, port
    print("⚠  No proxy worked — trying without proxy")
    return None, None

# ── HTML parser ────────────────────────────────────────────────────────────────
def parse_html(html, verbose=False):
    recs = []
    if re.search(r"loading results|please wait|host not", html, re.I):
        return []

    soup = BeautifulSoup(html, "lxml")

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2: continue

        hdr_cells = rows[0].find_all(["th","td"])
        hdrs      = [c.get_text(" ",strip=True) for c in hdr_cells]
        hdr_lower = " ".join(hdrs).lower()

        if "grantor" not in hdr_lower and "doc" not in hdr_lower:
            continue

        if verbose:
            print(f"\n  TABLE HEADERS: {hdrs}")

        def fi(*frags):
            for f in frags:
                for i,h in enumerate(hdrs):
                    if f.lower() in h.lower(): return i
            return -1

        IX = {
            "num":  fi("doc number","instrument number","doc #","instrument #"),
            "type": fi("doc type","type"),
            "date": fi("recorded date","filed","recorded"),
            "own":  fi("grantor","owner","seller"),
            "gnt":  fi("grantee","lender","trustee"),
            "leg":  fi("legal description","legal"),
            "addr": fi("property address","situs"),
            "book": fi("book","volume"),
        }

        if verbose:
            print(f"  COLUMN MAP: {IX}")

        for ri, tr in enumerate(rows[1:]):
            cells = tr.find_all(["td","th"])
            if len(cells) < 4: continue

            if verbose and ri == 0:
                print(f"  FIRST ROW: {[c.get_text(' ',strip=True) for c in cells]}")

            def cv(*keys):
                for k in keys:
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

            doc_num = re.sub(r"\s+","",cv("num","book"))
            if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", doc_num): doc_num=""
            if not doc_num:
                for c in cells:
                    t=re.sub(r"\s+","",c.get_text(strip=True))
                    if re.match(r"^\d{6,}$",t) or re.match(r"^\d{4}-\d{4,}$",t):
                        doc_num=t; break
            if not doc_num: continue

            raw_type  = re.sub(r"\s+","",cv("type")).upper()
            doc_type  = raw_type if raw_type else "OTHER"
            cat_label = DOC_TYPE_MAP.get(doc_type, doc_type)

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

# ── Playwright with proxy ──────────────────────────────────────────────────────
async def playwright_scrape(start_mm, end_mm, start_iso, proxy_host, proxy_port):
    from playwright.async_api import async_playwright
    all_recs = []

    # Build proxy config for Playwright
    pw_proxy = None
    if proxy_host and PROXY_USER:
        pw_proxy = {
            "server":   f"http://{proxy_host}:{proxy_port}",
            "username": PROXY_USER,
            "password": PROXY_PASS,
        }
        print(f"\n🌐 Using proxy: {proxy_host}:{proxy_port}")
    else:
        print("\n🌐 Running without proxy")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy=pw_proxy,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900},
            locale="en-US",
        )
        page = await ctx.new_page()

        # Capture JSON API responses
        captured = []
        async def on_resp(resp):
            try:
                ct = resp.headers.get("content-type","")
                if resp.status==200 and "json" in ct:
                    body = await resp.json()
                    items = _items(body)
                    if items:
                        captured.append(items)
                        print(f"  📡 API captured: {len(items)} records")
            except Exception: pass
        page.on("response", on_resp)

        try:
            print("  → Loading Bexar County Clerk portal …")
            resp = await page.goto(CLERK_BASE, timeout=60000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await asyncio.sleep(3)

            body_text = await page.content()
            if "host not" in body_text.lower() or "403" in body_text[:50]:
                print(f"  ✗ Portal blocked this proxy. Status: {resp.status if resp else 'unknown'}")
                return []

            print(f"  ✅ Portal loaded: {await page.title()}")

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
                    await asyncio.sleep(0.5)
                    await page.keyboard.press("Escape")
                print(f"  ✅ Date range set: {start_mm} → {end_mm}")

            # Submit search
            submitted = False
            for sel in ["button[type='submit']","button:has-text('Search')"]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click(); submitted=True; break
                except Exception: pass
            if submitted:
                print("  ✅ Search submitted")

            # Wait for results
            await asyncio.sleep(5)
            for i in range(30):
                html = await page.content()
                if re.search(r"loading results|please wait", html, re.I):
                    await asyncio.sleep(1); continue
                soup  = BeautifulSoup(html,"lxml")
                trows = [r for r in soup.find_all("tr") if len(r.find_all("td"))>=4]
                if trows or captured:
                    print(f"  ✅ Results ready: {len(trows)} rows")
                    break
                await asyncio.sleep(1)

            # Parse API captures
            for items in captured:
                for row in items:
                    if isinstance(row,dict):
                        rec=_api_row(row)
                        if rec: all_recs.append(rec)

            # Parse HTML (verbose on first page)
            html = await page.content()
            html_recs = parse_html(html, verbose=True)
            all_recs.extend(html_recs)
            print(f"\n  Page 1: {len(html_recs)} HTML + "
                  f"{len(all_recs)-len(html_recs)} API = {len(all_recs)} total")

            # Paginate
            page_num = 2
            while page_num <= 200:
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
                            dis = await el.get_attribute("disabled")
                            ard = await el.get_attribute("aria-disabled")
                            cls = await el.get_attribute("class") or ""
                            if not dis and ard!="true" and "disabled" not in cls:
                                nxt=el; break
                    except Exception: pass
                if not nxt:
                    print(f"  ✅ Done — {page_num-1} pages scraped")
                    break

                captured.clear()
                await nxt.click(); await asyncio.sleep(2)
                for _ in range(15):
                    h = await page.content()
                    if not re.search(r"loading|please wait",h,re.I): break
                    await asyncio.sleep(1)

                for items in captured:
                    for row in items:
                        if isinstance(row,dict):
                            rec=_api_row(row)
                            if rec: all_recs.append(rec)

                more = parse_html(await page.content())
                all_recs.extend(more)
                if page_num % 10 == 0:
                    print(f"  Page {page_num}: {len(all_recs)} total")
                page_num += 1

        except Exception as e:
            print(f"\n  ✗ Error: {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return all_recs

def _items(body):
    if isinstance(body,list): return body
    if isinstance(body,dict):
        for k in ["hits","data","results","instruments","records","rows","items","value"]:
            v=body.get(k)
            if isinstance(v,list) and v: return v
            if isinstance(v,dict):
                inner=v.get("hits") or v.get("data") or []
                if inner: return inner
    return []

def _api_row(row):
    try:
        doc_num=safe(row.get("instrumentNumber") or row.get("docNumber") or
                     row.get("id") or row.get("documentNumber") or "")
        if not doc_num: return None
        doc_type=safe(row.get("docType") or row.get("documentType") or "OTHER").upper()
        if doc_type not in TARGET_TYPES:
            doc_type=next((t for t in sorted(TARGET_TYPES,key=len,reverse=True)
                          if doc_type.startswith(t)),doc_type)
        filed=to_iso(row.get("filedDate") or row.get("recordedDate") or "")
        def names(keys):
            for k in keys:
                v=row.get(k)
                if not v: continue
                if isinstance(v,list):
                    parts=[safe(g.get("name") or g.get("fullName") or str(g))
                           for g in v if g]
                    if parts: return "; ".join(p for p in parts if p)
                elif isinstance(v,str) and v.strip(): return v.strip()
            return ""
        rec=blank_rec(doc_type)
        rec.update({
            "doc_num":  doc_num,"filed":filed,
            "owner":    names(["grantors","grantor","seller","debtor"]),
            "grantee":  names(["grantees","grantee","lender","trustee"]),
            "amount":   parse_amt(row.get("consideration") or row.get("amount") or 0),
            "legal":    safe(row.get("legalDescription") or row.get("legal") or ""),
            "clerk_url":f"{CLERK_BASE}/doc/{doc_num}",
        })
        return rec
    except: return None

# ── BCAD Parcel Lookup ─────────────────────────────────────────────────────────
class ParcelLookup:
    def __init__(self):
        self.idx={}; self._load()

    def _load(self):
        import urllib3; urllib3.disable_warnings()
        sess = requests.Session()
        sess.headers["User-Agent"] = "Mozilla/5.0"
        for ep in ["https://opendata.bcad.org/resource/tpvk-6xh3.json",
                   "https://opendata.bcad.org/resource/real-property.json"]:
            try:
                offset=0; limit=50000; loaded=0
                while True:
                    r=sess.get(ep,params={"$limit":limit,"$offset":offset},
                               timeout=60,verify=False)
                    r.raise_for_status(); rows=r.json()
                    if not rows: break
                    for row in rows: self._add(row)
                    loaded+=len(rows)
                    if len(rows)<limit: break
                    offset+=limit; time.sleep(0.3)
                if self.idx:
                    print(f"  ✅ BCAD: {len(self.idx):,} entries ({loaded:,} parcels)")
                    return
            except Exception as e: print(f"  ✗ BCAD Socrata: {e}")

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
                if self.idx:
                    print(f"  ✅ DBF: {len(self.idx):,} entries"); return
            except Exception as e: print(f"  ✗ {e}"); time.sleep(5)
        print("  ⚠  BCAD unavailable")

    def _add(self,row):
        owner=(row.get("owner_name") or row.get("owner") or "").upper().strip()
        if not owner: return
        p={
            "prop_address":row.get("situs_address") or row.get("site_address",""),
            "prop_city":   row.get("situs_city","San Antonio"),
            "prop_zip":    row.get("situs_zip",""),
            "mail_address":row.get("mail_address",""),
            "mail_city":   row.get("mail_city",""),
            "mail_state":  row.get("mail_state","TX"),
            "mail_zip":    row.get("mail_zip",""),
        }
        for k in self._v(owner): self.idx.setdefault(k,p)

    def _dbf(self,path):
        try:
            for row in DBF(path,ignore_missing_memofile=True,encoding="latin-1"):
                r={k.upper():safe(v) for k,v in row.items()}
                owner=(r.get("OWNER") or r.get("OWN1") or "").upper().strip()
                if not owner: continue
                p={
                    "prop_address":r.get("SITE_ADDR") or r.get("SITEADDR",""),
                    "prop_city":   r.get("SITE_CITY","San Antonio"),
                    "prop_zip":    r.get("SITE_ZIP") or r.get("SITEZIP",""),
                    "mail_address":r.get("ADDR_1") or r.get("MAILADR1",""),
                    "mail_city":   r.get("CITY") or r.get("MAILCITY",""),
                    "mail_state":  r.get("STATE","TX"),
                    "mail_zip":    r.get("ZIP") or r.get("MAILZIP",""),
                }
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

    def lookup(self,owner):
        if not owner: return {}
        for k in self._v(owner.upper()):
            h=self.idx.get(k)
            if h: return h
        return {}

# ── Scoring ────────────────────────────────────────────────────────────────────
def score_record(r,cutoff):
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
def export_csv(records,path):
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
    print("  Bexar County Motivated Seller Scraper — Final")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Date window: {s_iso} → {e_iso}")
    print(f"   Proxy user:  {'SET' if PROXY_USER else 'NOT SET'}\n")

    # Find working proxy
    proxy_host, proxy_port = find_working_proxy()

    # Load BCAD
    print("\n📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    # Scrape — try each proxy until we get records
    all_recs = []
    proxies_to_try = [(proxy_host, proxy_port)] if proxy_host else [(None, None)]

    # If first proxy failed, try remaining US proxies
    if not proxy_host:
        proxies_to_try = [(None, None)]
    else:
        used = (proxy_host, proxy_port)
        remaining = [(h,p) for h,p in PROXY_LIST if (h,p) != used]
        proxies_to_try = [(proxy_host, proxy_port)] + remaining

    for ph, pp in proxies_to_try:
        print(f"\n🏛  Scraping via {'proxy '+ph if ph else 'direct connection'} …")
        recs = await playwright_scrape(s_mm, e_mm, s_iso, ph, pp)
        if recs:
            all_recs = recs
            print(f"  ✅ Got {len(recs)} records")
            break
        else:
            print(f"  ✗ No records from this proxy, trying next …")

    print(f"\n  Raw total: {len(all_recs)}")

    # Dedup
    seen=set(); unique=[]
    for r in all_recs:
        k=f"{r['doc_num']}|{r.get('doc_type','')}"
        if k not in seen: seen.add(k); unique.append(r)
    print(f"  After dedup: {len(unique)}")

    # Date filter
    in_window=[]; dropped=0
    for r in unique:
        fd=r.get("filed","")
        if not fd or fd>=s_iso: in_window.append(r)
        else: dropped+=1
    print(f"  In window: {len(in_window)}  |  Old dropped: {dropped}")

    # Enrich
    with_addr=0
    for r in in_window:
        hit=parcel.lookup(r.get("owner",""))
        if hit:
            for f in ["prop_address","prop_city","prop_zip",
                      "mail_address","mail_city","mail_state","mail_zip"]:
                if hit.get(f): r[f]=hit[f]
        if r.get("prop_address"): with_addr+=1
        r["flags"],r["score"]=score_record(r,s_iso)

    in_window.sort(key=lambda x:x["score"],reverse=True)

    # Doc type breakdown
    from collections import Counter
    type_counts=Counter(r["doc_type"] for r in in_window if r["doc_type"] in TARGET_TYPES)
    if type_counts:
        print("\n  Motivated seller breakdown:")
        for dt,cnt in sorted(type_counts.items(),key=lambda x:-x[1]):
            print(f"    {dt:12} {DOC_TYPE_MAP.get(dt,dt):25} {cnt}")

    print(f"\n  With address: {with_addr}")

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
