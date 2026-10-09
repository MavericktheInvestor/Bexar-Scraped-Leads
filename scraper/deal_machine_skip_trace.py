"""
Deal Machine Skip Trace
=======================
Reads:  data/skip_trace_queue.csv   (built by skip_trace_queue.py)
Action: Uses Deal Machine CLI (dm enrich address) to look up owner
        phones for each lead, writes Phone 1/2 back to the CSV and
        to the Google Sheets "Skip Trace Queue" tab.

Uses the official DM CLI to avoid Cloudflare IP bans on GitHub Actions.

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

# ── DM CLI helpers ─────────────────────────────────────────────────────────────

def install_dm_cli():
    """Install the Deal Machine CLI if not already present."""
    import subprocess
    result = subprocess.run(["which", "dm"], capture_output=True)
    if result.returncode == 0:
        return  # already installed
    print("  Installing Deal Machine CLI …")
    subprocess.run(
        ["npm", "install", "-g", "@dealmachine/cli"],
        check=True, capture_output=False
    )


def dm_cli_login():
    """Authenticate the CLI with the API key using --key flag."""
    import subprocess
    result = subprocess.run(
        ["dm", "login", "--key", DM_API_KEY],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"dm login failed: {result.stderr.strip()[:300]}")
    print(f"  ✅ DM CLI logged in: {result.stdout.strip()[:100]}")

    # Print available flags for dm enrich address so we know what's valid
    help_result = subprocess.run(
        ["dm", "enrich", "address", "--help"],
        capture_output=True, text=True, timeout=15
    )
    print("=== dm enrich address --help ===")
    print(help_result.stdout[:2000])
    print(help_result.stderr[:500])


# ── Skip trace logic ───────────────────────────────────────────────────────────

def enrich_address_cli(address, city, state="TX", zip_=None):
    """
    Use `dm enrich address` CLI to get owner phones.
    Returns (phones_list, credits_used) where phones_list is
    [{number, type, do_not_call}, ...] shaped the same as the API.

    dm enrich address takes ONE positional arg: the full address string.
    Format: "123 MAIN ST, SAN ANTONIO, TX 78245"
    """
    import subprocess, re

    # Normalize state — CLI needs 2-letter abbreviation
    STATE_MAP = {
        "TEXAS": "TX", "CALIFORNIA": "CA", "FLORIDA": "FL",
        "GEORGIA": "GA", "ARIZONA": "AZ", "NEW YORK": "NY",
    }
    if state:
        state = STATE_MAP.get(state.upper().strip(), state.upper().strip())
    state = state or "TX"

    # Parse address — it may already contain city/state/zip embedded
    # e.g. "12019 LA CUCHILLA, SAN ANTONIO, TEXAS, 78245"
    # We want: "12019 LA CUCHILLA, SAN ANTONIO, TX 78245"
    parts = [p.strip() for p in address.split(",")]

    # Extract zip from last part if it looks like one
    if not zip_ and parts:
        last = parts[-1]
        m = re.match(r'^(\d{5})(?:-\d{4})?$', last.strip())
        if m:
            zip_ = m.group(1)
            parts = parts[:-1]

    # Normalize state within parts
    if parts:
        last = parts[-1].upper().strip()
        if last in STATE_MAP or (len(last) == 2 and last.isalpha()):
            state = STATE_MAP.get(last, last)
            parts = parts[:-1]

    street = parts[0] if parts else address
    if not city and len(parts) > 1:
        city = parts[1]
    city = city or "San Antonio"

    # Build canonical address string: "STREET, CITY, STATE ZIP"
    full_addr = f"{street}, {city}, {state}"
    if zip_:
        full_addr += f" {zip_}"

    cmd = [
        "dm", "enrich", "address", full_addr,
        "--contact-audience", "owners",
        "--fields", "phones",
        "--json",
    ]

    env = os.environ.copy()
    env["DM_API_KEY"] = DM_API_KEY
    env["DEALMACHINE_API_KEY"] = DM_API_KEY

    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)

    if result.returncode != 0:
        raise RuntimeError(f"dm enrich failed: {result.stderr.strip()[:300]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"dm enrich bad JSON: {result.stdout[:300]}")

    # Debug: summarize response
    import sys
    matched = data.get("matched", False) if isinstance(data, dict) else False
    people  = (data.get("totals") or {}).get("people", 0) if isinstance(data, dict) else 0
    print(f"  [DEBUG] dm enrich JSON type={type(data).__name__} matched={matched} people={people} preview={str(data)[:200]}", file=sys.stderr)

    # CLI returns: {matched: bool, data: [{..., people: [{phones:[...]}, ...]}], totals: {...}}
    all_phones = []
    credits_used = 0

    if isinstance(data, list):
        address_results = data
    elif isinstance(data, dict):
        credits_used = (data.get("credits") or {}).get("used", 0)
        if not credits_used:
            credits_used = (data.get("totals") or {}).get("submitted", 0)
        raw = data.get("data", [])
        address_results = raw if isinstance(raw, list) else []
    else:
        address_results = []

    for addr_result in address_results:
        if not isinstance(addr_result, dict):
            continue
        # phones may be in: addr_result.people[].phones  OR  addr_result.contacts[].phones  OR  addr_result.phones
        for person in addr_result.get("people", []):
            if isinstance(person, dict):
                all_phones.extend(person.get("phones", []))
        for contact in addr_result.get("contacts", []):
            if isinstance(contact, dict):
                all_phones.extend(contact.get("phones", []))
        # flat phones list on the address result itself
        all_phones.extend(addr_result.get("phones", []))

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

    if not dry_run:
        install_dm_cli()
        dm_cli_login()

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
            # Try mailing address first (owner's address = tighter match)
            mail_addr = get(r, "mail_address", "Mailing Address")
            prop_addr = get(r, "prop_address", "Property Address")
            phones, credits = [], 0

            if mail_addr:
                phones, credits = enrich_address_cli(
                    mail_addr,
                    get(r, "mail_city",  "Mailing City")  or "San Antonio",
                    get(r, "mail_state", "Mailing State") or "TX",
                    get(r, "mail_zip",   "Mailing Zip")   or None,
                )
                total_credits += credits

            # Fallback: enrich by property address
            if not phones and prop_addr:
                phones, credits2 = enrich_address_cli(
                    prop_addr,
                    get(r, "prop_city",  "Property City")  or "San Antonio",
                    get(r, "prop_state", "Property State") or "TX",
                    get(r, "prop_zip",   "Property Zip")   or None,
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
