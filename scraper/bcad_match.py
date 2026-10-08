"""
BCAD Property Ownership Matcher
================================
Cross-matches inmate names against Bexar County Appraisal District (BCAD)
property records to identify inmates who own property in Bexar County.

INPUT:  data/inmate_leads.csv  (output of inmates.py)
OUTPUT: data/inmate_property_matches.csv  (inmates with confirmed property)
        data/inmate_records.json  (updated with property match data)

NOTE: Property match ONLY — no skip trace, no contact info.
      Skip trace requires separate green-light from Maverick.

Uses Playwright + Webshare proxies (same setup as fetch.py).
BCAD search: https://bexar.trueautomation.com/clientdb/?cid=110
"""

import asyncio, csv, json, os, time, re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BCAD_URL    = "https://bexar.trueautomation.com/clientdb/?cid=110"
DATA_DIR    = Path(__file__).parent.parent / "data"

PROXY_USER  = os.environ.get("PROXY_USER", "")
PROXY_PASS  = os.environ.get("PROXY_PASS", "")
PROXY_LIST  = [
    ("31.56.127.193",  "7684"),
    ("198.23.243.226", "6361"),
    ("38.154.185.97",  "6370"),
    ("191.96.254.138", "6185"),
]

# How many top leads to match (saves time + proxy usage)
# Set to None to process all 8500+
MAX_LEADS   = int(os.environ.get("BCAD_MAX_LEADS", "500"))

# Delay between searches (seconds) — be polite to BCAD
SEARCH_DELAY = 1.5


def load_inmate_leads():
    """Load previously scored inmate leads."""
    csv_path = DATA_DIR / "inmate_leads.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"No inmate_leads.csv found at {csv_path}")

    leads = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            leads.append(row)

    # Sort by score descending so we check highest-value leads first
    leads.sort(key=lambda r: int(r.get("score", 0)), reverse=True)

    if MAX_LEADS:
        leads = leads[:MAX_LEADS]

    return leads


def format_search_name(last, first):
    """Format name for BCAD owner search: 'SMITH JOHN'"""
    last  = last.strip().upper()
    first = first.strip().upper().split()[0] if first.strip() else ""
    if first:
        return f"{last} {first}"
    return last


async def search_bcad_owner(page, last_name, first_name, timeout=20000):
    """
    Search BCAD for a property owner by name.
    Returns list of property dicts found, or empty list.
    """
    search_name = format_search_name(last_name, first_name)
    results = []

    try:
        # Navigate to BCAD search (or reuse open tab)
        if "trueautomation.com" not in page.url:
            await page.goto(BCAD_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1000)

        # Clear and fill Owner Name field
        # Field is identified by label "Owner Name"
        owner_field = page.locator('input[name*="OwnerName"], input[id*="OwnerName"], input[placeholder*="Smith"]').first
        await owner_field.wait_for(timeout=10000)
        await owner_field.triple_click()
        await owner_field.fill(search_name)

        # Clear other fields that might interfere
        for sel in ['input[name*="StreetNum"]', 'input[name*="StreetName"]', 'input[name*="PropID"]']:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill("")
            except Exception:
                pass

        # Submit the search
        search_btn = page.locator('input[type="submit"][value*="Search"], input[type="button"][value*="Search"], a:has-text("Search")').first
        await search_btn.click()
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(500)

        # Parse results table
        # BCAD results table has columns: Property ID, Owner Name, Address, Legal Desc
        rows = await page.locator("table.SearchResults tr, table#searchResults tr, table tr").all()

        for row in rows:
            cells = await row.locator("td").all()
            if len(cells) < 3:
                continue

            cell_texts = []
            for cell in cells:
                cell_texts.append((await cell.inner_text()).strip())

            # Skip header rows
            if not cell_texts[0] or cell_texts[0].lower() in ("property id", "prop id", "account"):
                continue

            # Look for rows that have a property ID (numeric) in first column
            prop_id = cell_texts[0].strip()
            if not re.match(r'^\d{6,}', prop_id.replace("-", "").replace(" ", "")):
                continue

            # Verify owner name roughly matches our inmate
            owner_cell = cell_texts[1] if len(cell_texts) > 1 else ""
            address_cell = cell_texts[2] if len(cell_texts) > 2 else ""

            # Basic name match check
            last_upper = last_name.upper()
            if last_upper not in owner_cell.upper():
                continue

            results.append({
                "prop_id":    prop_id,
                "owner_name": owner_cell,
                "prop_address": address_cell,
                "legal_desc": cell_texts[3] if len(cell_texts) > 3 else "",
            })

        # If no table rows parsed, check if page says "No results"
        page_text = await page.inner_text("body")
        if "no results" in page_text.lower() or "no records" in page_text.lower():
            return []

    except PWTimeout:
        print(f"    ⏱ Timeout searching: {search_name}")
    except Exception as e:
        print(f"    ✗ Error searching {search_name}: {e}")

    return results


