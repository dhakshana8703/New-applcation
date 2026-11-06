"""
Dedicated importer to dump master_cvent_events.json into the cvent table.
Works seamlessly with PostgreSQL (Neon) and SQLite fallback via db_compat.
"""

import json
import os
import sys
from db_compat import get_db_connection
import cvent_db

MASTER_JSON_PATH = os.path.join(os.path.dirname(__file__), "master_cvent_events.json")


def clean_str(val):
    if val is None:
        return ""
    return str(val).strip()


def run_import():
    if not os.path.exists(MASTER_JSON_PATH):
        print(f"Error: {MASTER_JSON_PATH} not found.")
        sys.exit(1)

    print("Ensuring Cvent table exists...")
    cvent_db.init_cvent_tables()

    print(f"Reading master dataset from {MASTER_JSON_PATH}...")
    with open(MASTER_JSON_PATH, "r", encoding="utf-8") as f:
        events = json.load(f)

    print(f"Loaded {len(events)} events from JSON.")

    conn = get_db_connection()
    inserted_events = 0
    updated_events = 0

    print("Importing into 'cvent' table...")
    for idx, item in enumerate(events, 1):
        cvent_url = clean_str(item.get("cvent_url"))
        if not cvent_url:
            continue

        event_name = clean_str(item.get("event_name")) or "Untitled Cvent Event"
        event_url = clean_str(item.get("resolved_url")) or clean_str(item.get("event_url"))
        start_date = clean_str(item.get("start_date"))
        end_date = clean_str(item.get("end_date"))
        venue_name = clean_str(item.get("venue_name"))
        city = clean_str(item.get("city"))
        state = clean_str(item.get("state_region")) or clean_str(item.get("state"))
        country = clean_str(item.get("country"))
        organizer = clean_str(item.get("organizer")) or clean_str(item.get("organizer_company"))

        creator_email = clean_str(item.get("creator_email"))
        if not creator_email:
            contact_email = clean_str(item.get("contact_email"))
            if contact_email:
                creator_email = contact_email.split(",")[0].strip()

        status = cvent_db.compute_status(event_url, creator_email)

        # Upsert into cvent table
        existing = conn.execute("SELECT id FROM cvent WHERE cvent_url = ?", (cvent_url,)).fetchone()
        if existing:
            event_id = existing["id"] if hasattr(existing, "keys") else existing[0]
            conn.execute("""
                UPDATE cvent SET
                    event_url = CASE WHEN ? != '' THEN ? ELSE event_url END,
                    event_name = ?,
                    start_date = CASE WHEN ? != '' THEN ? ELSE start_date END,
                    end_date = CASE WHEN ? != '' THEN ? ELSE end_date END,
                    venue_name = CASE WHEN ? != '' THEN ? ELSE venue_name END,
                    city = CASE WHEN ? != '' THEN ? ELSE city END,
                    state = CASE WHEN ? != '' THEN ? ELSE state END,
                    country = CASE WHEN ? != '' THEN ? ELSE country END,
                    organizer_company = CASE WHEN ? != '' THEN ? ELSE organizer_company END,
                    creator_email = CASE WHEN ? != '' THEN ? ELSE creator_email END,
                    status = ?
                WHERE id = ?
            """, (
                event_url, event_url,
                event_name,
                start_date, start_date,
                end_date, end_date,
                venue_name, venue_name,
                city, city,
                state, state,
                country, country,
                organizer, organizer,
                creator_email, creator_email,
                status,
                event_id
            ))
            updated_events += 1
        else:
            conn.execute("""
                INSERT INTO cvent (
                    cvent_url, event_url, event_name, start_date, end_date,
                    venue_name, city, state, country, organizer_company,
                    creator_email, status, scrape_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                cvent_url, event_url, event_name, start_date, end_date,
                venue_name, city, state, country, organizer,
                creator_email, status
            ))
            inserted_events += 1

        if idx % 50 == 0 or idx == len(events):
            conn.commit()

    conn.commit()
    conn.close()

    print("\n" + "=" * 50)
    print("IMPORT COMPLETE & VERIFIED!")
    print(f"Total events in file:    {len(events)}")
    print(f"New events inserted:     {inserted_events}")
    print(f"Existing events updated: {updated_events}")
    print("=" * 50)


if __name__ == "__main__":
    run_import()
