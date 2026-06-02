"""
Bexar County Motivated Seller Lead Scraper v9
==============================================
Root cause found from logs:
  - 3,850 records scraped across 77 pages ✅
  - API interception = 0 (portal renders pure HTML, no XHR JSON)
  - Unique: 1 = doc_num dedup collapsing all rows to same key
  
Fix: Print actual HTML table structure to find correct columns,
     use row index as fallback unique key, 
     extract doc_type from table data not just header matching.
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

# ── HTML parser — debug-first approach ───────────────────────────────────────
def parse_html(html, debug=False):
    """
    Parse results from the Bexar portal HTML.
    On first call prints column structure for debugging.
    """
    recs = []
    if re.search(r"loading results|please wait", html, re.I):
        return []
    soup = BeautifulSoup(html, "lxml")

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2: continue

        # Get headers
        hdrs = []
        header_row = rows[0]
        for th in header_row.find_all(["th","td"]):
            hdrs.append(th.get_text(" ", strip=True))

        if len(hdrs) < 2: continue

        if debug:
            print(f"  TABLE COLUMNS ({len(hdrs)}): {hdrs}")

        # Map column names to indices
        def find_idx(*frags):
            for frag in frags:
                for i,h in enumerate(hdrs):
                    if frag.lower() in h.lower():
                        return i
            return -1

        idx_docnum  = find_idx("instrument","doc #","doc#","book","number","docnum","ref #")
        idx_type    = find_idx("type","doc type","instrument type")
        idx_date    = find_idx("date","filed","recorded","entry date")
        idx_grantor = find_idx("grantor","owner","seller","debtor","party 1")
        idx_grantee = find_idx("grantee","lender","trustee","plaintiff","party 2")
        idx_amount  = find_idx("amount","consideration","value","debt")
        idx_legal   = find_idx("legal","description","subdivision")

        if debug:
            print(f"  MAPPED: docnum={idx_docnum} type={idx_type} "
                  f"date={idx_date} grantor={idx_grantor} "
                  f"grantee={idx_grantee} amount={idx_amount}")

        for row_i, tr in enumerate(rows[1:]):
            cells = tr.find_all(["td","th"])
            if len(cells) < 2: continue

            def cell(idx, *fallback_frags):
                """Get cell text by index, fallback to fragment search."""
                if idx >= 0 and idx < len(cells):
                    t = cells[idx].get_text(" ", strip=True)
                    if t: return t
                # Fallback: search all cells for fragment
                for frag in fallback_frags:
                    for c in cells:
                        t = c.get_text(" ", strip=True)
                        if frag.lower() in t.lower() and len(t) < 100:
                            return t
                return ""

            def cell_link(idx):
                """Get href from cell."""
                if idx >= 0 and idx < len(cells):
                    a = cells[idx].find("a")
                    if a and a.get("href"):
                        h = a["href"]
                        return h if h.startswith("http") else CLERK_BASE + h
                # Try any link in row
                for c in cells:
                    a = c.find("a")
                    if a and a.get("href"):
                        h = a["href"]
                        return h if h.startswith("http") else CLERK_BASE + h
                return ""

            # ── Extract doc number ────────────────────────────────────────
            doc_num = cell(idx_docnum)
            if not doc_num:
                # Try every cell — look for one that looks like a doc number
                # Bexar doc numbers are typically numeric or YYYY-XXXXXXX format
                for c in cells:
                    t = c.get_text(strip=True)
                    if re.match(r"\d{4,}", t) or re.match(r"\d{4}-\d+", t):
                        doc_num = t
                        break
            if not doc_num:
                # Last resort: use first non-empty cell
                for c in cells:
                    t = c.get_text(strip=True)
                    if t and not re.search(r"loading|please wait", t, re.I):
                        doc_num = t
                        break

            if not doc_num:
                continue

            # Skip rows that are clearly not records
            if re.search(r"^(loading|please wait|no records|showing|page \d)", 
                        doc_num, re.I):
                continue

            # ── Extract doc type ──────────────────────────────────────────
            raw_type = cell(idx_type).upper().strip()
            # Clean up: remove spaces, get just the code
            raw_type = re.sub(r"\s+","",raw_type)
            doc_type = raw_type if raw_type in TARGET_TYPES else ""
            if not doc_type:
                # Try prefix match
                doc_type = next((t for t in TARGET_TYPES 
                                if raw_type.startswith(t)), "LN")

            # ── Extract date ──────────────────────────────────────────────
            filed = to_iso(cell(idx_date))

            # ── Extract names ─────────────────────────────────────────────
            owner   = cell(idx_grantor)
            grantee = cell(idx_grantee)

            # ── Extract amount ────────────────────────────────────────────
            amount = parse_amt(cell(idx_amount))

            # ── Extract legal ─────────────────────────────────────────────
            legal = cell(idx_legal)

            # ── Get URL ───────────────────────────────────────────────────
            clerk_url = cell_link(idx_docnum) or cell_link(0) or CLERK_BASE

            rec = blank_rec(doc_type)
            rec.update({
                "doc_num":   doc_num,
                "filed":     filed,
                "owner":     owner,
                "grantee":   grantee,
                "amount":    amount,
                "legal":     legal,
                "clerk_url": clerk_url,
            })
            recs.append(rec)

        # Only use first valid table
        if recs:
            break

    return recs

# ── Playwright scraper ─────────────────────────────────────────────────────────
async def playwright_scrape(start_mm, end_mm, start_iso):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    all_recs = []
    print("\n🎭 Playwright: single broad date-range search …")

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
                await date_inputs[0].click()
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Delete")
                await date_inputs[0].fill(start_mm)
                await asyncio.sleep(0.4)
                await page.keyboard.press("Escape")
                print(f"  ✅ Date FROM: {start_mm}")

                await date_inputs[1].click()
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Delete")
                await date_inputs[1].fill(end_mm)
                await asyncio.sleep(0.4)
                await page.keyboard.press("Escape")
                print(f"  ✅ Date TO: {end_mm}")

            # Submit broad search (empty search box = all records in date range)
            print("  → Submitting …")
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
                if trows:
                    print(f"  ✅ Results ready: {len(trows)} rows")
                    break
                await asyncio.sleep(1)

            # Parse first page WITH debug to see column structure
            html = await page.content()
            page1_recs = parse_html(html, debug=True)
            all_recs.extend(page1_recs)
            print(f"  Page 1: {len(page1_recs)} records parsed")

            # Show sample of what was parsed
            if page1_recs:
                sample = page1_recs[0]
                print(f"  SAMPLE RECORD:")
                print(f"    doc_num  = '{sample['doc_num']}'")
                print(f"    doc_type = '{sample['doc_type']}'")
                print(f"    filed    = '{sample['filed']}'")
                print(f"    owner    = '{sample['owner']}'")
                print(f"    grantee  = '{sample['grantee']}'")
                print(f"    amount   = '{sample['amount']}'")
                print(f"    clerk_url= '{sample['clerk_url']}'")

            # Paginate — cap at 100 pages (5,000 records max)
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
                if page_num % 10 == 0:
                    print(f"  Page {page_num}: total so far = {len(all_recs)}")
                page_num += 1

        except Exception as e:
            print(f"\n  ✗ {e}")
            import traceback; traceback.print_exc()
        finally:
            await browser.close()

    return all_recs

# ── BCAD Parcel Lookup ─────────────────────────────────────────────────────────
class ParcelLookup:
    def __init__(self):
        self.idx = {}
        self._load()

    def _load(self):
        sess = make_session()
        # Try Socrata (skip SSL verify since cert hostname mismatch)
        for ep in [
            "https://opendata.bcad.org/resource/tpvk-6xh3.json",
            "https://opendata.bcad.org/resource/real-property.json",
        ]:
            print(f"  ↓ BCAD Socrata: {ep}")
            try:
                offset=0; limit=50000; loaded=0
                while True:
                    r = sess.get(ep,
                                 params={"$limit":limit,"$offset":offset},
                                 timeout=60, verify=False)
                    r.raise_for_status()
                    rows = r.json()
                    if not rows: break
                    for row in rows: self._idx_row(row)
                    loaded += len(rows)
                    if len(rows)<limit: break
                    offset+=limit; time.sleep(0.3)
                if self.idx:
                    print(f"  ✅ Socrata: {len(self.idx):,} entries")
                    return
            except Exception as e:
                print(f"  ✗ Socrata: {e}")

        # Bulk DBF fallback
        if not HAS_DBF: print("  ⚠  No parcel data"); return
        for url in [
            "https://www.bcad.org/clientdb/PropertyExport.zip",
            "https://www.bcad.org/Downloads/PropertyExport.zip",
        ]:
            print(f"  ↓ BCAD bulk: {url}")
            try:
                time.sleep(3)
                r = sess.get(url,timeout=120,stream=True); r.raise_for_status()
                buf=io.BytesIO()
                for chunk in r.iter_content(65536): buf.write(chunk)
                raw=buf.getvalue(); print(f"  ✅ {len(raw):,} bytes")
                zf=zipfile.ZipFile(io.BytesIO(raw))
                dbfs=sorted([n for n in zf.namelist() if n.lower().endswith(".dbf")],
                            key=lambda n:zf.getinfo(n).file_size,reverse=True)
                if not dbfs: continue
                with tempfile.TemporaryDirectory() as tmp:
                    zf.extractall(tmp); self._idx_dbf(os.path.join(tmp,dbfs[0]))
                print(f"  ✅ DBF: {len(self.idx):,} entries"); return
            except Exception as e: print(f"  ✗ {e}"); time.sleep(5)
        print("  ⚠  BCAD unavailable")

    def _idx_row(self, row):
        owner=(row.get("owner_name") or row.get("owner") or
               row.get("ownername") or "").upper().strip()
        if not owner: return
        p={
            "prop_address":row.get("situs_address") or row.get("site_address",""),
            "prop_city":   row.get("situs_city")    or row.get("site_city",""),
            "prop_state":  "TX",
            "prop_zip":    row.get("situs_zip")     or row.get("site_zip",""),
            "mail_address":row.get("mail_address",""),
            "mail_city":   row.get("mail_city",""),
            "mail_state":  row.get("mail_state","TX"),
            "mail_zip":    row.get("mail_zip",""),
        }
        for k in self._variants(owner): self.idx.setdefault(k,p)

    def _idx_dbf(self, path):
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
    print("  Bexar County Motivated Seller Scraper v9")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("="*60)

    s_mm,  e_mm  = date_range_mm()
    s_iso, e_iso = date_range_iso()
    print(f"\n📅 Window: {s_iso} → {e_iso}\n")

    print("📦 Loading BCAD parcel data …")
    parcel = ParcelLookup()

    print("\n🏛  Scraping portal …")
    all_recs = await playwright_scrape(s_mm, e_mm, s_iso)
    print(f"\n  Total raw records: {len(all_recs)}")

    # ── Dedup on doc_num + doc_type ────────────────────────────────────────
    seen=set(); unique=[]
    for r in all_recs:
        # Use doc_num + filed + owner as key to avoid false dedup
        k = f"{r.get('doc_num','').strip()}|{r.get('filed','')}|{r.get('owner','')[:20]}"
        if k and k not in seen:
            seen.add(k); unique.append(r)

    print(f"  After dedup: {len(unique)}")

    # ── Filter: keep only target doc types ────────────────────────────────
    targeted = [r for r in unique if r.get("doc_type") in TARGET_TYPES]
    print(f"  Target doc types only: {len(targeted)}")

    # ── Date filter: keep in window OR undated ─────────────────────────────
    in_window=[]; dropped=0
    for r in targeted:
        fd=r.get("filed","")
        if not fd or fd>=s_iso: in_window.append(r)
        else: dropped+=1

    print(f"  In window / undated: {len(in_window)}  |  Dropped old: {dropped}")

    # ── Enrich + score ─────────────────────────────────────────────────────
    with_addr=0
    for r in in_window:
        hit=parcel.lookup(r.get("owner",""))
        if hit: r.update(hit); with_addr+=1
        r["flags"],r["score"]=score_record(r,s_iso)

    in_window.sort(key=lambda x:x["score"],reverse=True)
    print(f"  With address: {with_addr}")

    # ── Save ───────────────────────────────────────────────────────────────
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