async def run_bcad_match(leads):
    """Run BCAD property search for all leads using Playwright."""

    matched   = []
    no_match  = []
    errors    = []

    proxy_idx = 0
    proxy_host, proxy_port = PROXY_LIST[proxy_idx]

    print(f"\n🔍 Searching BCAD for {len(leads)} inmates …")
    print(f"   Proxy: {proxy_host}:{proxy_port}")
    print(f"   Est. time: ~{int(len(leads) * SEARCH_DELAY / 60)} min\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy={
                "server":   f"http://{proxy_host}:{proxy_port}",
                "username": PROXY_USER,
                "password": PROXY_PASS,
            },
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # Initial load
        try:
            print("  Loading BCAD search portal …")
            await page.goto(BCAD_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            print("  ✅ Portal loaded\n")
        except Exception as e:
            print(f"  ✗ Failed to load BCAD: {e}")
            await browser.close()
            return matched, no_match, errors

        for i, lead in enumerate(leads):
            last  = lead.get("last_name", "").strip()
            first = lead.get("first_name", "").strip()
            score = lead.get("score", "0")

            if not last:
                continue

            search_name = format_search_name(last, first)
            props = await search_bcad_owner(page, last, first)

            if props:
                print(f"  ✅ [{i+1}/{len(leads)}] {search_name} → {len(props)} propert{'y' if len(props)==1 else 'ies'} found")
                for p in props:
                    matched.append({
                        **lead,
                        "bcad_match":      "YES",
                        "prop_id":         p["prop_id"],
                        "bcad_owner_name": p["owner_name"],
                        "prop_address":    p["prop_address"],
                        "legal_desc":      p["legal_desc"],
                        "match_date":      datetime.utcnow().strftime("%Y-%m-%d"),
                    })
            else:
                no_match.append({**lead, "bcad_match": "NO"})
                if (i + 1) % 50 == 0:
                    print(f"  … [{i+1}/{len(leads)}] checked | {len(matched)} matches so far")

            # Polite delay
            await asyncio.sleep(SEARCH_DELAY)

            # Rotate proxy every 200 searches to avoid blocks
            if (i + 1) % 200 == 0:
                proxy_idx = (proxy_idx + 1) % len(PROXY_LIST)
                proxy_host, proxy_port = PROXY_LIST[proxy_idx]
                print(f"\n  🔄 Rotating to proxy {proxy_host}:{proxy_port}\n")
                await browser.close()
                browser = await pw.chromium.launch(
                    headless=True,
                    proxy={
                        "server":   f"http://{proxy_host}:{proxy_port}",
                        "username": PROXY_USER,
                        "password": PROXY_PASS,
                    },
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    ignore_https_errors=True,
                )
                page = await context.new_page()
                await page.goto(BCAD_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)

        await browser.close()

    return matched, no_match, errors


def save_match_results(matched, total_checked):
    """Save property match results to CSV and update JSON."""

    # CSV of matches only
    match_csv = DATA_DIR / "inmate_property_matches.csv"
    fieldnames = [
        "last_name", "first_name", "tdcj_num", "sid_num",
        "facility", "offense", "sentence_date", "projected_release",
        "sentence_years", "score", "flags",
        "bcad_match", "prop_id", "bcad_owner_name",
        "prop_address", "legal_desc", "match_date",
    ]
    with open(match_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(matched)
    print(f"\n  💾 {match_csv} ({len(matched)} property matches)")

    # Update inmate_records.json with match stats
    json_path = DATA_DIR / "inmate_records.json"
    if json_path.exists():
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        data["bcad_match_run"]    = datetime.utcnow().isoformat() + "Z"
        data["bcad_checked"]      = total_checked
        data["bcad_matches"]      = len(matched)
        data["bcad_match_rate"]   = f"{len(matched)/total_checked*100:.1f}%" if total_checked else "0%"
        # Embed matched records
        data["property_matches"]  = matched
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  💾 {json_path} (updated with match data)")

    return match_csv


def push_matches_to_sheets(matched):
    """
    Push property-matched inmates to a new 'Inmate Property Matches' tab.
    NOTE: No contact info — property address + score only.
    Skip trace requires separate authorization.
    """
    print("\n📊 Pushing property matches to Google Sheets …")
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        print("  ⚠  GOOGLE_CREDENTIALS not set — skipping")
        return

    SHEET_ID   = "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk"
    MATCH_TAB  = "Inmate Property Matches"
    HEADERS    = [
        "Last Name", "First Name", "TDCJ #",
        "Offense", "Sentence Yrs", "Projected Release",
        "Inmate Score", "Flags",
        "BCAD Prop ID", "Property Address",
        "BCAD Owner Name (on deed)", "Legal Description",
        "Match Date",
        # ── SKIP TRACE COLUMNS (empty until green-lit) ──
        "⛔ Phone (pending)", "⛔ Mailing Addr (pending)",
        # ── WORKFLOW COLUMNS ──
        "Status", "Partner Notes", "Date Contacted",
    ]

    try:
        import google.oauth2.service_account as sa
        import googleapiclient.discovery as discovery

        creds = sa.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        svc   = discovery.build("sheets", "v4", credentials=creds, cache_discovery=False)
        sheet = svc.spreadsheets()

        # Ensure tab exists
        meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
        existing_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

        if MATCH_TAB not in existing_tabs:
            print(f"  Creating '{MATCH_TAB}' tab …")
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": MATCH_TAB}}}]},
            ).execute()
            meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()

        match_sheet_id = None
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == MATCH_TAB:
                match_sheet_id = s["properties"]["sheetId"]
                break

        # Fetch existing to dedup by TDCJ + prop_id
        result   = sheet.values().get(
            spreadsheetId=SHEET_ID,
            range=f"'{MATCH_TAB}'!A:R"
        ).execute()
        existing = result.get("values", [])
        existing_keys = set()
        for row in existing[1:]:
            if len(row) >= 3:
                existing_keys.add(f"{row[2]}_{row[8] if len(row) > 8 else ''}")

        new_recs = [
            r for r in matched
            if f"{r['tdcj_num']}_{r.get('prop_id','')}" not in existing_keys
        ]
        print(f"  Existing rows: {len(existing)-1 if existing else 0}")
        print(f"  New to add: {len(new_recs)}")

        if not new_recs:
            print("  ✅ Sheet up to date")
            return

        def make_row(r):
            return [
                r.get("last_name", ""),
                r.get("first_name", ""),
                r.get("tdcj_num", ""),
                r.get("offense", ""),
                r.get("sentence_years", ""),
                r.get("projected_release", ""),
                str(r.get("score", "")),
                r.get("flags", ""),
                r.get("prop_id", ""),
                r.get("prop_address", ""),
                r.get("bcad_owner_name", ""),
                r.get("legal_desc", ""),
                r.get("match_date", ""),
                "PENDING AUTHORIZATION",  # Phone placeholder
                "PENDING AUTHORIZATION",  # Mailing placeholder
                "", "", "",               # Status, Notes, Contacted
            ]

        new_rows = [make_row(r) for r in new_recs]

        if not existing:
            sheet.values().update(
                spreadsheetId=SHEET_ID,
                range=f"'{MATCH_TAB}'!A1",
                valueInputOption="RAW",
                body={"values": [HEADERS] + new_rows},
            ).execute()
            print(f"  ✅ Created '{MATCH_TAB}' with {len(new_rows)} records")
        else:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"insertDimension": {
                    "range": {
                        "sheetId":    match_sheet_id,
                        "dimension":  "ROWS",
                        "startIndex": 1,
                        "endIndex":   1 + len(new_rows),
                    },
                    "inheritFromBefore": False,
                }}]},
            ).execute()
            sheet.values().update(
                spreadsheetId=SHEET_ID,
                range=f"'{MATCH_TAB}'!A2",
                valueInputOption="RAW",
                body={"values": new_rows},
            ).execute()
            print(f"  ✅ {len(new_rows)} property matches added")

        print(f"  🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")

    except Exception as e:
        print(f"  ✗ Sheets error: {e}")
        import traceback; traceback.print_exc()


