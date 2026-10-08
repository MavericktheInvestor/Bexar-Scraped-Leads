"""
Skip Trace Queue Builder
========================
Reads:  data/leads.csv  (output of fetch.py)
        data/inmate_property_matches.csv  (optional — inmate owners)
Output:
  - data/skip_trace_queue.csv   (ranked, deduplicated, ready for Deal Machine)
  - Pushes to Google Sheets tab "Skip Trace Queue"

Rules:
  - Score >= 70 only
  - No businesses / LLCs (can't skip trace a corp)
  - Deduplicates by owner name across both lead sources
  - Sorted by score descending
  - Monthly cap: SKIP_TRACE_MONTHLY_CAP (default 20,000)
  - Does NOT trigger skip trace — output is a queue for manual/green-light dispatch
"""

import csv, json, os, re
from datetime import datetime
from pathlib import Path

SHEET_ID   = "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk"
SHEET_NAME = "Skip Trace Queue"

SCORE_THRESHOLD        = 70
SKIP_TRACE_MONTHLY_CAP = int(os.environ.get("SKIP_TRACE_MONTHLY_CAP", "20000"))

BUSINESS_KEYWORDS = (
    "LLC","INC","CORP","LTD","TRUST","LP ","L.P.","ASSOC",
    "PROPERTIES","HOLDINGS","INVESTMENTS","REALTY","GROUP",
    "PARTNERS","VENTURES","CAPITAL","FUND","ESTATE OF",
)

SHEET_HEADERS = [
    "Priority Rank",
    "First Name", "Last Name",
    "Property Address", "Property City", "Property State", "Property Zip",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
    "Seller Score", "Lead Type", "Flags",
    "Source", "Document Number", "Date Filed",
    "Skip Trace Status",   # blank = pending, "Sent" = dispatched, "Done" = returned
    "Phone 1", "Phone 2",
    "Skip Trace Date",
    "Partner Notes",
]

DATA_DIR = Path(__file__).parent.parent / "data"


# ── Helpers ───────────────────────────────────────────────────────────────────

