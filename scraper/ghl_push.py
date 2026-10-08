"""
GoHighLevel (Jarvis) Lead Push
================================
Reads:  data/skip_trace_queue.csv  (built by skip_trace_queue.py)
Action: Creates contacts + opportunities in GHL Acquisitions Pipeline

Env vars required:
  GHL_API_KEY      — Private Integration Token
  GHL_LOCATION_ID  — Sub-account Location ID
  GHL_PIPELINE_ID  — Pipeline ID (auto-discovered on first run)
  GHL_STAGE_ID     — Stage ID for "Hot Lead" (auto-discovered on first run)

Run with --discover to print pipeline/stage IDs and exit.
"""

import csv, json, os, sys, time, re
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GHL_API_KEY     = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
GHL_PIPELINE_ID = os.environ.get("GHL_PIPELINE_ID", "")   # set after --discover
GHL_STAGE_ID    = os.environ.get("GHL_STAGE_ID", "")      # set after --discover

API_BASE    = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
API_VERSION_V2 = "2021-07-28"   # v2 private integrations still use this header
RATE_LIMIT_DELAY = 0.25   # seconds between API calls (GHL: 100 req/10s)

DATA_DIR = Path(__file__).parent.parent / "data"

SHEET_ID   = "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk"
SHEET_NAME = "Skip Trace Queue"


# ── GHL API client ────────────────────────────────────────────────────────────

def ghl_headers(version=None):
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version":       version or API_VERSION,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

def ghl_get(path, params=None, version=None):
    import urllib.request, urllib.parse
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=ghl_headers(version))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def ghl_post(path, body, version=None):
    import urllib.request
    url  = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers=ghl_headers(version), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), resp.status
    except Exception as e:
        if hasattr(e, "read"):
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {getattr(e, 'code', '?')}: {err_body}") from e
        raise


def ghl_put(path, body, version=None):
    import urllib.request
    url  = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers=ghl_headers(version), method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), resp.status
    except Exception as e:
        if hasattr(e, "read"):
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {getattr(e, 'code', '?')}: {err_body}") from e
        raise


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_ids():
    """Print all pipeline + stage IDs for this location. Run once to configure."""
    print("\n🔍 Discovering pipelines for location:", GHL_LOCATION_ID)

    pipelines = []
    # GHL API v2 (Private Integrations) — try all known endpoint patterns
    attempts = [
        ("/opportunities/pipelines",                  {"locationId": GHL_LOCATION_ID}, "2021-07-28"),
        ("/opportunities/pipelines",                  {"locationId": GHL_LOCATION_ID}, "2021-04-15"),
        (f"/locations/{GHL_LOCATION_ID}/pipelines",   {},                              "2021-07-28"),
        (f"/locations/{GHL_LOCATION_ID}/pipelines",   {},                              "2021-04-15"),
        ("/pipelines",                                {"locationId": GHL_LOCATION_ID}, "2021-07-28"),
        ("/pipelines",                                {"locationId": GHL_LOCATION_ID}, "2021-04-15"),
        # v2 specific paths
        (f"/opportunities/pipelines",                 {"locationId": GHL_LOCATION_ID}, "2023-11-15"),
        (f"/crm/pipelines",                           {"locationId": GHL_LOCATION_ID}, "2021-07-28"),
    ]
    last_error = ""
    for path, params, ver in attempts:
        try:
            print(f"  Trying {path} (version {ver}) …")
            data = ghl_get(path, params, version=ver)
            pipelines = data.get("pipelines", [])
            if not pipelines:
                # Some responses wrap differently
                pipelines = data.get("data", []) or (data if isinstance(data, list) else [])
            if pipelines:
                print(f"  ✅ Got {len(pipelines)} pipeline(s) via {path}")
                break
        except Exception as e:
            if hasattr(e, "read"):
                try:
                    err_body = e.read().decode(errors="replace")[:400]
                    last_error = f"HTTP {getattr(e, 'code', '?')}: {err_body}"
                except Exception:
                    last_error = str(e)
            else:
                last_error = str(e)
            print(f"    ✗ {last_error[:300]}")

    if not pipelines:
        print(f"\n  ❌ Could not fetch pipelines. Last error: {last_error}")
        print("\n  Troubleshooting:")
        print("  1. In GHL: Settings → Private Integrations → your integration")
        print("     → make sure 'Opportunities: Read' scope is enabled")
        print("  2. Make sure the integration is installed/authorized on your sub-account")
        print("  3. Try regenerating the token and updating GHL_API_KEY secret")
        return

    print(f"\n  Found {len(pipelines)} pipeline(s):\n")
    for pl in pipelines:
        print(f"  Pipeline: {pl.get('name','?')}")
        print(f"    ID: {pl.get('id','?')}")
        print(f"    Stages:")
        for st in pl.get("stages", []):
            print(f"      [{st.get('position','?')}] {st.get('name','?')}")
            print(f"           ID: {st.get('id','?')}")
        print()

    print("─" * 60)
    print("Add these to your GitHub secrets:")
    print(f"  GHL_PIPELINE_ID = <id of 'Acquisitions Pipeline'>")
    print(f"  GHL_STAGE_ID    = <id of 'Hot Lead' stage>")


