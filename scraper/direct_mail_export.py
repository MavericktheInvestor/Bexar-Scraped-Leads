"""
Direct Mail Export
==================
Reads:  data/skip_traced.csv
Output: data/direct_mail_export.csv   (YellowLettersComplete / Click2Mail format)

Exports ONLY leads with NO phone number — these are the ones that
couldn't be reached by phone and need a direct mail touchpoint instead.

Also pushes the list to a Google Sheets tab "Direct Mail Queue" for review.

Run:
  python scraper/direct_mail_export.py
"""

import csv, json, os, re
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR   = Path(__file__).parent.parent / "data"
INPUT_CSV  = DATA_DIR / "skip_traced.csv"
OUTPUT_CSV = DATA_DIR / "direct_mail_export.csv"

SHEET_ID   = "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk"
SHEET_NAME = "Direct Mail Queue"

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_address(raw):
    """
    Strip embedded city/state/zip from county clerk address strings.
    e.g. '11211 ARISTIDES, SAN ANTONIO, TEXAS, 78245' → '11211 ARISTIDES'
    """
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(",")]
    # First part is always the street
    return parts[0].title() if parts else raw.title()


def extract_zip(raw):
    """Pull 5-digit zip from an embedded address string."""
    m = re.search(r'\b(\d{5})\b', raw or "")
    return m.group(1) if m else ""


def title(s):
    return s.strip().title() if s else ""


# ── Export ────────────────────────────────────────────────────────────────────

# YellowLettersComplete / Click2Mail standard columns
MAIL_HEADERS = [
    "First Name",
    "Last Name",
    "Mailing Address",
    "Mailing City",
    "Mailing State",
    "Mailing Zip",
    "Property Address",
    "Property City",
    "Property State",
    "Property Zip",
    "Lead Type",
    "Seller Score",
    "Flags",
    "Document Number",
    "Date Filed",
]


def build_mail_row(r):
    """Build one mail-house row from a skip_traced.csv record."""
    first = title(r.get("first", ""))
    last  = title(r.get("last",  ""))

    # Mailing address — prefer explicit mail_address columns; fall back to property
    mail_addr  = r.get("mail_address", "").strip()
    mail_city  = title(r.get("mail_city",  ""))
    mail_state = r.get("mail_state", "TX").strip().upper() or "TX"
    mail_zip   = r.get("mail_zip",   "").strip()

    prop_raw   = r.get("prop_address", "")
    prop_addr  = clean_address(prop_raw)
    prop_city  = title(r.get("prop_city",  "") or "San Antonio")
    prop_state = r.get("prop_state", "TX").strip().upper() or "TX"
    prop_zip   = r.get("prop_zip", "").strip() or extract_zip(prop_raw)

    # If no separate mailing address, mail to the property
    if not mail_addr:
        mail_addr  = prop_addr
        mail_city  = prop_city
        mail_state = prop_state
        mail_zip   = prop_zip
    else:
        mail_addr = title(mail_addr)
        if not mail_zip:
            mail_zip = extract_zip(r.get("mail_address", ""))

    return {
        "First Name":       first,
        "Last Name":        last,
        "Mailing Address":  mail_addr,
        "Mailing City":     mail_city,
        "Mailing State":    mail_state,
        "Mailing Zip":      mail_zip,
        "Property Address": prop_addr,
        "Property City":    prop_city,
        "Property State":   prop_state,
        "Property Zip":     prop_zip,
        "Lead Type":        r.get("lead_type", ""),
        "Seller Score":     r.get("score", ""),
        "Flags":            r.get("flags", ""),
        "Document Number":  r.get("doc_num", ""),
        "Date Filed":       r.get("filed", ""),
    }


# ── Google Sheets ─────────────────────────────────────────────────────────────

def push_to_sheets(rows):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        print("  ⚠  GOOGLE_CREDENTIALS not set — skipping sheet push")
        return

    try:
        import google.oauth2.service_account as sa
        import googleapiclient.discovery as discovery

        creds = sa.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        svc   = discovery.build("sheets", "v4", credentials=creds, cache_discovery=False)
        sheet = svc.spreadsheets()

        # Ensure tab exists
        meta = sheet.get(spreadsheetId=SHEET_ID).execute()
        existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if SHEET_NAME not in existing:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]}
            ).execute()

        values = [MAIL_HEADERS] + [[r[h] for h in MAIL_HEADERS] for r in rows]

        sheet.values().clear(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_NAME}'!A:Z"
        ).execute()

        sheet.values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_NAME}'!A1",
            valueInputOption="RAW",
            body={"values": values}
        ).execute()

        print(f"  ✅ {len(rows)} leads written to '{SHEET_NAME}' tab")
        print(f"  🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")

    except Exception as e:
        print(f"  ⚠  Sheet push failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Direct Mail Export")
    print(f"  {datetime.now(timezone.utc).isoformat()} UTC")
    print("=" * 60)

    if not INPUT_CSV.exists():
        print(f"❌ {INPUT_CSV} not found — run skip trace first")
        return

    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f:
        all_leads = list(csv.DictReader(f))

    # Only leads with NO phone number
    no_phone = [
        r for r in all_leads
        if not (r.get("Phone 1", "") or r.get("phone1", "")).strip()
        and (r.get("first", "") or r.get("last", ""))  # must have a name
    ]

    print(f"\n📂 Total leads in skip_traced.csv : {len(all_leads)}")
    print(f"   No phone (direct mail targets)  : {len(no_phone)}")
    print(f"   Already have phones (skip)       : {len(all_leads) - len(no_phone)}")

    if not no_phone:
        print("\n🎉 All leads have phone numbers — nothing to mail!")
        return

    mail_rows = [build_mail_row(r) for r in no_phone]

    # Write CSV
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MAIL_HEADERS)
        w.writeheader()
        w.writerows(mail_rows)

    print(f"\n📄 Export: {OUTPUT_CSV}")
    print(f"   {len(mail_rows)} leads ready for mail house upload")
    print(f"\n📊 Lead type breakdown:")
    from collections import Counter
    for lt, cnt in Counter(r["Lead Type"] for r in mail_rows).most_common():
        print(f"   {lt}: {cnt}")

    print(f"\n🏆 Top 5 by score:")
    for r in sorted(mail_rows, key=lambda x: int(x["Seller Score"] or 0), reverse=True)[:5]:
        print(f"   [{r['Seller Score']}] {r['Last Name']}, {r['First Name']}"
              f" | {r['Property Address']}, {r['Property City']} {r['Property Zip']}")

    push_to_sheets(mail_rows)

    print(f"\n✅ Done — upload {OUTPUT_CSV.name} to YellowLettersComplete or Click2Mail")


if __name__ == "__main__":
    main()
