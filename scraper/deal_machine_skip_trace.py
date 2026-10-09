"""
Deal Machine Skip Trace
=======================
Reads:  data/skip_trace_queue.csv   (built by skip_trace_queue.py)
Action: Calls Deal Machine Enrich-by-Name API for each lead that
        has no phone yet, writes phones back to the CSV and to
        the Google Sheets "Skip Trace Queue" tab.

⚠️  DO NOT RUN until Maverick gives explicit green-light.
    The workflow is gated: the GitHub Actions job only runs when
    triggered with inputs.green_light = "YES_RUN_SKIP_TRACE".

Env vars required:
  DM_API_KEY          — Deal Machine API key (dm_sk_live_...)
  GOOGLE_CREDENTIALS  — Service-account JSON for sheet updates

Run modes:
  python deal_machine_skip_trace.py --dry-run   # estimate credits only
  python deal_machine_skip_trace.py             # live run (green-light only)
"""

import csv, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
DM_API_KEY   = os.environ.get("DM_API_KEY", "")
DM_API_BASE  = "https://api.v2.dealmachine.com/v1"
RATE_LIMIT   = 0.5   # seconds between API calls (conservative)
BATCH_SIZE   = 10    # people to enrich per API call (max 250 per request)

DATA_DIR   = Path(__file__).parent.parent / "data"
QUEUE_CSV  = DATA_DIR / "skip_trace_queue.csv"
OUTPUT_CSV = DATA_DIR / "skip_traced.csv"

SHEET_ID   = "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk"
SHEET_NAME = "Skip Trace Queue"

# Column indices in skip_trace_queue.csv (0-based after DictReader — use keys)
PHONE1_KEY  = "Phone 1"
PHONE2_KEY  = "Phone 2"
STATUS_KEY  = "Skip Trace Status"
DATE_KEY    = "Skip Trace Date"
DOCNUM_KEY  = "Document Number"

# ── DM API helpers ─────────────────────────────────────────────────────────────

def dm_headers():
    return {
        "Authorization": f"Bearer {DM_API_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def dm_post(path, body):
    import urllib.request
    url  = f"{DM_API_BASE}{path}"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers=dm_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), resp.status
    except Exception as e:
        if hasattr(e, "read"):
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {getattr(e, 'code', '?')}: {err_body}") from e
        raise


def dm_get(path, params=None):
    import urllib.request, urllib.parse
    url = f"{DM_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=dm_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ── Skip trace logic ───────────────────────────────────────────────────────────

def enrich_by_name(first, last, address=None, city=None, state="TX", zip_=None):
    """
    Deal Machine Enrich-by-Name endpoint.
    Returns list of phone dicts: [{number, type, do_not_call, carrier}, ...]
    Costs 1 credit per person returned.
    """
    body = {
        "first_name": first,
        "last_name":  last,
        "fields": ["phones", "emails"],
    }
    # Adding address tightens the match significantly
    if address:
        body["address"] = address
    if city:
        body["city"] = city
    if state:
        body["state"] = state
    if zip_:
        body["zip"] = zip_

    result, status = dm_post("/enrichment/name", body)
    data = result.get("data", {})
    phones = data.get("phones", [])
    credits_used = result.get("credits", {}).get("used", 0)
    return phones, credits_used


def enrich_by_address(address, city, state="TX", zip_=None):
    """
    Deal Machine Enrich-by-Address endpoint.
    Returns owner + phones for a property address.
    Fallback when name enrichment returns nothing.
    """
    body = {
        "address": address,
        "city":    city,
        "state":   state,
        "fields":  ["owner", "contacts"],
    }
    if zip_:
        body["zip"] = zip_

    result, status = dm_post("/enrichment/address", body)
    data   = result.get("data", {})
    # contacts is array of {dm_person_id, full_name, phones, ...}
    contacts      = data.get("contacts", [])
    credits_used  = result.get("credits", {}).get("used", 0)

    # Flatten phones from all contacts
    all_phones = []
    for c in contacts:
        all_phones.extend(c.get("phones", []))
    return all_phones, credits_used