# ── Contact helpers ───────────────────────────────────────────────────────────

def urgency_tag(score):
    if score >= 90: return "Hot Lead 🔥"
    if score >= 80: return "Warm Lead"
    return "Cool Lead"


def build_contact(r):
    tags = [
        "Bexar County Lead",
        urgency_tag(r["score"]),
        r["source"],
    ]
    if r.get("lead_type"):
        tags.append(r["lead_type"])

    # Custom fields — GHL uses field keys
    custom_fields = []
    if r.get("prop_address"):
        custom_fields.append({"key": "property_address",
                               "field_value": r["prop_address"]})
    if r.get("score"):
        custom_fields.append({"key": "seller_score",
                               "field_value": str(r["score"])})
    if r.get("flags"):
        custom_fields.append({"key": "motivated_seller_flags",
                               "field_value": r["flags"]})
    if r.get("doc_num"):
        custom_fields.append({"key": "document_number",
                               "field_value": r["doc_num"]})
    if r.get("filed"):
        custom_fields.append({"key": "date_filed",
                               "field_value": r["filed"]})

    body = {
        "locationId": GHL_LOCATION_ID,
        "firstName":  r.get("first", ""),
        "lastName":   r.get("last", ""),
        "tags":       tags,
        "source":     "Bexar County Scraper",
    }

    # Address
    if r.get("mail_address"):
        body["address1"] = r["mail_address"]
        body["city"]     = r.get("mail_city", "")
        body["state"]    = r.get("mail_state", "TX")
        body["postalCode"] = r.get("mail_zip", "")
    elif r.get("prop_address"):
        body["address1"] = r["prop_address"]
        body["city"]     = r.get("prop_city", "San Antonio")
        body["state"]    = r.get("prop_state", "TX")
        body["postalCode"] = r.get("prop_zip", "")

    if custom_fields:
        body["customFields"] = custom_fields

    return body


def find_existing_contact(first, last):
    """Search GHL for an existing contact by name."""
    try:
        q = f"{first} {last}".strip()
        data = ghl_get("/contacts/search", {
            "locationId": GHL_LOCATION_ID,
            "query": q,
            "limit": 5,
        })
        contacts = data.get("contacts", [])
        for c in contacts:
            fn = (c.get("firstName") or "").strip().lower()
            ln = (c.get("lastName") or "").strip().lower()
            if fn == first.lower() and ln == last.lower():
                return c["id"]
    except Exception:
        pass
    return None


def create_or_update_contact(r):
    """Create contact, or update tags if already exists."""
    existing_id = find_existing_contact(r.get("first",""), r.get("last",""))
    body = build_contact(r)

    if existing_id:
        # Update tags only — don't overwrite existing data
        try:
            result, status = ghl_put(f"/contacts/{existing_id}", {
                "locationId": GHL_LOCATION_ID,
                "tags": body.get("tags", []),
            })
            return existing_id, "updated"
        except Exception as e:
            print(f"    ⚠  Update failed: {e}")
            return existing_id, "update_failed"
    else:
        result, status = ghl_post("/contacts/", body)
        contact_id = result.get("contact", {}).get("id") or result.get("id", "")
        return contact_id, "created"


