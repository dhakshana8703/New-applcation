"""
Production-grade normalization and deduplication pipeline for cvent_people.
Eliminates:
  - Unicode/HTML artifacts (e.g. u003e)
  - Mismatched names (e.g. attributing James Taylor to Fleur Hollis)
  - Department/conference names masquerading as person names
  - Generic departmental inboxes falsely flagged as 'creator' (YES)
"""

import re
from db_compat import get_db_connection

GENERIC_PREFIXES = {
    'info', 'support', 'contact', 'events', 'event', 'conference', 'summit',
    'admin', 'registration', 'hello', 'help', 'sales', 'inquiry', 'inquiries',
    'office', 'mail', 'team', 'general', 'webinar', 'lawconference', 'marketing',
    'reservations', 'booking', 'reception', 'desk', 'service', 'services',
    'wyaarp', 'ndaarp', 'miaarp', 'caevents', 'nyevents'
}

DISCARD_NAME_TOKENS = {
    'conference', 'summit', 'annual', 'meeting', 'event', 'events', 'ratings',
    'team', 'staff', 'registration', 'desk', 'first last', 'planner', 'test',
    'admin', 'unknown', 'coordinator', 'group', 'association', 'department',
    'services', 'hospitality', 'foundation', 'center', 'centre'
}


def clean_email(raw):
    if not raw:
        return ""
    e = str(raw).strip().lower()
    # Strip unicode entities like u003e or \u003e or HTML entities
    e = re.sub(r'^(?:u003[ce]|\\u003[ce]|&[a-z]+;|[<>"\'])+', '', e)
    e = re.sub(r'(?:u003[ce]|\\u003[ce]|&[a-z]+;|[<>"\'])+$', '', e)
    e = e.strip()
    if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', e):
        if not any(e.endswith(bad) for bad in ['.png', '.jpg', '.jpeg', '.svg', '.gif']):
            return e
    return ""


def is_generic_email(email):
    if not email or '@' not in email:
        return True
    user = email.split('@')[0].lower()
    clean_user = re.sub(r'[0-9._\-]+$', '', user)
    if clean_user in GENERIC_PREFIXES:
        return True
    if any(user.startswith(p) for p in ['info', 'support', 'contact', 'events', 'admin', 'registration']):
        return True
    return False


def extract_name_from_email(email):
    if not email or '@' not in email:
        return "-"
    user = email.split('@')[0]
    user = re.sub(r'[0-9]+$', '', user)
    parts = re.split(r'[._\-]', user)
    if len(parts) >= 2 and all(p.isalpha() and len(p) >= 2 for p in parts[:2]):
        return f"{parts[0].capitalize()} {parts[1].capitalize()}"
    return "-"


def match_name_for_email(email, raw_contact_name):
    if not email:
        return "-"
    user = email.split('@')[0].lower()
    user_parts = [p for p in re.split(r'[0-9._\-]+', user) if len(p) >= 2]

    # Try matching against individual names in raw_contact_name
    if raw_contact_name and raw_contact_name.strip():
        candidates = [c.strip() for c in re.split(r'[,;/]+', raw_contact_name) if c.strip()]
        for cand in candidates:
            cand_lower = cand.lower()
            if any(cand_lower == d or f" {d}" in cand_lower or f"{d} " in cand_lower for d in DISCARD_NAME_TOKENS):
                continue
            cand_tokens = [t for t in re.split(r'\s+', cand_lower) if len(t) >= 2]
            if any(t in user_parts for t in cand_tokens):
                return cand.strip()

    # Derived cleanly from email username (e.g. james.taylor1 -> James Taylor)
    derived = extract_name_from_email(email)
    if derived != "-":
        return derived

    # If raw_contact_name is a clean single human name and not a generic department
    if raw_contact_name and raw_contact_name.strip():
        cand = raw_contact_name.strip()
        cand_lower = cand.lower()
        if not any(d in cand_lower for d in DISCARD_NAME_TOKENS):
            words = cand.split()
            if len(words) in [2, 3] and all(w.isalpha() for w in words):
                return cand

    return "-"