def select_best_phones(phones, max_phones=2):
    """
    Rank phones: wireless > voip > landline > unknown.
    Filter out DNC numbers.
    Return up to max_phones E.164-formatted strings.
    """
    ORDER = {"wireless": 0, "mobile": 1, "voip": 2, "landline": 3, "unknown": 4}
    # Keep non-DNC only; sort by type preference
    candidates = [p for p in phones if not p.get("do_not_call", False)]
    candidates.sort(key=lambda p: ORDER.get(p.get("type", "unknown"), 4))

    results = []
    for p in candidates[:max_phones]:
        num = str(p.get("number", "")).strip()
        if len(num) == 10 and num.isdigit():
            num = f"({num[:3]}) {num[3:6]}-{num[6:]}"
        results.append(num)
    return results


def estimate_credits(leads):
    """Dry-run: count leads needing enrichment, print credit estimate."""
    need = [r for r in leads if not r.get(PHONE1_KEY, "").strip()
            and (r.get("first") or r.get("First Name") or
                 r.get("last")  or r.get("Last Name"))]
    print(f"\n📊 Dry-run estimate:")
    print(f"   Total in queue:       {len(leads)}")
    print(f"   Already have phones:  {len(leads) - len(need)}")
    print(f"   Need skip trace:      {len(need)}")
    print(f"   Estimated credits:    ~{len(need)} (1 per person)")
    print(f"\n   ⚠️  Run without --dry-run to execute.")


# ── Google Sheets update ───────────────────────────────────────────────────────

