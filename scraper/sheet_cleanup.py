"""
One-time cleanup: removes institutional lender / government rows from Sheet1.
Banks, mortgage servicers, credit unions, HOAs, government agencies, etc.
Run via GitHub Actions: workflow_dispatch on sheet_cleanup.yml
"""
import os, json

SHEET_ID   = "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk"
SHEET_GID  = 0   # Sheet1

INSTITUTION_KEYWORDS = (
    "BANK", "MORTGAGE", "LENDING", "LENDER", "FINANCIAL", "FINANCE",
    "CREDIT UNION", "FEDERAL CREDIT", "FEDERAL SAVINGS",
    "FEDERAL RESERVE", "FEDERAL DEPOSIT",
    "FANNIE MAE", "FREDDIE MAC", "GINNIE MAE",
    "HOUSING AND URBAN DEVELOPMENT", "HUD", "FHA", "VA LOAN",
    "SECRETARY OF", "DEPARTMENT OF", "UNITED STATES OF AMERICA",
    "STATE OF TEXAS", "STATE OF ", "COUNTY OF", "CITY OF",
    "INTERNAL REVENUE", "IRS", "COMPTROLLER",
    "WELLS FARGO", "JPMORGAN", "CHASE BANK", "BANK OF AMERICA",
    "CITIBANK", "CITIGROUP", "US BANK", "USBANK",
    "ROCKET MORTGAGE", "QUICKEN LOAN", "PENNYMAC", "NEWREZ",
    "LOANCARE", "NATIONSTAR", "MR. COOPER", "MR COOPER",
    "SELENE FINANCE", "OCWEN", "PHH MORTGAGE", "SPECIALIZED LOAN",
    "ELECTRONIC REGISTRATION SYSTEMS", "MERS",
    "RANDOLPH-BROOKS", "BROADWAY BANK", "FROST BANK",
    "SECURITY STATE BANK", "GENERATIONS COMMUNITY",
    "TRUSTMARK", "AMERIPRISE", "AMERITAS",
    "HOMEOWNERS ASSOCIATION", "OWNERS ASSOCIATION", "COMMUNITY ASSOCIATION",
    "CONDOMINIUM ASSOCIATION", "TOWNHOUSE ASSOCIATION",
    "HABITAT FOR HUMANITY", "AFFORDABLE HOUSING",
    "VETERANS LAND BOARD", "SMALL BUSINESS ADMINISTRATION",
    "INSURANCE COMPANY",
)

def is_institution(last, first):
    full = f"{last} {first}".upper()
    return any(k in full for k in INSTITUTION_KEYWORDS)

def main():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        print("❌ No GOOGLE_CREDENTIALS env var")
        raise SystemExit(1)

    import google.oauth2.service_account as sa
    import googleapiclient.discovery as discovery

    creds = sa.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    svc   = discovery.build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheet = svc.spreadsheets()

    print("📋 Reading Sheet1 (columns A:B) …")
    result = sheet.values().get(
        spreadsheetId=SHEET_ID,
        range="Sheet1!A:B"
    ).execute()
    rows = result.get("values", [])
    print(f"  Total rows (including header): {len(rows)}")

    # Identify rows to delete (1-indexed, skip header at row 1)
    rows_to_delete = []
    for i, row in enumerate(rows[1:], start=2):
        last  = row[0] if len(row) > 0 else ""
        first = row[1] if len(row) > 1 else ""
        if is_institution(last, first):
            rows_to_delete.append(i)

    print(f"  Rows to delete: {len(rows_to_delete)}")
    if not rows_to_delete:
        print("  Nothing to clean up. Done.")
        return

    print("\n  Sample of rows being removed (first 30):")
    for r in rows_to_delete[:30]:
        row = rows[r - 1]
        print(f"    Row {r}: {row[0]}, {row[1] if len(row) > 1 else ''}")

    # Build deleteDimension requests in REVERSE order (preserve row indices)
    requests = []
    for row_num in sorted(rows_to_delete, reverse=True):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": SHEET_GID,
                    "dimension": "ROWS",
                    "startIndex": row_num - 1,  # 0-indexed
                    "endIndex":   row_num,       # exclusive
                }
            }
        })

    # Send in chunks of 500
    chunk_size = 500
    total_chunks = (len(requests) + chunk_size - 1) // chunk_size
    print(f"\n🗑️  Deleting {len(requests)} rows in {total_chunks} batch(es) …")
    for i in range(0, len(requests), chunk_size):
        chunk = requests[i:i + chunk_size]
        sheet.batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": chunk}
        ).execute()
        print(f"  ✅ Batch {i // chunk_size + 1}/{total_chunks} done ({len(chunk)} rows)")

    print(f"\n✅ Cleanup complete — removed {len(rows_to_delete)} institutional rows")
    print(f"  🔗 https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")

if __name__ == "__main__":
    main()