def rebuild():
    conn = get_db_connection()
    print("Clearing stale 'cvent_people' table...")
    conn.execute("DELETE FROM cvent_people")
    conn.commit()

    print("Fetching events from 'cvent' table...")
    cur = conn.cursor()
    cur.execute("""
        SELECT id, cvent_url, event_url, event_name, organizer_company,
               creator_email, contact_name, contact_phone, general_emails, contact_type
        FROM cvent
        ORDER BY id ASC
    """)
    events = cur.fetchall()

    people_inserted = 0
    creator_count = 0
    secondary_count = 0

    print(f"Normalizing contacts across {len(events)} events...")
    for ev in events:
        event_id = ev["id"]
        organizer = (ev["organizer_company"] or ev["event_name"] or "").strip()
        source_url = (ev["cvent_url"] or ev["event_url"] or "").strip()
        contact_type = (ev["contact_type"] or "In-House").strip()
        raw_contact_name = (ev["contact_name"] or "").strip()
        raw_phone = (ev["contact_phone"] or "-").strip()
        if not raw_phone:
            raw_phone = "-"

        # Parse Creator Emails
        raw_creator_str = ev["creator_email"] or ""
        creator_candidates = [clean_email(e) for e in re.split(r"[,;\s]+", raw_creator_str)]
        creator_emails = [e for e in creator_candidates if e]

        # Parse General Emails
        raw_general_str = ev["general_emails"] or ""
        general_candidates = [clean_email(e) for e in re.split(r"[,;\s]+", raw_general_str)]
        general_emails = [e for e in general_candidates if e]

        seen_emails_for_event = set()

        # 1. Process Creator Emails
        for em in creator_emails:
            if em in seen_emails_for_event:
                continue
            seen_emails_for_event.add(em)

            # Determine if genuinely a personal creator email or generic
            generic = is_generic_email(em)
            is_creator = not generic

            person_name = match_name_for_email(em, raw_contact_name)

            conn.execute("""
                INSERT INTO cvent_people (
                    event_id, organizer_name, contact_name, designation,
                    is_creator_email, phone, linkedin_url, email,
                    source_url, scraper_source, contact_type
                ) VALUES (?, ?, ?, '-', ?, ?, '-', ?, ?, 'Cvent', ?)
                ON CONFLICT (event_id, email) DO UPDATE SET
                    organizer_name = EXCLUDED.organizer_name,
                    contact_name = EXCLUDED.contact_name,
                    is_creator_email = EXCLUDED.is_creator_email,
                    phone = EXCLUDED.phone,
                    contact_type = EXCLUDED.contact_type
            """, (event_id, organizer, person_name, is_creator, raw_phone, em, source_url, contact_type))

            people_inserted += 1
            if is_creator:
                creator_count += 1
            else:
                secondary_count += 1

        # 2. Process General Team Emails
        for em in general_emails:
            if em in seen_emails_for_event:
                continue
            seen_emails_for_event.add(em)

            person_name = match_name_for_email(em, raw_contact_name)

            conn.execute("""
                INSERT INTO cvent_people (
                    event_id, organizer_name, contact_name, designation,
                    is_creator_email, phone, linkedin_url, email,
                    source_url, scraper_source, contact_type
                ) VALUES (?, ?, ?, '-', FALSE, '-', '-', ?, ?, 'Cvent', ?)
                ON CONFLICT (event_id, email) DO NOTHING
            """, (event_id, organizer, person_name, em, source_url, contact_type))

            people_inserted += 1
            secondary_count += 1

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM cvent_people").fetchone()[0]
    verified_creators = conn.execute("SELECT COUNT(*) FROM cvent_people WHERE is_creator_email = TRUE").fetchone()[0]
    sec = conn.execute("SELECT COUNT(*) FROM cvent_people WHERE is_creator_email = FALSE").fetchone()[0]
    conn.close()

    print("\n" + "=" * 55)
    print("CLEAN NORMALIZATION COMPLETE!")
    print(f"Total Normalized People: {total}")
    print(f"Genuine Creators (YES):   {verified_creators}")
    print(f"General / Secondary (NO): {sec}")
    print("=" * 55)


if __name__ == "__main__":
    rebuild()