def update_sheet_phones(phone_map):
    """
    phone_map: {doc_num: {"phone1": str, "phone2": str, "status": "Done"}}
    Updates the Skip Trace Queue sheet columns Phone 1, Phone 2, Status, Date.
    Sheet columns (1-based, matching SHEET_HEADERS in skip_trace_queue.py):
      S=19 Phone 1, T=20 Phone 2, Q=17 Status, U=21 Date
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json or not phone_map:
        return

    try:
        import google.oauth2.service_account as sa
        import googleapiclient.discovery as discovery

        creds = sa.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        svc   = discovery.build("sheets", "v4", credentials=creds, cache_discovery=False)
        sheet = svc.spreadsheets()

        result = sheet.values().get(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_NAME}'!A:U"
        ).execute()
        rows = result.get("values", [])

        updates = []
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for i, row in enumerate(rows[1:], start=2):
            doc_num = row[15].strip() if len(row) > 15 else ""
            if doc_num in phone_map:
                info = phone_map[doc_num]
                # Q17 = Status, S19 = Phone1, T20 = Phone2, U21 = Date
                updates += [
                    {"range": f"'{SHEET_NAME}'!Q{i}", "values": [[info.get("status", "Done")]]},
                    {"range": f"'{SHEET_NAME}'!S{i}", "values": [[info.get("phone1", "")]]},
                    {"range": f"'{SHEET_NAME}'!T{i}", "values": [[info.get("phone2", "")]]},
                    {"range": f"'{SHEET_NAME}'!U{i}", "values": [[today]]},
                ]

        if updates:
            sheet.values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": updates}
            ).execute()
            print(f"  ✅ Updated {len(phone_map)} leads in sheet")
    except Exception as e:
        print(f"  ⚠  Sheet update failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("  Deal Machine Skip Trace")
    print(f"  {datetime.now(timezone.utc).isoformat()} UTC")
    if dry_run:
        print("  MODE: DRY RUN (no credits consumed)")
    print("=" * 60)

    # Gate check — must be explicitly unlocked
    if not dry_run and os.environ.get("SKIP_TRACE_GREEN_LIGHT", "") != "YES_RUN_SKIP_TRACE":
        print("\n🔒 BLOCKED: Skip trace is gated.")
        print("   Set env var SKIP_TRACE_GREEN_LIGHT=YES_RUN_SKIP_TRACE to run.")
        print("   Or use --dry-run to estimate credits without consuming any.")
        sys.exit(0)

    if not dry_run and not DM_API_KEY:
        print("❌ DM_API_KEY not set")
        sys.exit(1)

    # Load queue
    if not QUEUE_CSV.exists():
        print(f"❌ {QUEUE_CSV} not found — run skip_trace_queue.py first")
        sys.exit(1)

    with open(QUEUE_CSV, newline="", encoding="utf-8-sig") as f:
        leads = list(csv.DictReader(f))

    print(f"\n📂 Queue: {len(leads)} leads loaded")

    if dry_run:
        estimate_credits(leads)
        return

    # Live run
    skipped = enriched = failed = no_phones = 0
    total_credits = 0
    phone_map = {}  # doc_num -> {phone1, phone2, status}

    # Output CSV — copy of queue with phones filled in
    # Add phone/status columns if not already present
    fieldnames = list(leads[0].keys()) if leads else []
    for col in [PHONE1_KEY, PHONE2_KEY, STATUS_KEY, DATE_KEY]:
        if col not in fieldnames:
            fieldnames.append(col)
    for r in leads:
        for col in [PHONE1_KEY, PHONE2_KEY, STATUS_KEY, DATE_KEY]:
            r.setdefault(col, "")

    def get(r, *keys):
        """Try multiple key spellings, return first non-empty value."""
        for k in keys:
            v = r.get(k, "").strip()
            if v:
                return v
        return ""

    print("\n🔍 Enriching leads …")
    for i, r in enumerate(leads, 1):
        # Handle both snake_case (queue CSV) and Title Case (enriched CSV)
        first = get(r, "first", "First Name")
        last  = get(r, "last",  "Last Name")

        # Skip already traced
        if r.get(PHONE1_KEY, "").strip():
            skipped += 1
            continue

        if not first and not last:
            skipped += 1
            continue

        try:
            # Try name enrichment first
            phones, credits = enrich_by_name(
                first, last,
                address = get(r, "mail_address", "Mailing Address",
                                 "prop_address",  "Property Address"),
                city    = get(r, "mail_city",    "Mailing City",
                                 "prop_city",     "Property City"),
                state   = get(r, "mail_state",   "Mailing State",
                                 "prop_state",    "Property State") or "TX",
                zip_    = get(r, "mail_zip",     "Mailing Zip",
                                 "prop_zip",      "Property Zip"),
            )
            total_credits += credits

            # Fallback: enrich by property address
            if not phones and prop_addr:
                phones, credits2 = enrich_by_address(
                    prop_addr,
                    get(r, "prop_city", "Property City") or "San Antonio",
                    get(r, "prop_state", "Property State") or "TX",
                    get(r, "prop_zip", "Property Zip") or None,
                )
                total_credits += credits2

            best = select_best_phones(phones, max_phones=2)
            p1 = best[0] if len(best) > 0 else ""
            p2 = best[1] if len(best) > 1 else ""

            r[PHONE1_KEY] = p1
            r[PHONE2_KEY] = p2
            r[STATUS_KEY] = "Done" if p1 else "No Phone"
            r[DATE_KEY]   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            doc_num = get(r, "doc_num", "Document Number")
            if doc_num:
                phone_map[doc_num] = {"phone1": p1, "phone2": p2,
                                       "status": r[STATUS_KEY]}

            if p1:
                enriched += 1
                if i <= 10 or i % 100 == 0:
                    print(f"  [{i}] ✅ {last}, {first} → {p1}")
            else:
                no_phones += 1
                if i <= 10 or i % 100 == 0:
                    print(f"  [{i}] 📵 {last}, {first} — no phone found")

        except Exception as e:
            failed += 1
            print(f"  [{i}] ✗ {last}, {first}: {e}")
            r[STATUS_KEY] = "Error"

        time.sleep(RATE_LIMIT)

    # Write enriched CSV
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(leads)

    print(f"\n✅ Done:")
    print(f"   Enriched (phone found):  {enriched}")
    print(f"   No phone found:          {no_phones}")
    print(f"   Already had phones:      {skipped}")
    print(f"   Errors:                  {failed}")
    print(f"   Total credits used:      {total_credits}")
    print(f"\n📄 Output: {OUTPUT_CSV}")

    # Push phone numbers back to Google Sheet
    update_sheet_phones(phone_map)


if __name__ == "__main__":
    main()
