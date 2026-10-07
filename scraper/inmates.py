"""
Bexar County Inmate Lead Processor
====================================
Reads: data/BEXAR INMATE *.csv  (TDCJ export, manually uploaded)
Output:
  - data/inmate_leads.csv        (all processed leads)
  - data/inmate_records.json     (for dashboard)
  - Pushes to Google Sheets tab "Inmates"
"""

import csv, json, os, re, glob
from datetime import datetime
from pathlib import Path

SHEET_ID    = "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk"
SHEET_NAME  = "Inmates"

# ── Column headers for the Inmates sheet tab ──────────────────────────────────
SHEET_HEADERS = [
    "Last Name", "First Name",
    "TDCJ Number", "SID Number",
    "Current Facility",
    "Offense",
    "Sentence Date", "Projected Release",
    "Sentence (Years)",
    "Inmate Score",
    "Flags",
    "Source",
    "Status", "Partner Notes", "Date Contacted",
]

# ── Scoring ───────────────────────────────────────────────────────────────────
# Base score for any Bexar inmate (they own property and are incarcerated)
BASE_SCORE = 40

def score_inmate(row):
    score  = BASE_SCORE
    flags  = []
    years  = 0

    try:
        years = float(row.get("Sentence (Years)", 0))
    except (ValueError, TypeError):
        years = 0

    # Long sentence = more desperate / less able to manage property
    if years >= 20:
        score += 20
        flags.append("LIFE/LONG SENTENCE")
    elif years >= 10:
        score += 15
        flags.append("10+ YR SENTENCE")
    elif years >= 5:
        score += 10
        flags.append("5+ YR SENTENCE")
    elif years >= 2:
        score += 5
        flags.append("2+ YR SENTENCE")

    # Serious offense categories — more likely to need quick cash / property relief
    offense = row.get("TDCJ Offense", "").upper()
    violent = any(kw in offense for kw in [
        "MURDER", "MANSLAUGHTER", "ROBBERY", "ASSAULT", "KIDNAP",
        "SEXUAL ASSAULT", "AGGRAVATED", "HOMICIDE"
    ])
    drug = any(kw in offense for kw in [
        "DRUG", "CONTROLLED SUBSTANCE", "POSS", "MANUFACTURE", "DELIVER"
    ])
    fraud = any(kw in offense for kw in [
        "FRAUD", "THEFT", "FORGERY", "EMBEZZLE", "MONEY LAUNDER"
    ])

    if violent:
        score += 10
        flags.append("VIOLENT OFFENSE")
    elif drug:
        score += 8
        flags.append("DRUG OFFENSE")
    elif fraud:
        score += 5
        flags.append("FINANCIAL OFFENSE")

    # Recently sentenced = family still dealing with situation
    try:
        sentence_date = datetime.strptime(row.get("Sentence Date", ""), "%m/%d/%Y")
        days_ago = (datetime.now() - sentence_date).days
        if days_ago <= 180:
            score += 10
            flags.append("RECENTLY SENTENCED")
        elif days_ago <= 365:
            score += 5
            flags.append("SENTENCED <1YR")
    except (ValueError, TypeError):
        pass

    # Release within 2 years = motivated to sell before release or can't maintain
    try:
        release_date = datetime.strptime(row.get("Projected Release", ""), "%m/%d/%Y")
        days_until = (release_date - datetime.now()).days
        if 0 < days_until <= 365:
            score += 10
            flags.append("RELEASING <1YR")
        elif 0 < days_until <= 730:
            score += 5
            flags.append("RELEASING <2YR")
        elif days_until < 0:
            # Already past projected release - may still be in
            flags.append("PAST RELEASE DATE")
    except (ValueError, TypeError):
        pass

    score = min(score, 100)
    return score, flags


def split_name(full_name):
    """'LAST,FIRST MIDDLE' → (last, first)"""
    full_name = full_name.strip()
    if "," in full_name:
        parts = full_name.split(",", 1)
        last  = parts[0].strip().title()
        first = parts[1].strip().split()[0].title() if parts[1].strip() else ""
    else:
        parts = full_name.split()
        last  = parts[-1].title() if parts else ""
        first = parts[0].title() if len(parts) > 1 else ""
    return last, first


def find_inmate_csv():
    """Find the most recent TDCJ CSV in the data folder."""
    data_dir = Path(__file__).parent.parent / "data"
    # Match both 'inmates.csv' and 'BEXAR INMATE *.csv'
    patterns = [
        str(data_dir / "inmates.csv"),
        str(data_dir / "BEXAR INMATE*.csv"),
        str(data_dir / "bexar inmate*.csv"),
        str(data_dir / "inmate*.csv"),
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat, recursive=False))

    if not candidates:
        return None

    # Return most recently modified
    candidates.sort(key=lambda p: Path(p).stat().st_mtime, reverse=True)
    return candidates[0]