async def main():
    print("=" * 60)
    print("  BCAD Property Ownership Matcher")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)
    print(f"\n  ⛔ SKIP TRACE: NOT ENABLED — property match only")
    print(f"     Contact info requires separate authorization.\n")

    if not PROXY_USER or not PROXY_PASS:
        print("⚠  PROXY_USER / PROXY_PASS not set — BCAD will likely block")
        print("   Set these env vars or the search will fail.\n")

    leads = load_inmate_leads()
    print(f"📋 Loaded {len(leads)} inmate leads to check")
    print(f"   (Top {MAX_LEADS} by score)" if MAX_LEADS else "   (All leads)")

    matched, no_match, errors = await run_bcad_match(leads)

    total_checked = len(matched) + len(no_match)
    match_rate    = len(matched) / total_checked * 100 if total_checked else 0

    print(f"\n{'='*60}")
    print(f"  Results:")
    print(f"  Checked:    {total_checked}")
    print(f"  Matched:    {len(matched)}  ({match_rate:.1f}%)")
    print(f"  No match:   {len(no_match)}")
    print(f"{'='*60}")

    if matched:
        print(f"\n🏆 Top 5 property matches:")
        for r in matched[:5]:
            print(f"   [{r['score']}] {r['last_name']}, {r['first_name']}"
                  f" → {r['prop_address']}")

    save_match_results(matched, total_checked)

    if matched:
        push_matches_to_sheets(matched)

    print(f"\n✅ Done — {len(matched)} property matches found.")


if __name__ == "__main__":
    asyncio.run(main())
