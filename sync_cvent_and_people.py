"""
Syncs the 542 existing events in the 'cvent' table with their full contact payloads
and populates the 'cvent_people' table relationally using event_id.
"""

import json
import os
import re
from db_compat import get_db_connection

MASTER_JSON = os.path.join(os.path.dirname(__file__), "master_cvent_events.json")


def clean(val):
    if val is None:
        return ""
    return str(val).strip()


def derive_name_from_email(email):
    """Fallback to derive clean Name from corporate email if contact_name is blank."""
    if not email or "@" not in email:
        return "-"
    user = email.split("@")[0]
    parts = re.split(r"[._\-]", user)
    if len(parts) >= 2 and all(p.isalpha() and len(p) >= 2 for p in parts[:2]):
        return f"{parts[0].capitalize()} {parts[1].capitalize()}"
    return "-"


def sync_all():
    print(f"Loading master data from {MASTER_JSON}...")
    with open(MASTER_JSON, "r", encoding="utf-8") as f:
        events = json.load(f)

    conn = get_db_connection()

    print("Updating 'cvent' table with contact payloads...")
    # Step 1: Update cvent table
    for ev in events:
        cvent_url = clean(ev.get("cvent_url"))
        if not cvent_url:
            continue

        contact_name = clean(ev.get("contact_name"))
        contact_phone = clean(ev.get("creator_phone")) or clean(ev.get("contact_phone"))
        general_emails = clean(ev.get("general_emails"))
        contact_type = clean(ev.get("contact_type")) or "In-House"
        creator_email = clean(ev.get("creator_email"))
        if not creator_email:
            contact_email = clean(ev.get("contact_email"))
            if contact_email:
                creator_email = contact_email.split(",")[0].strip()

        conn.execute("""
            UPDATE cvent SET
                contact_name = ?,
                contact_phone = ?,
                general_emails = ?,
                contact_type = ?,
                creator_email = CASE WHEN creator_email = '' THEN ? ELSE creator_email END
            WHERE cvent_url = ?
        """, (contact_name, contact_phone, general_emails, contact_type, creator_email, cvent_url))

    conn.commit()
    print("Events table updated with full contact payloads!")

    # Step 2: Query all events directly from 'cvent' table and derive People
    print("Deriving People directly from 'cvent' table...")
    cur = conn.cursor()
    cur.execute("""
        SELECT id, cvent_url, event_url, event_name, organizer_company,
               creator_email, contact_name, contact_phone, general_emails, contact_type
        FROM cvent
        ORDER BY id ASC
    """)
    rows = cur.fetchall()

    people_inserted = 0
    creator_count = 0
    secondary_count = 0

    for r in rows:
        event_id = r["id"]
        organizer = clean(r["organizer_company"]) or clean(r["event_name"])
        source_url = clean(r["cvent_url"]) or clean(r["event_url"])
        contact_type = clean(r["contact_type"]) or "In-House"
        contact_name = clean(r["contact_name"]) or "-"
        phone = clean(r["contact_phone"]) or "-"
        creator_email = clean(r["creator_email"]).lower()
        general_emails_raw = clean(r["general_emails"]).lower()

        # 1. Primary Creator Record
        if creator_email and "@" in creator_email:
            primary_email = creator_email.split(",")[0].strip()
            # If contact_name is '-', try deriving from email
            display_name = contact_name if contact_name != "-" else derive_name_from_email(primary_email)

            conn.execute("""
                INSERT INTO cvent_people (
                    event_id, organizer_name, contact_name, designation,
                    is_creator_email, phone, linkedin_url, email,
                    source_url, scraper_source, contact_type
                ) VALUES (?, ?, ?, '-', TRUE, ?, '-', ?, ?, 'Cvent', ?)
                ON CONFLICT (event_id, email) DO UPDATE SET
                    organizer_name = EXCLUDED.organizer_name,
                    contact_name = EXCLUDED.contact_name,
                    phone = EXCLUDED.phone,
                    contact_type = EXCLUDED.contact_type
            """, (event_id, organizer, display_name, phone, primary_email, source_url, contact_type))
            people_inserted += 1
            creator_count += 1

        # 2. Secondary General Team Records
        if general_emails_raw:
            for em in re.split(r"[,;\s]+", general_emails_raw):
                em = em.strip().lower()
                if not em or "@" not in em or em == creator_email:
                    continue
                # Skip invalid extensions
                if any(bad in em for bad in [".png", ".jpg", ".svg", "schema.org", "w3.org"]):
                    continue

                sec_name = derive_name_from_email(em)
                conn.execute("""
                    INSERT INTO cvent_people (
                        event_id, organizer_name, contact_name, designation,
                        is_creator_email, phone, linkedin_url, email,
                        source_url, scraper_source, contact_type
                    ) VALUES (?, ?, ?, '-', FALSE, '-', '-', ?, ?, 'Cvent', ?)
                    ON CONFLICT (event_id, email) DO NOTHING
                """, (event_id, organizer, sec_name, em, source_url, contact_type))
                people_inserted += 1
                secondary_count += 1

    conn.commit()

    # Verify counts
    total_people = conn.execute("SELECT COUNT(*) FROM cvent_people").fetchone()[0]
    total_creators = conn.execute("SELECT COUNT(*) FROM cvent_people WHERE is_creator_email = TRUE").fetchone()[0]
    total_inhouse = conn.execute("SELECT COUNT(*) FROM cvent_people WHERE contact_type = 'In-House'").fetchone()[0]
    total_agency = conn.execute("SELECT COUNT(*) FROM cvent_people WHERE contact_type = '3rd-Party Agency'").fetchone()[0]
    conn.close()

    print("\n" + "=" * 55)
    print("RELATIONAL PEOPLE SYNC COMPLETE!")
    print(f"Total People in 'cvent_people': {total_people}")
    print(f"Creators (Is Creator = YES):   {total_creators}")
    print(f"Secondary Team Contacts:        {total_people - total_creators}")
    print(f"In-House Planners:             {total_inhouse}")
    print(f"3rd-Party Agency Planners:     {total_agency}")
    print("=" * 55)


if __name__ == "__main__":
    sync_all()
