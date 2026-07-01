#!/usr/bin/env python3
"""
daily_leads.py — Automatic daily lead sourcing for the Client Hub.

Reads target markets from client_hub.db, searches Google Places for each,
imports new leads into the pipeline. Run this daily via cron at 8 AM.

Cron line:
    0 8 * * * /usr/bin/python3 /Users/cnp/Downloads/tools/daily_leads.py >> /Users/cnp/Downloads/tools/daily_leads.log 2>&1

Manual run:
    python3 /Users/cnp/Downloads/tools/daily_leads.py
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)

DB_PATH = os.environ.get("CLIENT_HUB_DB", os.path.join(TOOLS_DIR, "client_hub.db"))

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  {msg}", flush=True)


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def upsert_lead(name, phone, website, rating, review_count, maps_url, city, state, dm_text=""):
    with db() as c:
        row = c.execute(
            "SELECT id FROM leads WHERE business_name=? AND city=?", (name, city)).fetchone()
        if row:
            return row["id"], False
        cur = c.execute(
            "INSERT INTO leads(business_name,phone,website,rating,review_count,"
            "maps_url,city,state,dm_text) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, phone, website, rating, review_count, maps_url, city, state, dm_text))
        lid = cur.lastrowid
        c.execute("INSERT INTO activity(lead_id,action,detail) VALUES(?,?,?)",
                  (lid, "added", "Daily auto-source"))
        return lid, True


def main():
    if not os.path.exists(DB_PATH):
        log("❌  client_hub.db not found. Open Client Hub once to initialize it.")
        sys.exit(1)

    with db() as c:
        targets = c.execute(
            "SELECT * FROM daily_targets WHERE active=1 ORDER BY id").fetchall()

    if not targets:
        log("ℹ️   No active daily targets configured. Open Client Hub → Auto-Source to add some.")
        sys.exit(0)

    # Load API tools
    try:
        import lead_finder as lf
    except ImportError:
        log("❌  Could not import lead_finder.py — make sure it's in the same folder.")
        sys.exit(1)

    api_key = lf.load_key()
    if not api_key:
        log("❌  No Google Places API key saved. Run: python3 lead_finder.py --save-key YOUR_KEY")
        sys.exit(1)

    total_found = 0
    total_new   = 0

    # Daily rotation + cap: with many markets (cities x types) we'd source far more
    # than we want per day. Cap the daily pull and rotate which markets run each day
    # so everything gets covered over several days without exceeding the cap.
    import math
    cap   = int(os.environ.get("DAILY_LEAD_CAP", "100"))
    total = len(targets)
    avg   = max(1, sum(t["max_per_run"] for t in targets) // total)
    per_day = max(1, math.ceil(cap / avg))
    doy   = datetime.now().timetuple().tm_yday
    start = (doy * per_day) % total
    order = [targets[(start + i) % total] for i in range(total)]
    log(f"📋  {total} markets total · cap {cap}/day · starting at market #{start} (rotates daily)")

    for t in order:
        if total_found >= cap:
            log(f"   ⏸️  Daily cap of {cap} reached — remaining markets run on upcoming days.")
            break
        biz_type = t["biz_type"]
        city     = t["city"]
        state    = t["state"]
        max_run  = t["max_per_run"]
        query    = f"{biz_type} in {city}, {state}"
        log(f"🔎  {query}  (up to {max_run})")

        try:
            places = lf.search(api_key, query, max_run)
        except Exception as e:
            log(f"   ⚠️  Search failed: {e}")
            continue

        found, added = 0, 0
        for p in places:
            name      = p.get("displayName", {}).get("text", "")
            if not name:
                continue
            phone     = p.get("nationalPhoneNumber", "")
            website   = p.get("websiteUri", "")
            rating    = p.get("rating", 0) or 0
            rev_count = p.get("userRatingCount", 0) or 0
            maps_url  = p.get("googleMapsUri", "")
            _, is_new = upsert_lead(name, phone, website, rating, rev_count, maps_url,
                                    city, state)
            found += 1
            if is_new:
                added += 1

        log(f"   ✅  {added} new / {found} total  ({city}, {state})")
        total_found += found
        total_new   += added

        time.sleep(2)  # polite gap between searches

    log(f"\n🎯  Done — {total_new} new leads added ({total_found} total found across {len(targets)} markets)")

    # macOS notification
    try:
        import subprocess
        subprocess.run([
            "osascript", "-e",
            f'display notification "{total_new} new leads added to pipeline" '
            f'with title "Client Hub — Daily Leads" sound name "Default"'
        ], capture_output=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