def owner_key(first, last):
    """Normalised dedup key from first + last name."""
    raw = f"{last} {first}".upper()
    raw = re.sub(r"[^A-Z0-9 ]", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def is_business(first, last, flags=""):
    name = f"{first} {last} {flags}".upper()
    return any(k in name for k in BUSINESS_KEYWORDS)


def split_name(full):
    """'LAST, FIRST MIDDLE' or 'FIRST LAST' → (first, last)"""
    if not full:
        return "", ""
    if "," in full:
        parts = full.split(",", 1)
        last  = parts[0].strip().title()
        first = parts[1].strip().split()[0].title() if parts[1].strip() else ""
    else:
        parts = full.strip().title().split()
        if len(parts) == 1:
            return "", parts[0]
        first = parts[0]
        last  = " ".join(parts[1:])
    return first, last


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_county_leads():
    path = DATA_DIR / "leads.csv"
    if not path.exists():
        print(f"  ⚠  {path} not found — skipping county leads")
        return []
    records = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            try:
                score = int(float(row.get("Seller Score", 0) or 0))
            except (ValueError, TypeError):
                score = 0
            if score < SCORE_THRESHOLD:
                continue
            first = row.get("First Name", "").strip()
            last  = row.get("Last Name", "").strip()
            flags = row.get("Motivated Seller Flags", "")
            if is_business(first, last, flags):
                continue
            records.append({
                "first":        first,
                "last":         last,
                "prop_address": row.get("Property Address", ""),
                "prop_city":    row.get("Property City", ""),
                "prop_state":   row.get("Property State", "TX"),
                "prop_zip":     row.get("Property Zip", ""),
                "mail_address": row.get("Mailing Address", ""),
                "mail_city":    row.get("Mailing City", ""),
                "mail_state":   row.get("Mailing State", "TX"),
                "mail_zip":     row.get("Mailing Zip", ""),
                "score":        score,
                "lead_type":    row.get("Lead Type", ""),
                "flags":        flags,
                "source":       "County Clerk",
                "doc_num":      row.get("Document Number", ""),
                "filed":        row.get("Date Filed", ""),
            })
    print(f"  County leads (score {SCORE_THRESHOLD}+, no LLC): {len(records)}")
    return records


def load_inmate_matches():
    path = DATA_DIR / "inmate_property_matches.csv"
    if not path.exists():
        print(f"  ⚠  {path} not found — skipping inmate matches")
        return []
    records = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            try:
                score = int(float(row.get("Inmate Score", 0) or 0))
            except (ValueError, TypeError):
                score = 0
            if score < SCORE_THRESHOLD:
                continue
            first = row.get("First Name", "").strip()
            last  = row.get("Last Name", "").strip()
            if is_business(first, last):
                continue
            records.append({
                "first":        first,
                "last":         last,
                "prop_address": row.get("Property Address", ""),
                "prop_city":    row.get("Property City", ""),
                "prop_state":   row.get("Property State", "TX"),
                "prop_zip":     row.get("Property Zip", ""),
                "mail_address": "",
                "mail_city":    "",
                "mail_state":   "TX",
                "mail_zip":     "",
                "score":        score,
                "lead_type":    "Incarcerated Owner",
                "flags":        row.get("Flags", ""),
                "source":       "TDCJ Inmate",
                "doc_num":      row.get("TDCJ Number", ""),
                "filed":        row.get("Sentence Date", ""),
            })
    print(f"  Inmate matches (score {SCORE_THRESHOLD}+): {len(records)}")
    return records


# ── Build queue ───────────────────────────────────────────────────────────────

def build_queue(county, inmates):
    """
    Merge both sources, deduplicate by owner name, sort by score,
    cap at monthly budget.
    """
    all_leads = county + inmates
    all_leads.sort(key=lambda r: r["score"], reverse=True)

    seen = set()
    queue = []
    for r in all_leads:
        k = owner_key(r["first"], r["last"])
        if not k:
            continue   # skip nameless rows
        if k in seen:
            continue
        seen.add(k)
        queue.append(r)
        if len(queue) >= SKIP_TRACE_MONTHLY_CAP:
            break

    print(f"\n  ✅ Queue: {len(queue)} unique leads (cap {SKIP_TRACE_MONTHLY_CAP:,}/mo)")
    return queue


# ── Save ──────────────────────────────────────────────────────────────────────

def save_queue(queue):
    path = DATA_DIR / "skip_trace_queue.csv"
    fields = [
        "priority_rank","first","last",
        "prop_address","prop_city","prop_state","prop_zip",
        "mail_address","mail_city","mail_state","mail_zip",
        "score","lead_type","flags","source","doc_num","filed",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for i, r in enumerate(queue, 1):
            row = dict(r)
            row["priority_rank"] = i
            w.writerow(row)
    print(f"  💾 {path}")
    return path


# ── Google Sheets ─────────────────────────────────────────────────────────────

def push_to_sheets(queue):
    print("\n📊 Pushing to Google Sheets (Skip Trace Queue tab) …")
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        print("  ⚠  GOOGLE_CREDENTIALS not set — skipping")
        return

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
        meta = sheet.get(spreadsheetId=SHEET_ID).execute()
        existing_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

        if SHEET_NAME not in existing_tabs:
            print(f"  Creating '{SHEET_NAME}' tab …")
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
            ).execute()

        # Full replace — queue is rebuilt fresh each run
        new_rows = []
        for i, r in enumerate(queue, 1):
            new_rows.append([
                i,
                r["first"], r["last"],
                r["prop_address"], r["prop_city"], r["prop_state"], r["prop_zip"],
                r["mail_address"], r["mail_city"], r["mail_state"], r["mail_zip"],
                r["score"], r["lead_type"], r["flags"],
                r["source"], r["doc_num"], r["filed"],
                "",   # Skip Trace Status
                "",   # Phone 1
                "",   # Phone 2
                "",   # Skip Trace Date
                "",   # Partner Notes
            ])

        # Clear and rewrite
        sheet.values().clear(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_NAME}'!A:W",
        ).execute()

        sheet.values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_NAME}'!A1",
            valueInputOption="RAW",
            body={"values": [SHEET_HEADERS] + new_rows},
        ).execute()

        print(f"  ✅ {len(new_rows)} leads written to '{SHEET_NAME}'")
        print(f"  🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")

    except Exception as e:
        print(f"  ✗ Sheets error: {e}")
        import traceback; traceback.print_exc()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Skip Trace Queue Builder")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print(f"  Score threshold : {SCORE_THRESHOLD}")
    print(f"  Monthly cap     : {SKIP_TRACE_MONTHLY_CAP:,}")
    print("=" * 60)

    print("\n📂 Loading leads …")
    county  = load_county_leads()
    inmates = load_inmate_matches()

    print("\n⚙️  Building queue …")
    queue = build_queue(county, inmates)

    if not queue:
        print("\n⚠  No qualifying leads — nothing to queue.")
        return

    # Score breakdown
    tiers = {"90-100": 0, "80-89": 0, "70-79": 0}
    for r in queue:
        s = r["score"]
        if s >= 90:   tiers["90-100"] += 1
        elif s >= 80: tiers["80-89"]  += 1
        else:         tiers["70-79"]  += 1

    sources = {}
    for r in queue:
        sources[r["source"]] = sources.get(r["source"], 0) + 1

    print(f"\n📊 Score breakdown:")
    for tier, cnt in tiers.items():
        print(f"   {tier}: {cnt}")
    print(f"\n📊 By source:")
    for src, cnt in sources.items():
        print(f"   {src}: {cnt}")

    print(f"\n🏆 Top 5:")
    for r in queue[:5]:
        print(f"   [{r['score']}] {r['last']}, {r['first']}"
              f" | {r['lead_type']} | {r['prop_address']} | {r['flags'][:60]}")

    save_queue(queue)
    push_to_sheets(queue)

    print(f"\n🎯 Done — {len(queue)} leads queued."
          f" {SKIP_TRACE_MONTHLY_CAP - len(queue):,} budget remaining this month.")
    print("\n⚠️  Skip trace is NOT triggered automatically.")
    print("   Green-light the queue to dispatch to Deal Machine.")


if __name__ == "__main__":
    main()