def load_inmates(csv_path):
    """Load and parse the TDCJ CSV."""
    records = []
    with open(csv_path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Filter: must have a name and be a real sentence
            name = row.get("Name", "").strip()
            if not name:
                continue
            try:
                years = float(row.get("Sentence (Years)", 0))
            except (ValueError, TypeError):
                years = 0
            # Only include 2+ year sentences (property likely abandoned)
            if years < 2:
                continue
            records.append(row)
    return records


def process_inmates(records):
    """Score and enrich all inmate records."""
    processed = []
    for row in records:
        last, first = split_name(row.get("Name", ""))
        score, flags = score_inmate(row)
        processed.append({
            "last_name":        last,
            "first_name":       first,
            "full_name":        row.get("Name", "").strip(),
            "tdcj_num":         row.get("TDCJ Number", "").strip(),
            "sid_num":          row.get("SID Number", "").strip(),
            "facility":         row.get("Current Facility", "").strip(),
            "offense":          row.get("TDCJ Offense", "").strip(),
            "sentence_date":    row.get("Sentence Date", "").strip(),
            "projected_release":row.get("Projected Release", "").strip(),
            "sentence_years":   row.get("Sentence (Years)", "").strip(),
            "score":            score,
            "flags":            flags,
        })
    # Sort by score descending
    processed.sort(key=lambda r: r["score"], reverse=True)
    return processed


def save_outputs(records):
    """Save CSV and JSON outputs."""
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    # CSV
    csv_path = data_dir / "inmate_leads.csv"
    fieldnames = [
        "last_name", "first_name", "full_name", "tdcj_num", "sid_num",
        "facility", "offense", "sentence_date", "projected_release",
        "sentence_years", "score", "flags"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            row = dict(r)
            row["flags"] = " | ".join(r["flags"])
            writer.writerow(row)
    print(f"  💾 {csv_path}")

    # JSON for dashboard
    json_path = data_dir / "inmate_records.json"
    output = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "source":     "TDCJ / Bexar County",
        "total":      len(records),
        "high_score": sum(1 for r in records if r["score"] >= 70),
        "records":    records,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  💾 {json_path}")

    return records


def push_to_sheets(records):
    """Push inmate leads to the 'Inmates' tab in Google Sheets."""
    print("\n📊 Pushing to Google Sheets (Inmates tab) …")
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

        # ── Ensure the Inmates tab exists ──────────────────────────────────
        meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
        existing_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

        if SHEET_NAME not in existing_tabs:
            print(f"  Creating '{SHEET_NAME}' tab …")
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
            ).execute()
            # Re-fetch to get the new sheetId
            meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()

        # Get sheetId for Inmates tab
        inmates_sheet_id = None
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == SHEET_NAME:
                inmates_sheet_id = s["properties"]["sheetId"]
                break

        # ── Fetch existing rows to avoid dupes ────────────────────────────
        result   = sheet.values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_NAME}!A:O"
        ).execute()
        existing = result.get("values", [])

        # Dedup by TDCJ number (col index 2)
        existing_tdcj = set()
        for row in existing[1:]:
            if len(row) > 2 and row[2]:
                existing_tdcj.add(str(row[2]).strip())

        print(f"  Existing rows: {len(existing) - 1 if existing else 0}")

        new_recs = [r for r in records if r["tdcj_num"] not in existing_tdcj]
        print(f"  New to add: {len(new_recs)}")

        if not new_recs:
            print("  ✅ Inmates sheet up to date")
            return

        def make_row(r):
            return [
                r["last_name"],
                r["first_name"],
                r["tdcj_num"],
                r["sid_num"],
                r["facility"],
                r["offense"],
                r["sentence_date"],
                r["projected_release"],
                r["sentence_years"],
                str(r["score"]),
                " | ".join(r["flags"]),
                "TDCJ Bexar County",
                "", "", "",  # Status, Partner Notes, Date Contacted
            ]

        new_rows = [make_row(r) for r in new_recs]

        if not existing or len(existing) == 0:
            # Fresh sheet — write headers + data
            sheet.values().update(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_NAME}!A1",
                valueInputOption="RAW",
                body={"values": [SHEET_HEADERS] + new_rows},
            ).execute()
            print(f"  ✅ Created Inmates sheet with {len(new_rows)} records")
        else:
            # Insert at top (after header)
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"insertDimension": {
                    "range": {
                        "sheetId":    inmates_sheet_id,
                        "dimension":  "ROWS",
                        "startIndex": 1,
                        "endIndex":   1 + len(new_rows),
                    },
                    "inheritFromBefore": False,
                }}]},
            ).execute()
            sheet.values().update(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_NAME}!A2",
                valueInputOption="RAW",
                body={"values": new_rows},
            ).execute()
            print(f"  ✅ {len(new_rows)} inmate leads added to top of sheet")

        print(f"  🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")

    except Exception as e:
        print(f"  ✗ Sheets error: {e}")
        import traceback; traceback.print_exc()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Bexar County Inmate Lead Processor")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)

    csv_path = find_inmate_csv()
    if not csv_path:
        print("\n⚠  No inmate CSV found in data/")
        print("   Upload TDCJ export as: data/inmates.csv")
        print("   or: data/BEXAR INMATE <date>.csv")
        return

    print(f"\n📂 Loading: {Path(csv_path).name}")
    raw = load_inmates(csv_path)
    print(f"  Raw rows (2+ yr sentences): {len(raw)}")

    print("\n⚙️  Scoring inmates …")
    processed = process_inmates(raw)

    high = sum(1 for r in processed if r["score"] >= 70)
    print(f"\n✅ {len(processed)} inmate leads")
    print(f"   High score (70+): {high}")
    print(f"   Score range: {processed[-1]['score']} – {processed[0]['score']}")

    # Sample top 5
    print("\n🏆 Top 5 leads:")
    for r in processed[:5]:
        print(f"   [{r['score']}] {r['last_name']}, {r['first_name']}"
              f" | {r['offense']} | {r['sentence_years']} yrs"
              f" | Flags: {', '.join(r['flags'])}")

    save_outputs(processed)
    push_to_sheets(processed)

    print(f"\n🎯 Done — {len(processed)} inmate leads | {high} high score (70+).")


if __name__ == "__main__":
    main()
