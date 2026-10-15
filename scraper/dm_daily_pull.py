"""
DealMachine Daily Pull
======================
Pulls ~440 tax-delinquent absentee-owned SFR leads from Bexar County each day.
Appends new leads (deduped by phone) to data/dm_fresh_leads.csv.

Uses a page-cursor file (data/dm_pull_state.json) to advance through the
DealMachine result set each day so we never re-pull the same page.

Run via Claude scheduled task each morning before ghl_push fires.

SKIP TRACE GATE:
  This script NEVER calls any skip-trace / phone-enrichment endpoint.
  The SKIP_TRACE_GREEN_LIGHT env var is intentionally not read here.
  Phones come pre-attached from DealMachine contacts[] only.
"""

import json
import os
import csv
import sys
from datetime import date
from pathlib import Path

# ── DealMachine MCP is only available inside Claude sessions.
# This script is intended to be run by a Claude scheduled task, not directly.
# It prints a structured prompt that the scheduled task sends to Claude,
# which then calls the DealMachine MCP tools and writes results back.

DATA_DIR   = Path(__file__).parent.parent / "data"
STATE_FILE = DATA_DIR / "dm_pull_state.json"
LEADS_FILE = DATA_DIR / "dm_fresh_leads.csv"

LEADS_PER_DAY  = 440
COUNTY_FIPS    = "48029"  # Bexar County
SOURCE_TAG     = "DealMachine-TaxDelinquent-Absentee"

CSV_HEADERS = [
    "first", "last", "prop_address",
    "Phone 1", "Phone 2",
    "estimated_value", "tax_delinquent_year",
    "source", "Skip Trace Status", "Skip Trace Date",
]


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"page": 1, "total_pulled": 0, "last_run": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_existing_phones():
    """Return set of all phone numbers already in the leads file."""
    phones = set()
    if not LEADS_FILE.exists():
        return phones
    with open(LEADS_FILE, newline="") as f:
        for row in csv.DictReader(f):
            p1 = (row.get("Phone 1") or "").strip()
            p2 = (row.get("Phone 2") or "").strip()
            if p1: phones.add(p1)
            if p2: phones.add(p2)
    return phones


def append_leads(rows):
    """Append rows to dm_fresh_leads.csv, creating headers if needed."""
    write_header = not LEADS_FILE.exists() or LEADS_FILE.stat().st_size == 0
    with open(LEADS_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def extract_leads_from_dm_results(properties, existing_phones):
    """
    Parse DealMachine property_search results into CSV rows.
    Only includes contacts with at least one non-DNC phone not already seen.
    NEVER calls skip trace — phones must come from DM contacts[] directly.
    """
    rows = []
    today = date.today().isoformat()

    for prop in properties:
        address = prop.get("address") or prop.get("property_address") or ""
        est_val = prop.get("estimated_value") or prop.get("avm") or ""
        tax_year = ""

        # Dig out tax delinquent year if available
        tax_info = prop.get("tax_info") or {}
        if isinstance(tax_info, dict):
            tax_year = tax_info.get("delinquent_year") or tax_info.get("year") or ""

        contacts = prop.get("contacts") or []
        if not contacts:
            continue

        for contact in contacts:
            first = (contact.get("first_name") or contact.get("firstName") or "").strip()
            last  = (contact.get("last_name")  or contact.get("lastName")  or "").strip()

            phones_raw = contact.get("phones") or []
            live_phones = [
                p["number"] for p in phones_raw
                if p.get("number")
                and not p.get("do_not_call")
                and str(p.get("number")).strip() not in existing_phones
            ]

            if not live_phones:
                continue

            phone1 = live_phones[0] if len(live_phones) > 0 else ""
            phone2 = live_phones[1] if len(live_phones) > 1 else ""

            rows.append({
                "first":             first,
                "last":              last,
                "prop_address":      address,
                "Phone 1":           phone1,
                "Phone 2":           phone2,
                "estimated_value":   est_val,
                "tax_delinquent_year": tax_year,
                "source":            SOURCE_TAG,
                "Skip Trace Status": "pre-attached",
                "Skip Trace Date":   today,
            })

            # Track so we don't add the same phone twice within this batch
            if phone1: existing_phones.add(phone1)
            if phone2: existing_phones.add(phone2)

            break  # one contact row per property (primary contact)

    return rows


if __name__ == "__main__":
    # When run directly (e.g. in a test), just print state and exit.
    state = load_state()
    print(f"Current pull state: {json.dumps(state, indent=2)}")
    print(f"Leads file rows:    {sum(1 for _ in open(LEADS_FILE)) - 1 if LEADS_FILE.exists() else 0}")
    print()
    print("This script is designed to be driven by a Claude scheduled task.")
    print("The scheduled task calls DealMachine MCP and passes results here.")
