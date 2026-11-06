"""
Ground-Truth Re-extraction Engine for Cvent
Authoritative, concurrent, multi-entity scraper & reconciler.
"""

import sys
import re
import json
import time
from urllib.parse import urlparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = 'postgresql://neondb_owner:npg_IGE71mZURpNc@ep-sparkling-breeze-aodc615z-pooler.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
}

GENERIC_PREFIXES = {
    'info', 'support', 'events', 'event', 'conference', 'registration',
    'inquiries', 'help', 'admin', 'contact', 'hello', 'team', 'office',
    'service', 'rsvp', 'webinar', 'general', 'desk', 'marketing',
    'sales', 'membership', 'communications', 'operations', 'billing', 'muconf'
}

DISCARD_NAME_TOKENS = {
    'first name', 'last name', 'first last', 'planner first', 'planner last',
    'planner first planner last', 'first name last name', 'test planner',
    'admin', 'summary', 'welcome', 'ratings', 'cvent', 'online registration',
    'event planner', 'meeting planner', 'organizer', 'speaker', 'tbd', 'tba',
    'office', 'conference office', 'department', 'committee', 'association'
}

KNOWN_AGENCIES = [
    'demarsemeetings', '300brand', 'fanfares', 'conferencedirect',
    'helmsbriscoe', 'executivevents', 'powereventgroup', 'signalhouseevents',
    'jkingconsult', 'factory73', 'micepodium', 'premiogroup'
]

AGENCY_KEYWORDS = [
    'demarsemeetings', 'meetings', 'eventgroup', 'eventagency', 'eventproduction',
    'associationsmgt', '-pco', '_pco', 'pco-'
]