def create_opportunity(contact_id, r):
    """Create an opportunity in Acquisitions Pipeline → Hot Lead stage."""
    prop = r.get("prop_address") or f"{r.get('last','')} Property"
    name = f"{r.get('last','')}, {r.get('first','')} — {prop}"

    body = {
        "locationId": GHL_LOCATION_ID,
        "pipelineId": GHL_PIPELINE_ID,
        "pipelineStageId": GHL_STAGE_ID,
        "contactId":  contact_id,
        "name":       name,
        "status":     "open",
        "source":     "Bexar County Scraper",
    }
    result, status = ghl_post("/opportunities/", body)
    return result.get("opportunity", {}).get("id") or result.get("id", "")


# ── Google Sheets status update ───────────────────────────────────────────────

def update_sheet_status(pushed_doc_nums):
    """Mark pushed leads as 'Sent to CRM' in the Skip Trace Queue tab."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json or not pushed_doc_nums:
        return
    try:
        import google.oauth2.service_account as sa
        import googleapiclient.discovery as discovery

        creds = sa.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
        svc   = discovery.build("sheets", "v4", credentials=creds, cache_discovery=False)
        sheet = svc.spreadsheets()

        result = sheet.values().get(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_NAME}'!A:R"
        ).execute()
        rows = result.get("values", [])

        updates = []
        for i, row in enumerate(rows[1:], start=2):   # skip header, 1-indexed
            doc_num = row[15] if len(row) > 15 else ""  # col P = Document Number
            if doc_num in pushed_doc_nums:
                # Col R = Skip Trace Status (index 17, column R)
                updates.append({
                    "range": f"'{SHEET_NAME}'!R{i}",
                    "values": [["Sent to CRM"]],
                })

        if updates:
            sheet.values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": updates}
            ).execute()
            print(f"  ✅ Marked {len(updates)} leads as 'Sent to CRM' in sheet")
    except Exception as e:
        print(f"  ⚠  Sheet update failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Discovery mode
    if "--discover" in sys.argv:
        if not GHL_API_KEY or not GHL_LOCATION_ID:
            print("❌ Set GHL_API_KEY and GHL_LOCATION_ID env vars first")
            sys.exit(1)
        discover_ids()
        return

    print("=" * 60)
    print("  GoHighLevel Lead Push")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)

    # Validate config
    missing = [v for v in ["GHL_API_KEY","GHL_LOCATION_ID","GHL_PIPELINE_ID","GHL_STAGE_ID"]
               if not os.environ.get(v)]
    if missing:
        print(f"\n❌ Missing env vars: {', '.join(missing)}")
        print("   Run with --discover first to get pipeline/stage IDs")
        sys.exit(1)

    # Load queue
    queue_path = DATA_DIR / "skip_trace_queue.csv"
    if not queue_path.exists():
        print(f"\n⚠  {queue_path} not found — run skip_trace_queue.py first")
        sys.exit(1)

    leads = []
    with open(queue_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            leads.append(row)

    print(f"\n📂 Queue: {len(leads)} leads")

    # Stats
    created = updated = skipped = errors = 0
    pushed_doc_nums = set()

    print("\n🚀 Pushing to GHL …")
    for i, r in enumerate(leads, 1):
        first = r.get("first", "").strip()
        last  = r.get("last", "").strip()
        if not first and not last:
            skipped += 1
            continue

        try:
            contact_id, action = create_or_update_contact(r)
            if not contact_id:
                print(f"  [{i}] ✗ No contact ID returned for {last}, {first}")
                errors += 1
                continue

            if action == "created":
                opp_id = create_opportunity(contact_id, r)
                created += 1
                if i <= 10 or i % 50 == 0:
                    print(f"  [{i}] ✅ Created: {last}, {first} → opp {opp_id[:8]}…")
            else:
                updated += 1
                if i <= 10 or i % 50 == 0:
                    print(f"  [{i}] 🔄 Updated: {last}, {first}")

            doc_num = r.get("doc_num", "")
            if doc_num:
                pushed_doc_nums.add(doc_num)

        except Exception as e:
            print(f"  [{i}] ✗ {last}, {first}: {e}")
            errors += 1

        time.sleep(RATE_LIMIT_DELAY)

    print(f"\n✅ Done:")
    print(f"   Created : {created}")
    print(f"   Updated : {updated}")
    print(f"   Skipped : {skipped}")
    print(f"   Errors  : {errors}")
    print(f"   🔗 https://app.gohighlevel.com/location/{GHL_LOCATION_ID}/opportunities/list")

    update_sheet_status(pushed_doc_nums)


if __name__ == "__main__":
    main()
