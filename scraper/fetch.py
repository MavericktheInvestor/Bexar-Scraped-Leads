"""
Bexar County Motivated Seller Lead Scraper v12
===============================================
Critical fixes:
1. Don't discard unknown doc types — keep ALL records, label unknown ones
2. Parser confirmed working (50 rows found) — just type filter was too strict
3. Show ALL records on dashboard, flag the motivated seller types prominently
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
    if re.match(r"^[-/]+$", raw): return ""          # --/--/-- placeholder
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

def blank_rec(doc_type, cat_label=None):
    return {
        "doc_num":"","doc_type":doc_type,
        "cat_label": cat_label or DOC_TYPE_MAP.get(doc_type, doc_type),
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
    })
    return s

# ── HTML parser ────────────────────────────────────────────────────────────────
def parse_html(html, verbose=False):
    """
    Parse portal HTML. Keeps ALL records regardless of doc type.
    Motivated seller types are flagged in scoring, not filtered here.
    """
    recs = []
    if re.search(r"loading results|please wait", html, re.I):
        return []

    soup = BeautifulSoup(html, "lxml")

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2: continue

        hdr_row   = rows[0]
        hdr_cells = hdr_row.find_all(["th","td"])
        hdrs      = [c.get_text(" ", strip=True) for c in hdr_cells]
        hdr_lower = " ".join(hdrs).lower()

        # Must be the results table
        if "grantor" not in hdr_lower and "doc" not in hdr_lower:
            continue

        if verbose:
            print(f"  HEADERS ({len(hdrs)}): {hdrs}")

        # Find column indices by header name
        def idx(*frags):
            for frag in frags:
                for i, h in enumerate(hdrs):
                    if frag.lower() in h.lower():
                        return i
            return -1

        I_docnum   = idx("doc number","instrument number","doc #","docnum","instrument #")
        I_type     = idx("doc type","type","instrument type")
        I_date     = idx("recorded date","filed date","date recorded","entry date","recorded")
        I_grantor  = idx("grantor","owner","seller","debtor")
        I_grantee  = idx("grantee","lender","trustee","plaintiff")
        I_legal    = idx("legal description","legal")
        I_propaddr = idx("property address","prop address","situs address")
        I_book     = idx("book","volume","book/volume")

        if verbose:
            print(f"  INDICES: docnum={I_docnum} type={I_type} "
                  f"date={I_date} grantor={I_grantor} "
                  f"grantee={I_grantee} propaddr={I_propaddr}")

        for tr in rows[1:]:
            cells = tr.find_all(["td","th"])
            if len(cells) < 4: continue

            def cv(i, *also):
                """Get non-empty cell text."""
                for j in ([i] + list(also)):
                    if j >= 0 and j < len(cells):
                        t = cells[j].get_text(" ", strip=True)
                        if t and not re.match(r"^[-\s/]+$", t):
                            return t
                return ""

            def cl():
                """Get first meaningful link from row."""
                for c in cells:
                    for a in c.find_all("a"):
                        h = a.get("href","")
                        if h:
                            return h if h.startswith("http") else CLERK_BASE+h
                return CLERK_BASE

            # ── Extract doc number ─────────────────────────────────────────
            doc_num = cv(I_docnum, I_book)
            doc_num = re.sub(r"\s+","",doc_num)

            # If looks like a date, skip
            if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", doc_num):
                doc_num = ""

            # Try scanning all cells for numeric doc number pattern
            if not doc_num:
                for c in cells:
                    t = re.sub(r"\s+","",c.get_text(strip=True))
                    if re.match(r"^\d{7,}$",t) or re.match(r"^\d{4}-\d{5,}$",t):
                        doc_num = t; break

            if not doc_num: continue

            # ── Doc type — keep even if unknown ───────────────────────────
            raw_type  = re.sub(r"\s+","", cv(I_type)).upper()
            # Match against known types (prefix match)
            doc_type  = raw_type if raw_type in TARGET_TYPES else ""
            if not doc_type:
                doc_type = next(
                    (t for t in sorted(TARGET_TYPES, key=len, reverse=True)
                     if raw_type.startswith(t)),
                    raw_type or "OTHER"   # Keep as-is, don't discard
                )

            cat_label = DOC_TYPE_MAP.get(doc_type, doc_type)

            # ── Other fields ───────────────────────────────────────────────
            filed     = to_iso(cv(I_date))
            owner     = cv(I_grantor)
            grantee   = cv(I_grantee)
            legal     = cv(I_legal)
            prop_addr = cv(I_propaddr)
            clerk_url = cl()

            rec = blank_rec(doc_type, cat_label)
            rec.update({
                "doc_num":      doc_num,
                "filed":        filed,
                "owner":        owner,
                "grantee":      grantee,
                "legal":        legal,
                "clerk_url":    clerk_url,
                "prop_address": prop_addr,
            })
            recs.append(rec)

        if recs:
            if verbose and recs:
                s = recs[0]
                print(f"  SAMPLE: num='{s['doc_num']}' "
                      f"type='{s['doc_type']}' "
                      f"filed='{s['filed']}' "
                      f"owner='{s['owner'][:50]}' "
                      f"addr='{s['prop_address'][:50]}'")
            break

    return recs

# ── Playwright ─────────────────────────────────────────────────────────────────
async def playwright_scrape(start_mm, end_mm, start_iso):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
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

            # Wait for results
            await asyncio.sleep(4)
            for i in range(20):
                html = await page.content()
                if re.search(r"loading results|please wait", html, re.I):
                    await asyncio.sleep(1); continue
                soup  = BeautifulSoup(html,"lxml")
                trows = [r for r in soup.find_all("tr") if len(r.find_all("td"))>=4]
                if trows: print(f"  ✅ {len(trows)} rows"); break
                await asyncio.sleep(1)

            # Page 1 verbose
            html = await page.content()
            p1   = parse_html(html, verbose=True)
            all_recs.extend(p1)
            print(f"  Page 1: {len(p1)} records")

            # Paginate
            page_num = 2
            while page_num <= 100:
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
                if not nxt: print(f"  Done at page {page_num-1}"); break

                await nxt.click(); await asyncio.sleep(2)
                for _ in range(10):
                    h = await page.content()
                    if not re.search(r"loading|please wait",h,re.I): break
                    await asyncio.sleep(1)

                more = parse_html(await page.content())
                if not more: break
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
                offset=0; limit=50000; loaded=0
                while True:
                    r=sess.get(ep,params={"$limit":limit,"$offset":offset},
                               timeout=60,verify=False)
                    r.raise_for_status()
                    rows=r.json()
                    if not rows: break
                    for row in rows: self._add(row)
                    loaded+=len(rows)
                    if len(rows)<limit: break
                    offset+=limit; time.sleep(0.3)
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
                print(f"  ✅ DBF: {len(self.idx):,} entries"); return
            except Exception as e: print(f"  ✗ {e}"); time.sleep(5)
        print("  ⚠  BCAD unavailable")

    def _add(self, row):
        owner=(row.get("owner_name") or row.get("owner") or "").upper().strip()
        if not owner: return
        p={
            "mail_address":row.get("mail_address",""),
            "mail_city":   row.get("mail_city",""),
            "mail_state":  row.get("mail_state","TX"),
            "mail_zip":    row.get("mail_zip",""),
        }
        for k in self._v(owner): self.idx.setdefault(k,p)

    def _dbf(self, path):
        try:
            for row in DBF(path,ignore_missing_memofile=True,encoding="latin-1"):
                r={k.upper():safe(v) for k,v in row.items()}
                owner=(r.get("OWNER") or r.get("OWN1") or "").upper().strip()
                if not owner: continue
                p={
                    "mail_address":r.get("ADDR_1") or r.get("MAILADR1",""),
                    "mail_city":   r.get("CITY")   or r.get("MAILCITY",""),
                    "mail_state":  r.get("STATE","TX"),
                    "mail_zip":    r.get("ZIP")    or r.get("MAILZIP",""),
                }
                for k in self._v(owner): self.idx.setdefault(k,p)
        except Exception as e: print(f"  ✗ DBF: {e}")

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
    print("  Bexar County Motivated Seller Scraper v12")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    print("\n🏛  Scraping portal …")
    all_recs = await playwright_scrape(s_mm, e_mm, s_iso)
    print(f"\n  Raw records: {len(all_recs)}")

    # Dedup
    seen=set(); unique=[]
    for r in all_recs:
        k=f"{r['doc_num']}|{r['doc_type']}"
        if k not in seen: seen.add(k); unique.append(r)
    print(f"  After dedup: {len(unique)}")

    # Date filter — keep in window or undated
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

    # Report doc type breakdown
    from collections import Counter
    type_counts = Counter(r["doc_type"] for r in in_window)
    print("\n  Doc type breakdown:")
    for dt, cnt in sorted(type_counts.items(), key=lambda x:-x[1])[:15]:
        label = DOC_TYPE_MAP.get(dt, dt)
        print(f"    {dt:12} {label:25} {cnt}")

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