def clean_email(raw_email: str) -> str:
    """Strip unicode escape sequences, html tags, leading u003e, and whitespace."""
    if not raw_email:
        return ""
    e = raw_email.strip().lower()
    e = re.sub(r'^(?:u003[ce]|\\u003[ce]|&[a-z]+;|[<>"\'])+', '', e)
    e = re.sub(r'(?:[<>"\';]+|\s)+$', '', e)
    # Check valid email format
    if re.match(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$', e):
        # Ignore system domains
        domain = e.split('@')[-1]
        if any(d in domain for d in ['cvent.com', 'schema.org', 'example.com', 'w3.org']):
            return ""
        return e
    return ""


def is_generic_email(email: str) -> bool:
    if not email or '@' not in email:
        return False
    user = email.split('@')[0].lower()
    clean_user = re.sub(r'[0-9_\-\.]+', '', user)
    if user in GENERIC_PREFIXES or clean_user in GENERIC_PREFIXES:
        return True
    if any(p in user for p in ['conference', 'lawconference', 'annualmeeting', 'registration', 'events']):
        return True
    return False


def determine_contact_type(email: str) -> str:
    if not email:
        return "In-House"
    domain = email.split('@')[-1].lower()
    company = domain.split('.')[0]
    if any(ag in company for ag in KNOWN_AGENCIES):
        return "3rd-Party Agency"
    if any(k in company for k in AGENCY_KEYWORDS):
        return "3rd-Party Agency"
    return "In-House"


COMMON_FIRST_NAMES = {
    'brian', 'hannah', 'matt', 'matthew', 'sarah', 'anna', 'john', 'david',
    'alex', 'chris', 'michael', 'jessica', 'jennifer', 'emily', 'daniel',
    'james', 'robert', 'mary', 'lisa', 'paul', 'mark', 'kevin', 'jason',
    'laura', 'rachel', 'ashley', 'amanda', 'stephanie', 'nicole', 'justin',
    'brandon', 'eric', 'steven', 'andrew', 'ryan', 'joshua', 'sam', 'samuel',
    'tom', 'thomas', 'peter', 'ben', 'benjamin', 'adam', 'dan', 'steve', 'joe'
}

DEPARTMENT_WORDS = {
    'courses', 'course', 'rehab', 'training', 'workshop', 'office', 'chapter',
    'program', 'services', 'service', 'center', 'centre', 'institute', 'group',
    'events', 'event', 'conference', 'summit', 'meeting', 'team', 'staff',
    'admin', 'desk', 'support', 'help', 'info', 'inquiry', 'inquiries', 'rsvp'
}


def derive_name_from_email(email: str) -> str:
    if not email or '@' not in email:
        return "-"
    user = email.split('@')[0].lower()
    clean_user = re.sub(r'[0-9]+', '', user)
    
    # Check for department/functional words
    if any(dw in clean_user for dw in DEPARTMENT_WORDS):
        return "-"

    # 1. dot, underscore, or dash separated (e.g. james.taylor1 or fleur_hollis)
    parts = re.split(r'[._\-]+', user)
    clean_parts = [re.sub(r'[0-9]+', '', p) for p in parts if len(p) >= 2]
    clean_parts = [p for p in clean_parts if p.isalpha() and len(p) >= 2]
    if len(clean_parts) >= 2:
        return f"{clean_parts[0].capitalize()} {clean_parts[1].capitalize()}"

    # 2. Known single first names (e.g. brian -> Brian, hannah -> Hannah)
    if clean_user in COMMON_FIRST_NAMES:
        return clean_user.capitalize()

    # 3. single token: initial + surname (e.g. sgerzina -> S. Gerzina, equinlivan -> E. Quinlivan)
    if len(clean_user) >= 5 and clean_user.isalpha():
        initial = clean_user[0]
        remainder = clean_user[1:]
        if remainder in COMMON_FIRST_NAMES or remainder in {'ian', 'annah', 'on', 'in'}:
            return "-"
        return f"{initial.upper()}. {remainder.capitalize()}"

    return "-"


def reconcile_contact_name(email: str, planner_name: str) -> str:
    """
    Strict reconciler: ONLY pair planner_name with email if there is unambiguous
    token overlap. Otherwise, derive name from email or return '-'.
    NEVER pair Aleksandra Gorczynska with sgerzina!
    """
    if is_generic_email(email):
        return "-"

    user = email.split('@')[0].lower()
    user_tokens = set(re.split(r'[0-9._\-]+', user))

    if planner_name and planner_name.strip() and planner_name != "-":
        pn_clean = planner_name.strip()
        pn_lower = pn_clean.lower()
        
        # Check against discard list
        if not any(d in pn_lower for d in DISCARD_NAME_TOKENS):
            name_parts = [p for p in re.split(r'\s+', pn_lower) if len(p) >= 2]
            # Match condition: at least one substantial name part is present in email username
            if any(p in user for p in name_parts) or any(t in pn_lower for t in user_tokens if len(t) >= 3):
                return pn_clean

    # Fallback to deriving name from email itself
    derived = derive_name_from_email(email)
    if derived != "-":
        return derived

    return "-"


def scrape_event_page(url: str, session: requests.Session) -> dict:
    """
    Fetches raw HTML and extracts structured entities.
    """
    try:
        resp = session.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        html = resp.text
    except Exception as e:
        return {"error": str(e)}

    # 1. ContactPlanner Structured Widget
    planner_name = ""
    planner_phone = ""
    planner_company = ""
    
    cp_blocks = re.findall(r'ContactPlanner[\\"]+:\s*\{([^}]+)\}', html)
    for block in cp_blocks:
        fn = re.search(r'firstName[\\"]+:\s*[\\"]*([^\\",}]+)', block)
        ln = re.search(r'lastName[\\"]+:\s*[\\"]*([^\\",}]+)', block)
        ph = re.search(r'phoneNumber[\\"]+:\s*[\\"]*([^\\",}]+)', block)
        co = re.search(r'company[\\"]+:\s*[\\"]*([^\\",}]+)', block)
        
        f_str = fn.group(1).strip() if fn else ""
        l_str = ln.group(1).strip() if ln else ""
        full_name = f"{f_str} {l_str}".strip()
        
        if full_name and not any(d in full_name.lower() for d in DISCARD_NAME_TOKENS):
            planner_name = full_name
        if ph and ph.group(1).strip():
            clean_ph = re.sub(r'[^\d+()\s\-]', '', ph.group(1).strip())
            if len(re.sub(r'[^\d]', '', clean_ph)) >= 7:
                planner_phone = clean_ph
        if co and co.group(1).strip():
            planner_company = co.group(1).strip()

    # Fallback planner name from explicit first/last pairs if still empty
    if not planner_name:
        pairs = re.findall(r'firstName[\\"]+:\s*[\\"]*([^\\",}]+)[\\"]*,\s*lastName[\\"]+:\s*[\\"]*([^\\",}]+)', html)
        for f, l in pairs:
            cand = f"{f} {l}".strip()
            if cand and len(cand) < 50 and not any(d in cand.lower() for d in DISCARD_NAME_TOKENS):
                planner_name = cand
                break

    # 2. General Phones from tel: or json
    phones = []
    if planner_phone:
        phones.append(planner_phone)
    for tel in re.findall(r'tel:([+0-9\(\)\s\-\.]{7,25})', html):
        clean_ph = re.sub(r'[^\d+()\s\-]', '', tel.strip())
        if len(re.sub(r'[^\d]', '', clean_ph)) >= 7 and clean_ph not in phones:
            phones.append(clean_ph)

    # 3. Creator Emails (createdBy / lastModifiedBy)
    created_bys = set(re.findall(r'createdBy[\\"]+:\s*[\\"]+([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', html))
    last_mods = set(re.findall(r'lastModifiedBy[\\"]+:\s*[\\"]+([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', html))
    
    creator_emails = []
    for em in (created_bys | last_mods):
        cleaned = clean_email(em)
        if cleaned and cleaned not in creator_emails:
            creator_emails.append(cleaned)

    # 4. General / Inquiries Emails (mailto + DOM text)
    mailtos = set(re.findall(r'mailto:([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', html, re.I))
    all_dom_emails = set(re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', html))
    
    general_emails = []
    for em in (mailtos | all_dom_emails):
        cleaned = clean_email(em)
        if cleaned and cleaned not in creator_emails and cleaned not in general_emails:
            general_emails.append(cleaned)

    return {
        "planner_name": planner_name,
        "planner_phone": planner_phone,
        "general_phones": phones,
        "creator_emails": creator_emails,
        "general_emails": general_emails
    }


def run_pipeline():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()

    print("=" * 60)
    print("STARTING CVENT GROUND-TRUTH EXTRACTION PIPELINE")
    print("=" * 60)

    cur.execute("SELECT id, cvent_url, event_url, organizer_company, event_name FROM cvent ORDER BY id ASC")
    events = cur.fetchall()
    total_events = len(events)
    print(f"Loaded {total_events} events from 'cvent' table.")

    t0 = time.time()
    results = {}

    def worker(ev):
        ev_id, cvent_url, event_url, org, name = ev
        target_url = event_url if (event_url and 'http' in event_url) else cvent_url
        if not target_url:
            return ev_id, None
        session = requests.Session()
        try:
            data = scrape_event_page(target_url, session)
        except Exception as ex:
            data = {"error": str(ex)}
        return ev_id, data

    print(f"Scraping live Cvent pages concurrently using 20 workers...")
    completed = 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(worker, ev): ev for ev in events}
        for fut in as_completed(futures):
            ev_id, data = fut.result()
            results[ev_id] = data
            completed += 1
            if completed % 50 == 0 or completed == total_events:
                print(f"  Progress: {completed}/{total_events} events extracted ({time.time() - t0:.1f}s)")

    print(f"\nAll {total_events} events extracted in {time.time() - t0:.1f}s!")

    print("\nRebuilding 'cvent_people' and updating 'cvent' table with verified records...")
    
    # Clear current cvent_people
    cur.execute("DELETE FROM cvent_people")

    cvent_updates = []
    people_records = []

    for ev in events:
        ev_id, cvent_url, event_url, org, ev_name = ev
        data = results.get(ev_id)
        if not data or "error" in data:
            continue

        planner_name = data.get("planner_name", "")
        planner_phone = data.get("planner_phone", "")
        phones = data.get("general_phones", [])
        creator_emails = data.get("creator_emails", [])
        general_emails = data.get("general_emails", [])

        # Default phone
        default_phone = planner_phone or (phones[0] if phones else "-")

        # Update cvent table summary fields
        primary_creator = creator_emails[0] if creator_emails else ""
        cvent_updates.append((
            planner_name or "-",
            planner_phone or "-",
            primary_creator,
            ", ".join(general_emails[:5]),
            determine_contact_type(primary_creator or (general_emails[0] if general_emails else "")),
            ev_id
        ))

        # Build discrete people records
        seen_emails_for_event = set()

        # 1. Creator emails
        for em in creator_emails:
            if em in seen_emails_for_event:
                continue
            seen_emails_for_event.add(em)
            
            is_gen = is_generic_email(em)
            is_creator = not is_gen
            c_name = reconcile_contact_name(em, planner_name)
            c_type = determine_contact_type(em)
            # phone is planner_phone only if name matches planner_name
            ph = planner_phone if (c_name == planner_name and planner_phone) else "-"

            people_records.append((
                ev_id,
                org or "Cvent Event",
                c_name,
                "-",
                is_creator,
                ph,
                "-",
                em,
                cvent_url or event_url,
                "Cvent",
                c_type
            ))

        # 2. General / Inquiries emails
        for em in general_emails:
            if em in seen_emails_for_event:
                continue
            seen_emails_for_event.add(em)

            is_creator = False
            c_name = reconcile_contact_name(em, planner_name)
            c_type = determine_contact_type(em)
            ph = planner_phone if (c_name == planner_name and planner_phone) else (phones[0] if phones else "-")

            people_records.append((
                ev_id,
                org or "Cvent Event",
                c_name,
                "-",
                is_creator,
                ph,
                "-",
                em,
                cvent_url or event_url,
                "Cvent",
                c_type
            ))

    # Batch update cvent table
    print(f"Updating 'cvent' table with {len(cvent_updates)} refreshed events...")
    for u in cvent_updates:
        cur.execute("""
            UPDATE cvent 
            SET contact_name = %s, contact_phone = %s, creator_email = %s, 
                general_emails = %s, contact_type = %s
            WHERE id = %s
        """, u)

    # Batch insert into cvent_people
    print(f"Inserting {len(people_records)} discrete, verified contacts into 'cvent_people'...")
    insert_sql = """
        INSERT INTO cvent_people (
            event_id, organizer_name, contact_name, designation, is_creator_email,
            phone, linkedin_url, email, source_url, scraper_source, contact_type
        ) VALUES %s
        ON CONFLICT (event_id, email) DO NOTHING
    """
    execute_values(cur, insert_sql, people_records)

    # RUN AUTOMATED INTEGRITY AUDIT
    print("\n" + "=" * 60)
    print("RUNNING AUTOMATED INTEGRITY AUDIT (100% OF ROWS)")
    print("=" * 60)

    cur.execute("SELECT id, organizer_name, contact_name, is_creator_email, email FROM cvent_people")
    all_people = cur.fetchall()
    total_people = len(all_people)

    unicode_errors = []
    generic_as_creator_errors = []
    mismatch_errors = []

    for p in all_people:
        p_id, p_org, p_name, p_is_creator, p_email = p

        # Check 1: Unicode/HTML artifacts
        if any(bad in p_email.lower() for bad in ['u003', '&', '<', '>', '"', "'"]):
            unicode_errors.append((p_id, p_email))

        # Check 2: Generic email marked as creator
        if p_is_creator and is_generic_email(p_email):
            generic_as_creator_errors.append((p_id, p_email))

        # Check 3: Blatant mismatch (e.g. Aleksandra with sgerzina)
        if p_name and p_name != "-":
            u_tokens = set(re.split(r'[0-9._\-]+', p_email.split('@')[0].lower()))
            n_tokens = set(re.split(r'\s+', p_name.lower()))
            # If name is a multi-word person name, verify token overlap or derived match
            if len(n_tokens) >= 2:
                has_overlap = any(nt in p_email.lower() for nt in n_tokens if len(nt) >= 3) or any(ut in p_name.lower() for ut in u_tokens if len(ut) >= 3)
                derived = derive_name_from_email(p_email)
                if not has_overlap and derived != p_name:
                    mismatch_errors.append((p_id, p_org, p_name, p_email))

    print(f"Total Contacts Verified: {total_people}")
    print(f"Unicode/HTML Entities:   {len(unicode_errors)} errors")
    print(f"Generic Marked Creator:  {len(generic_as_creator_errors)} errors")
    print(f"Identity Mismatches:     {len(mismatch_errors)} errors")

    # Specific Spot-Check: Akamai
    print("\n--- SPOT-CHECK: Akamai Record ---")
    cur.execute("SELECT id, organizer_name, contact_name, is_creator_email, email FROM cvent_people WHERE organizer_name ILIKE '%Akamai%' AND email = 'sgerzina@akamai.com'")
    akamai_rows = cur.fetchall()
    for row in akamai_rows:
        print(f"  Result: ID={row[0]} | Org={row[1]} | Contact={row[2]} | IsCreator={row[3]} | Email={row[4]}")

    # Specific Spot-Check: S&P Global
    print("\n--- SPOT-CHECK: S&P Global Record ---")
    cur.execute("SELECT id, organizer_name, contact_name, is_creator_email, email FROM cvent_people WHERE email ILIKE '%spglobal.com%'")
    sp_rows = cur.fetchall()
    for row in sp_rows:
        print(f"  Result: ID={row[0]} | Org={row[1]} | Contact={row[2]} | IsCreator={row[3]} | Email={row[4]}")

    conn.close()
    print("\n" + "=" * 60)
    print("PIPELINE & AUDIT COMPLETE!")
    print("=" * 60)


if __name__ == '__main__':
    run_pipeline()
