"""
Bexar County Motivated Seller Lead Scraper - Fixed Type Matching
================================================================
Fix: Portal returns full-word doc types (LISPENDENS, STATETAXLIEN,
MECHANICSLIEN) not short codes (LP, LNCORPTX, LNMECH).
Solution: Map both forms to standard codes before filtering.
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
SHEET_ID      = "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk"
SHEET_NAME    = "Sheet1"

PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")
PROXY_LIST = [
    ("31.56.127.193",  "7684"),
    ("198.23.243.226", "6361"),
    ("38.154.185.97",  "6370"),
    ("191.96.254.138", "6185"),
]

# ── Standard doc type map ──────────────────────────────────────────────────────
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
    "AFFH":     "Affidavit of Heirship",
    "SUBTRST":  "Substitute Trustee",
}

# ── Portal full-word → standard code mapping ───────────────────────────────────
# The portal uses full words; we map them to our standard codes
TYPE_ALIAS = {
    # Lis Pendens variants
    "LISPENDENS":           "LP",
    "LIS PENDENS":          "LP",
    "LP":                   "LP",
    "RELLISPENDENS":        "RELLP",
    "RELEASELISPENDENS":    "RELLP",
    "RELLP":                "RELLP",

    # Foreclosure
    "NOTICEOFFORECLOSURE":  "NOFC",
    "NOFC":                 "NOFC",
    "TAXDEED":              "TAXDEED",
    "TAX DEED":             "TAXDEED",

    # Judgments
    "JUDGMENT":             "JUD",
    "JUD":                  "JUD",
    "CCJ":                  "CCJ",
    "CERTIFIEDJUDGMENT":    "CCJ",
    "DRJUD":                "DRJUD",
    "DOMESTICJUDGMENT":     "DRJUD",

    # Tax/Federal Liens
    "STATETAXLIEN":         "LNCORPTX",
    "STATE TAX LIEN":       "LNCORPTX",
    "CORPTAXLIEN":          "LNCORPTX",
    "LNCORPTX":             "LNCORPTX",
    "IRSLIEN":              "LNIRS",
    "IRS LIEN":             "LNIRS",
    "LNIRS":                "LNIRS",
    "FEDERALLIEN":          "LNFED",
    "FEDERAL LIEN":         "LNFED",
    "LNFED":                "LNFED",
    "RELEASEOFSTATETAXLIEN":"LNCORPTX",  # release — still useful signal

    # Mechanic / HOA Liens
    "MECHANICLIEN":         "LNMECH",
    "MECHANIC LIEN":        "LNMECH",
    "MECHANICSLIEN":        "LNMECH",
    "LNMECH":               "LNMECH",
    "HOALIEN":              "LNHOA",
    "HOA LIEN":             "LNHOA",
    "LNHOA":                "LNHOA",
    "HOSPITALLIEN":         "LNHOA",   # hospital = medical lien

    # Medicaid
    "MEDICAIDLIEN":         "MEDLN",
    "MEDLN":                "MEDLN",

    # Probate — many portal variants
    "PROBATE":                      "PRO",
    "PRO":                          "PRO",
    "PROBATEDOCUMENT":              "PRO",
    "PROBATEDOC":                   "PRO",
    "LETTERSTESTAMENTARY":          "PRO",
    "LETTERSOFADMINISTRATION":      "PRO",
    "WILLPROBATE":                  "PRO",

    # Affidavit of Heirship — estate/inheritance signal
    "AFFIDAVITOFHEIRSHIP":          "AFFH",
    "AFFIDAVIT OF HEIRSHIP":        "AFFH",
    "AFFH":                         "AFFH",
    "HEIRSHIPAFFIDAVIT":            "AFFH",
    "AFFIDAVITOFHEIRSHIPS":         "AFFH",
    "HEIRSHIP":                     "AFFH",
    "AFFIDAVITOFHEIRS":             "AFFH",

    # Substitute Trustee — strong pre-foreclosure signal
    "SUBSTITUTETRUSTEE":            "SUBTRST",
    "SUBSTITUTE TRUSTEE":           "SUBTRST",
    "SUBTRST":                      "SUBTRST",
    "SUBSTITUTIONOFTRUSTEE":        "SUBTRST",
    "SUBSTITUTION OF TRUSTEE":      "SUBTRST",
    "APPOINTMENTOFSUBTRUSTEEE":     "SUBTRST",
    "APPOINTMENTOFSUBSTITUTE":      "SUBTRST",
    "SUBTRUSTEE":                   "SUBTRST",

    # General lien
    "LIEN":                         "LN",
    "LN":                           "LN",

    # Notice of commencement
    "NOTICEOFCOMMENCEMENT":         "NOC",
    "NOC":                          "NOC",
    "NOTICE":                       "NOC",
}

TARGET_TYPES = set(DOC_TYPE_MAP.keys())

SHEET_HEADERS = [
    "First Name","Last Name",
    "Mailing Address","Mailing City","Mailing State","Mailing Zip",
    "Property Address","Property City","Property State","Property Zip",
    "Lead Type","Document Type","Date Filed","Document Number",
    "Amount/Debt Owed","Seller Score","Motivated Seller Flags",
    "Source","Public Records URL",
    "Status","Partner Notes","Date Contacted",
]

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

def normalise_type(raw):
    """
    Convert any portal doc type string to our standard code.
    Returns (standard_code, cat_label, is_motivated_seller)
    """
    if not raw: return "OTHER", "Other", False
    # Strip spaces, uppercase
    key = re.sub(r"\s+","",raw.strip().upper())
    # Direct alias lookup
    code = TYPE_ALIAS.get(key)
    if not code:
        # Try partial match — portal sometimes adds suffixes
        for alias_key, alias_code in TYPE_ALIAS.items():
            if key.startswith(alias_key) or alias_key.startswith(key):
                code = alias_code
                break
    if not code:
        code = key  # keep as-is
    label = DOC_TYPE_MAP.get(code, raw.title())
    is_motivated = code in TARGET_TYPES
    return code, label, is_motivated

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

def split_name(full):
    if not full: return "",""
    if "," in full:
        p=[x.strip().title() for x in full.split(",",1)]; return p[1],p[0]
    p=full.strip().title().split()
    if len(p)==1: return "",p[0]
    return p[0]," ".join(p[1:])

# ── Scoring ────────────────────────────────────────────────────────────────────
def score_record(r, cutoff):
    flags=[]; sc=30
    dt  = r.get("doc_type","")
    amt = r.get("amount",0.0)
    own = (r.get("owner") or "").upper()
    fd  = r.get("filed","")

    if dt=="LP":
        flags.append("Lis pendens"); sc+=20
    if dt=="RELLP":
        flags.append("Lis pendens"); sc+=5
    if dt in ("NOFC","TAXDEED"):
        flags.append("Pre-foreclosure"); sc+=20
    if dt in ("JUD","CCJ","DRJUD"):
        flags.append("Judgment lien"); sc+=15
    if dt in ("LNCORPTX","LNIRS","LNFED"):
        flags.append("Tax lien"); sc+=15
    if dt=="LNMECH":
        flags.append("Mechanic lien"); sc+=10
    if dt=="LNHOA":
        flags.append("HOA lien"); sc+=10
    if dt=="MEDLN":
        flags.append("Medicaid lien"); sc+=10
    if dt=="PRO":
        flags.append("Probate / estate"); sc+=10
    if dt=="NOC":
        flags.append("Notice of commencement"); sc+=5
    if dt=="LN":
        flags.append("General lien"); sc+=8
    if dt=="AFFH":
        flags.append("Affidavit of heirship"); sc+=18  # estate = very motivated
    if dt=="SUBTRST":
        flags.append("Substitute trustee"); sc+=22  # imminent foreclosure signal
    if "Lis pendens" in flags and "Pre-foreclosure" in flags:
        sc+=20
    if "Substitute trustee" in flags and "Lis pendens" in flags:
        sc+=15  # trustee + lis pendens = foreclosure imminent
    if any(k in own for k in ("LLC","INC","CORP","LTD","TRUST")):
        flags.append("LLC / corp owner"); sc+=10
    if amt>100_000: sc+=15
    elif amt>50_000: sc+=10
    elif amt>10_000: sc+=5
    if fd and fd>=cutoff:
        flags.append("New this week"); sc+=5
    if r.get("prop_address") and r["prop_address"] not in ("N/A",""):
        sc+=5
    return flags, min(sc,100)

# ── Proxy ──────────────────────────────────────────────────────────────────────
def get_proxy_url(host,port):
    if PROXY_USER and PROXY_PASS:
        return f"http://{PROXY_USER}:{PROXY_PASS}@{host}:{port}"
    return f"http://{host}:{port}"

def find_working_proxy():
    print("🔌 Testing proxies …")
    for host,port in PROXY_LIST:
        proxies={"http":get_proxy_url(host,port),"https":get_proxy_url(host,port)}
        try:
            r=requests.get(CLERK_BASE,proxies=proxies,timeout=15,
                headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"})
            if r.status_code==200 and len(r.text)>100:
                print(f"  ✅ {host}:{port}"); return host,port
            else: print(f"  ✗ {host}:{port} → {r.status_code}")
        except Exception as e: print(f"  ✗ {host}:{port} → {e}")
    return None,None

# ── HTML parser ────────────────────────────────────────────────────────────────
def parse_html(html, verbose=False):
    recs=[]
    if re.search(r"loading results|please wait|host not",html,re.I): return []
    soup=BeautifulSoup(html,"lxml")
    for tbl in soup.find_all("table"):
        rows=tbl.find_all("tr")
        if len(rows)<2: continue
        hdr_cells=rows[0].find_all(["th","td"])
        hdrs=[c.get_text(" ",strip=True) for c in hdr_cells]
        hdr_lower=" ".join(hdrs).lower()
        if "grantor" not in hdr_lower and "doc" not in hdr_lower: continue
        if verbose: print(f"\n  HEADERS: {hdrs}")
        def fi(*frags):
            for f in frags:
                for i,h in enumerate(hdrs):
                    if f.lower() in h.lower(): return i
            return -1
        IX={"num":fi("doc number","instrument number","doc #","instrument #"),
            "type":fi("doc type","type"),"date":fi("recorded date","filed","recorded"),
            "own":fi("grantor","owner","seller"),"gnt":fi("grantee","lender","trustee"),
            "leg":fi("legal description","legal"),"addr":fi("property address","situs"),
            "book":fi("book","volume")}
        if verbose: print(f"  MAP: {IX}")
        for ri,tr in enumerate(rows[1:]):
            cells=tr.find_all(["td","th"])
            if len(cells)<4: continue
            if verbose and ri==0:
                raw=[c.get_text(" ",strip=True) for c in cells]
                print(f"  ROW0: {raw}")
            def cv(*keys):
                for k in keys:
                    i=IX.get(k,-1)
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
            doc_num=re.sub(r"\s+","",cv("num","book"))
            if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$",doc_num): doc_num=""
            if not doc_num:
                for c in cells:
                    t=re.sub(r"\s+","",c.get_text(strip=True))
                    if re.match(r"^\d{6,}$",t) or re.match(r"^\d{4}-\d{4,}$",t):
                        doc_num=t; break
            if not doc_num: continue
            raw_type=cv("type")
            code,label,is_motivated=normalise_type(raw_type)
            rec=blank_rec(code,label)
            rec.update({
                "doc_num":      doc_num,
                "raw_type":     raw_type,  # keep original for debug
                "filed":        to_iso(cv("date")),
                "owner":        cv("own"),
                "grantee":      cv("gnt"),
                "legal":        cv("leg"),
                "prop_address": cv("addr"),
                "clerk_url":    any_link(),
                "is_motivated": is_motivated,
            })
            recs.append(rec)
        if recs: break
    return recs

# ── Playwright ─────────────────────────────────────────────────────────────────
async def playwright_scrape(start_mm,end_mm,start_iso,proxy_host,proxy_port):
    from playwright.async_api import async_playwright
    all_recs=[]
    pw_proxy=None
    if proxy_host and PROXY_USER:
        pw_proxy={"server":f"http://{proxy_host}:{proxy_port}",
                  "username":PROXY_USER,"password":PROXY_PASS}
        print(f"\n🌐 Proxy: {proxy_host}:{proxy_port}")
    async with async_playwright() as p:
        browser=await p.chromium.launch(
            headless=True,proxy=pw_proxy,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"])
        ctx=await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900},locale="en-US")
        page=await ctx.new_page()
        captured=[]
        async def on_resp(resp):
            try:
                ct=resp.headers.get("content-type","")
                if resp.status==200 and "json" in ct:
                    body=await resp.json()
                    items=_items(body)
                    if items: captured.append(items); print(f"  📡 API: {len(items)}")
            except Exception: pass
        page.on("response",on_resp)
        try:
            await page.goto(CLERK_BASE,timeout=60000)
            await page.wait_for_load_state("networkidle",timeout=30000)
            await asyncio.sleep(3)
            body_text=await page.content()
            if "host not" in body_text.lower():
                print("  ✗ Portal blocked"); return []
            print(f"  ✅ {await page.title()}")
            for txt in ["Close","Accept","I Agree","Continue","OK"]:
                try:
                    btn=page.get_by_role("button",name=re.compile(f"^{txt}$",re.I))
                    if await btn.count()>0 and await btn.first.is_visible():
                        await btn.first.click(); await asyncio.sleep(1.5)
                        print(f"  ✅ Dismissed: {txt}"); break
                except Exception: pass
            date_inputs=await page.query_selector_all(".react-datepicker__input")
            if len(date_inputs)>=2:
                for di,val in [(0,start_mm),(1,end_mm)]:
                    await date_inputs[di].click()
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Delete")
                    await date_inputs[di].fill(val)
                    await asyncio.sleep(0.5)
                    await page.keyboard.press("Escape")
                print(f"  ✅ Dates: {start_mm} → {end_mm}")
            for sel in ["button[type='submit']","button:has-text('Search')"]:
                try:
                    el=await page.query_selector(sel)
                    if el and await el.is_visible(): await el.click(); break
                except Exception: pass
            await asyncio.sleep(5)
            for _ in range(30):
                html=await page.content()
                if re.search(r"loading results|please wait",html,re.I):
                    await asyncio.sleep(1); continue
                soup=BeautifulSoup(html,"lxml")
                trows=[r for r in soup.find_all("tr") if len(r.find_all("td"))>=4]
                if trows or captured: print(f"  ✅ {len(trows)} rows ready"); break
                await asyncio.sleep(1)
            for items in captured:
                for row in items:
                    if isinstance(row,dict):
                        rec=_api_row(row)
                        if rec: all_recs.append(rec)
            html=await page.content()
            html_recs=parse_html(html,verbose=True)
            all_recs.extend(html_recs)
            print(f"  Page 1: {len(html_recs)} HTML + {len(all_recs)-len(html_recs)} API")

            # Print sample of doc types found for debug
            if html_recs:
                from collections import Counter
                raw_types=Counter(r.get("raw_type","?") for r in html_recs[:50])
                print(f"  Sample types: {dict(raw_types.most_common(10))}")

            page_num=2
            while page_num<=200:
                nxt=None
                for ns in [
                    "button[aria-label*='next' i]:not([disabled])",
                    "a[aria-label*='next' i]",
                    "li.next:not(.disabled) a",
                    "button:has-text('›'):not([disabled])",
                ]:
                    try:
                        el=await page.query_selector(ns)
                        if el and await el.is_visible():
                            dis=await el.get_attribute("disabled")
                            ard=await el.get_attribute("aria-disabled")
                            cls=await el.get_attribute("class") or ""
                            if not dis and ard!="true" and "disabled" not in cls:
                                nxt=el; break
                    except Exception: pass
                if not nxt: print(f"  ✅ Done — {page_num-1} pages"); break
                captured.clear()
                await nxt.click(); await asyncio.sleep(2)
                for _ in range(15):
                    h=await page.content()
                    if not re.search(r"loading|please wait",h,re.I): break
                    await asyncio.sleep(1)
                for items in captured:
                    for row in items:
                        if isinstance(row,dict):
                            rec=_api_row(row)
                            if rec: all_recs.append(rec)
                more=parse_html(await page.content())
                all_recs.extend(more)
                if page_num%10==0: print(f"  Page {page_num}: {len(all_recs)}")
                page_num+=1
        except Exception as e:
            print(f"\n  ✗ {e}")
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
        raw_type=safe(row.get("docType") or row.get("documentType") or "")
        code,label,is_motivated=normalise_type(raw_type)
        filed=to_iso(row.get("filedDate") or row.get("recordedDate") or "")
        def names(keys):
            for k in keys:
                v=row.get(k)
                if not v: continue
                if isinstance(v,list):
                    parts=[safe(g.get("name") or g.get("fullName") or str(g)) for g in v if g]
                    if parts: return "; ".join(p for p in parts if p)
                elif isinstance(v,str) and v.strip(): return v.strip()
            return ""
        rec=blank_rec(code,label)
        rec.update({
            "doc_num":      doc_num,
            "raw_type":     raw_type,
            "filed":        filed,
            "owner":        names(["grantors","grantor","seller","debtor"]),
            "grantee":      names(["grantees","grantee","lender","trustee"]),
            "amount":       parse_amt(row.get("consideration") or row.get("amount") or 0),
            "legal":        safe(row.get("legalDescription") or row.get("legal") or ""),
            "clerk_url":    f"{CLERK_BASE}/doc/{doc_num}",
            "is_motivated": is_motivated,
        })
        return rec
    except: return None

# ── BCAD ───────────────────────────────────────────────────────────────────────
class ParcelLookup:
    def __init__(self): self.idx={}; self._load()
    def _load(self):
        import urllib3; urllib3.disable_warnings()
        sess=requests.Session(); sess.headers["User-Agent"]="Mozilla/5.0"
        for ep in ["https://opendata.bcad.org/resource/tpvk-6xh3.json",
                   "https://opendata.bcad.org/resource/real-property.json"]:
            try:
                offset=0; limit=50000; loaded=0
                while True:
                    r=sess.get(ep,params={"$limit":limit,"$offset":offset},timeout=60,verify=False)
                    r.raise_for_status(); rows=r.json()
                    if not rows: break
                    for row in rows: self._add(row)
                    loaded+=len(rows)
                    if len(rows)<limit: break
                    offset+=limit; time.sleep(0.3)
                if self.idx: print(f"  ✅ BCAD: {len(self.idx):,} entries"); return
            except Exception as e: print(f"  ✗ BCAD: {e}")
        if not HAS_DBF: print("  ⚠  No parcel data"); return
        for url in ["https://www.bcad.org/clientdb/PropertyExport.zip",
                    "https://www.bcad.org/Downloads/PropertyExport.zip"]:
            try:
                time.sleep(3); r=sess.get(url,timeout=120,stream=True); r.raise_for_status()
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
    def _add(self,row):
        owner=(row.get("owner_name") or row.get("owner") or "").upper().strip()
        if not owner: return
        p={"prop_address":row.get("situs_address") or row.get("site_address",""),
           "prop_city":row.get("situs_city","San Antonio"),
           "prop_zip":row.get("situs_zip",""),
           "mail_address":row.get("mail_address",""),
           "mail_city":row.get("mail_city",""),
           "mail_state":row.get("mail_state","TX"),
           "mail_zip":row.get("mail_zip","")}
        for k in self._v(owner): self.idx.setdefault(k,p)
    def _dbf(self,path):
        try:
            for row in DBF(path,ignore_missing_memofile=True,encoding="latin-1"):
                r={k.upper():safe(v) for k,v in row.items()}
                owner=(r.get("OWNER") or r.get("OWN1") or "").upper().strip()
                if not owner: continue
                p={"prop_address":r.get("SITE_ADDR") or r.get("SITEADDR",""),
                   "prop_city":r.get("SITE_CITY","San Antonio"),
                   "prop_zip":r.get("SITE_ZIP") or r.get("SITEZIP",""),
                   "mail_address":r.get("ADDR_1") or r.get("MAILADR1",""),
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
    def lookup(self,owner):
        if not owner: return {}
        for k in self._v(owner.upper()):
            h=self.idx.get(k)
            if h: return h
        return {}

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
            fn,ln=split_name(r.get("owner",""))
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

# ── Google Sheets ──────────────────────────────────────────────────────────────
def push_to_sheets(records):
    print("\n📊 Pushing to Google Sheets …")
    creds_json=os.environ.get("GOOGLE_CREDENTIALS","")
    if not creds_json: print("  ⚠  GOOGLE_CREDENTIALS not set"); return
    try:
        import google.oauth2.service_account as sa
        import googleapiclient.discovery as discovery
        creds=sa.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
        svc=discovery.build("sheets","v4",credentials=creds,cache_discovery=False)
        sheet=svc.spreadsheets()
        result=sheet.values().get(spreadsheetId=SHEET_ID,
                                   range=f"{SHEET_NAME}!A:V").execute()
        existing=result.get("values",[])
        existing_nums=set()
        for row in existing[1:]:
            if len(row)>13 and row[13]: existing_nums.add(str(row[13]).strip())
        print(f"  Existing: {len(existing)-1 if existing else 0}")
        new_recs=[r for r in records
                  if str(r.get("doc_num","")).strip() not in existing_nums]
        print(f"  New to add: {len(new_recs)}")
        if not new_recs: print("  ✅ Up to date"); return
        def make_row(r):
            fn,ln=split_name(r.get("owner",""))
            return [fn,ln,
                r.get("mail_address",""),r.get("mail_city",""),
                r.get("mail_state","TX"),r.get("mail_zip",""),
                r.get("prop_address",""),r.get("prop_city","San Antonio"),
                r.get("prop_state","TX"),r.get("prop_zip",""),
                r.get("cat_label",""),r.get("doc_type",""),
                r.get("filed",""),r.get("doc_num",""),
                str(r.get("amount","")),str(r.get("score",0)),
                " | ".join(r.get("flags",[])),
                "Bexar County Clerk",r.get("clerk_url",""),
                "","",""]
        new_rows=[make_row(r) for r in new_recs]
        if not existing:
            sheet.values().update(
                spreadsheetId=SHEET_ID,range=f"{SHEET_NAME}!A1",
                valueInputOption="RAW",
                body={"values":[SHEET_HEADERS]+new_rows}).execute()
        else:
            sheet.batchUpdate(spreadsheetId=SHEET_ID,body={"requests":[{
                "insertDimension":{
                    "range":{"sheetId":0,"dimension":"ROWS",
                             "startIndex":1,"endIndex":1+len(new_rows)},
                    "inheritFromBefore":False}}]}).execute()
            sheet.values().update(
                spreadsheetId=SHEET_ID,range=f"{SHEET_NAME}!A2",
                valueInputOption="RAW",body={"values":new_rows}).execute()
        print(f"  ✅ {len(new_rows)} leads added to top of sheet")
        print(f"  🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")
    except Exception as e:
        print(f"  ✗ Sheets: {e}")
        import traceback; traceback.print_exc()

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    print("="*60)
    print("  Bexar County Motivated Seller Scraper")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)
    s_mm,e_mm   = date_range_mm()
    s_iso,e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    proxy_host,proxy_port = find_working_proxy()

    print("\n📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    all_recs=[]
    proxies_to_try=[]
    if proxy_host:
        proxies_to_try.append((proxy_host,proxy_port))
        proxies_to_try+=[(h,p) for h,p in PROXY_LIST if (h,p)!=(proxy_host,proxy_port)]
    else:
        proxies_to_try=[(None,None)]

    for ph,pp in proxies_to_try:
        print(f"\n🏛  Scraping via {'proxy '+ph if ph else 'direct'} …")
        recs=await playwright_scrape(s_mm,e_mm,s_iso,ph,pp)
        if recs: all_recs=recs; print(f"  ✅ {len(recs)} raw records"); break
        else: print("  ✗ Trying next …")

    # Dedup
    seen=set(); unique=[]
    for r in all_recs:
        k=f"{r['doc_num']}|{r.get('doc_type','')}"
        if k not in seen: seen.add(k); unique.append(r)

    # Date filter
    in_window=[]
    for r in unique:
        fd=r.get("filed","")
        if not fd or fd>=s_iso: in_window.append(r)

    print(f"\n  Raw: {len(all_recs)} | Unique: {len(unique)} | In window: {len(in_window)}")

    # Enrich + score all
    motivated=[]
    for r in in_window:
        hit=parcel.lookup(r.get("owner",""))
        if hit:
            for f in ["prop_address","prop_city","prop_zip",
                      "mail_address","mail_city","mail_state","mail_zip"]:
                if hit.get(f): r[f]=hit[f]
        r["flags"],r["score"]=score_record(r,s_iso)
        if r.get("is_motivated",False):
            motivated.append(r)

    motivated.sort(key=lambda x:x["score"],reverse=True)
    in_window.sort(key=lambda x:x["score"],reverse=True)

    # Print doc type breakdown
    from collections import Counter
    all_types=Counter(r.get("raw_type","?") for r in in_window)
    motivated_types=Counter(r["doc_type"] for r in motivated)
    print(f"\n  All doc types found (top 15):")
    for t,n in all_types.most_common(15):
        code,label,is_m=normalise_type(t)
        mark="✅" if is_m else "✗"
        print(f"    {mark} {t:25} → {code:12} {n}")
    print(f"\n  Motivated seller types:")
    for dt,cnt in sorted(motivated_types.items(),key=lambda x:-x[1]):
        print(f"    {dt:12} {DOC_TYPE_MAP.get(dt,dt):25} {cnt}")

    with_addr=sum(1 for r in motivated if r.get("prop_address") and r["prop_address"]!="N/A")
    high_score =[r for r in motivated if r["score"]>=70]
    heirship   =[r for r in motivated if r["doc_type"]=="AFFH"]
    sub_trustee=[r for r in motivated if r["doc_type"]=="SUBTRST"]
    print(f"\n  ✅ Motivated leads:       {len(motivated)}")
    print(f"  ✅ High score (70+):      {len(high_score)}")
    print(f"  ✅ With address:          {with_addr}")
    print(f"  ✅ Affidavit of Heirship: {len(heirship)}")
    print(f"  ✅ Substitute Trustee:    {len(sub_trustee)}")

    # Save
    payload={
        "fetched_at":   datetime.utcnow().isoformat()+"Z",
        "source":       "Bexar County Clerk / BCAD",
        "data_range":   f"{s_iso} to {e_iso}",
        "total":        len(motivated),
        "with_address": with_addr,
        "records":      motivated,
    }
    for dest in [DASH_DIR/"records.json",DATA_DIR/"records.json"]:
        dest.write_text(json.dumps(payload,indent=2,default=str))
        print(f"💾 {dest}")

    export_csv(in_window,     DATA_DIR/"leads.csv")
    export_csv(motivated,     DATA_DIR/"motivated_leads.csv")
    print(f"📊 leads.csv ({len(in_window)}) | motivated_leads.csv ({len(motivated)})")

    push_to_sheets(motivated)

    print(f"\n🎉 Done — {len(motivated)} motivated leads | {len(high_score)} high score (70+).\n")

if __name__=="__main__":
    asyncio.run(main())
