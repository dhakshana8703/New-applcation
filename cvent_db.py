"""
Dedicated, isolated database module for Cvent.
Strictly operates on:
  - 'cvent' table (Cvent Events)
  - 'cvent_companies' table (Cvent Companies)
Zero manager dependencies, zero legacy tables.
"""

import os
import re
import csv
import io
from urllib.parse import urlparse
from datetime import datetime
from db_compat import get_db_connection, adapt_create_table_sql, safe_alter_add_column

_tables_initialized = False


def init_cvent_tables():
    """Ensure cvent and cvent_companies tables and indexes exist."""
    global _tables_initialized
    if _tables_initialized:
        return
    conn = get_db_connection()

    # 1. Main Cvent Events Table (The 11 verified fields + status + company_id)
    conn.execute(adapt_create_table_sql("""
        CREATE TABLE IF NOT EXISTS cvent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cvent_url TEXT UNIQUE NOT NULL,
            event_url TEXT DEFAULT '',
            event_name TEXT NOT NULL,
            start_date TEXT DEFAULT '',
            end_date TEXT DEFAULT '',
            venue_name TEXT DEFAULT '',
            city TEXT DEFAULT '',
            state TEXT DEFAULT '',
            country TEXT DEFAULT '',
            organizer_company TEXT DEFAULT '',
            creator_email TEXT DEFAULT '',
            status TEXT DEFAULT 'incomplete',
            company_id INTEGER,
            scrape_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_unique_url ON cvent(cvent_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_org_comp ON cvent(organizer_company)")
    safe_alter_add_column(conn, "cvent", "company_id", "INTEGER")
    safe_alter_add_column(conn, "cvent", "contact_name", "TEXT DEFAULT ''")
    safe_alter_add_column(conn, "cvent", "contact_phone", "TEXT DEFAULT ''")
    safe_alter_add_column(conn, "cvent", "general_emails", "TEXT DEFAULT ''")
    safe_alter_add_column(conn, "cvent", "contact_type", "TEXT DEFAULT 'In-House'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_company_id ON cvent(company_id)")

    # 2. Pure Dedicated Cvent Companies Table
    conn.execute(adapt_create_table_sql("""
        CREATE TABLE IF NOT EXISTS cvent_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            domain TEXT DEFAULT '',
            website TEXT DEFAULT '',
            email TEXT DEFAULT '',
            city TEXT DEFAULT '',
            state TEXT DEFAULT '',
            country TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_comp_name ON cvent_companies(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_comp_domain ON cvent_companies(domain)")

    # 3. Pure Dedicated Cvent People Table (Relational & Extensible)
    conn.execute(adapt_create_table_sql("""
        CREATE TABLE IF NOT EXISTS cvent_people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES cvent(id) ON DELETE CASCADE,
            organizer_name TEXT DEFAULT '',
            contact_name TEXT DEFAULT '',
            designation TEXT DEFAULT '-',
            is_creator_email BOOLEAN DEFAULT FALSE,
            phone TEXT DEFAULT '-',
            linkedin_url TEXT DEFAULT '-',
            email TEXT NOT NULL,
            source_url TEXT DEFAULT '',
            scraper_source TEXT DEFAULT 'Cvent',
            contact_type TEXT DEFAULT 'In-House',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_event_person_email UNIQUE (event_id, email)
        )
    """))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_people_email ON cvent_people(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_people_event_id ON cvent_people(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_people_is_creator ON cvent_people(is_creator_email)")

    conn.commit()
    conn.close()
    _tables_initialized = True


def get_connection():
    """Get pooled DB connection."""
    return get_db_connection()


def _get_scalar(row, default=0):
    if row is None:
        return default
    try:
        val = row[0] if not hasattr(row, "keys") else list(row.values())[0]
        return val if val is not None else default
    except Exception:
        return default


def compute_status(event_url, creator_email):
    """Determine event completeness status."""
    if creator_email and creator_email.strip():
        return "complete"
    elif event_url and event_url.strip():
        return "has-website"
    return "incomplete"


# ==============================================================================
# CVENT EVENTS
# ==============================================================================

def get_existing_cvent_urls() -> set:
    """Return a set of all canonical cvent_urls already stored in the database."""
    conn = get_connection()
    rows = conn.execute("SELECT cvent_url FROM cvent WHERE cvent_url IS NOT NULL AND cvent_url != ''").fetchall()
    conn.close()
    urls = set()
    for r in rows:
        u = r["cvent_url"] if hasattr(r, "keys") else r[0]
        if u:
            urls.add(u.strip())
    return urls


def upsert_cvent_event(data):
    """
    Insert or update a verified Cvent event by unique cvent_url.
    Returns the event id.
    """
    cvent_url = data.get("cvent_url", "").strip()
    if not cvent_url:
        return None

    event_url = data.get("event_url", "").strip()
    event_name = data.get("event_name", "").strip() or "Untitled Cvent Event"
    start_date = data.get("start_date", "").strip()
    end_date = data.get("end_date", "").strip()
    venue_name = data.get("venue_name", "").strip()
    city = data.get("city", "").strip()
    state = data.get("state", "").strip()
    country = data.get("country", "").strip()
    organizer_company = data.get("organizer_company", "").strip()
    creator_email = data.get("creator_email", "").strip()
    contact_name = data.get("contact_name", "").strip()
    contact_phone = data.get("contact_phone", "").strip()
    general_emails = data.get("general_emails", "").strip()
    contact_type = data.get("contact_type", "").strip() or "In-House"
    status = compute_status(event_url, creator_email)

    conn = get_connection()
    row = conn.execute("SELECT id FROM cvent WHERE cvent_url = ?", (cvent_url,)).fetchone()

    if row:
        event_id = row["id"] if hasattr(row, "keys") else row[0]
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
                contact_name = CASE WHEN ? != '' THEN ? ELSE contact_name END,
                contact_phone = CASE WHEN ? != '' THEN ? ELSE contact_phone END,
                general_emails = CASE WHEN ? != '' THEN ? ELSE general_emails END,
                contact_type = CASE WHEN ? != '' THEN ? ELSE contact_type END,
                status = ?,
                scrape_date = CURRENT_TIMESTAMP
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
            organizer_company, organizer_company,
            creator_email, creator_email,
            contact_name, contact_name,
            contact_phone, contact_phone,
            general_emails, general_emails,
            contact_type, contact_type,
            status,
            event_id
        ))
    else:
        cur = conn.execute("""
            INSERT INTO cvent (
                cvent_url, event_url, event_name, start_date, end_date,
                venue_name, city, state, country, organizer_company,
                creator_email, contact_name, contact_phone, general_emails,
                contact_type, status, scrape_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            cvent_url, event_url, event_name, start_date, end_date,
            venue_name, city, state, country, organizer_company,
            creator_email, contact_name, contact_phone, general_emails,
            contact_type, status
        ))
        event_id = cur.lastrowid

    # Automatically derive & keep people in sync
    if event_id:
        try:
            sync_event_to_people(event_id, conn)
        except Exception:
            pass

    conn.commit()
    conn.close()
    return event_id


def get_all_cvent_events(limit=50, offset=0, search="", status_filter="all", sort_by="default", sort_dir="asc"):
    """Fetch paginated, filtered Cvent events from the 'cvent' table."""
    conn = get_connection()
    conditions = []
    params = []

    if search:
        s = f"%{search.lower()}%"
        conditions.append("""(
            LOWER(c.event_name) LIKE ? OR
            LOWER(c.organizer_company) LIKE ? OR
            LOWER(c.city) LIKE ? OR
            LOWER(c.country) LIKE ? OR
            LOWER(c.creator_email) LIKE ?
        )""")
        params.extend([s, s, s, s, s])

    if status_filter and status_filter != "all":
        conditions.append("c.status = ?")
        params.append(status_filter)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Smart order: prioritized rows with dates & emails, or explicit sort
    order_clause = "ORDER BY c.id ASC"
    if sort_by == "name":
        order_clause = f"ORDER BY c.event_name {'DESC' if sort_dir == 'desc' else 'ASC'}"
    elif sort_by == "date":
        order_clause = f"ORDER BY (CASE WHEN c.start_date = '' THEN 1 ELSE 0 END), c.start_date {'DESC' if sort_dir == 'desc' else 'ASC'}"
    elif sort_by == "company":
        order_clause = f"ORDER BY (CASE WHEN c.organizer_company = '' THEN 1 ELSE 0 END), c.organizer_company {'DESC' if sort_dir == 'desc' else 'ASC'}"
    elif sort_by == "city":
        order_clause = f"ORDER BY (CASE WHEN c.city = '' THEN 1 ELSE 0 END), c.city {'DESC' if sort_dir == 'desc' else 'ASC'}"
    elif sort_by == "id_desc":
        order_clause = "ORDER BY c.id DESC"
    elif sort_by == "default":
        # High-fidelity events with both dates & emails first
        order_clause = "ORDER BY (CASE WHEN c.start_date != '' AND c.creator_email != '' THEN 0 WHEN c.creator_email != '' THEN 1 ELSE 2 END), c.id ASC"

    # Single-roundtrip query with COUNT(*) OVER() and relational people_count
    data_sql = f"""
        SELECT c.id, c.cvent_url, c.event_url, c.event_name, c.start_date, c.end_date,
               c.venue_name, c.city, c.state, c.country, c.organizer_company,
               c.creator_email, c.status, c.company_id, c.scrape_date,
               comp.name as company_name, comp.domain as company_domain, comp.website as company_website,
               COUNT(*) OVER() as full_count,
               COALESCE(p_counts.people_count, 0) as people_count
        FROM cvent c
        LEFT JOIN cvent_companies comp ON c.company_id = comp.id
        LEFT JOIN (
            SELECT event_id, COUNT(*) as people_count
            FROM cvent_people
            GROUP BY event_id
        ) p_counts ON c.id = p_counts.event_id
        {where_clause}
        {order_clause}
        LIMIT ? OFFSET ?
    """
    data_params = list(params) + [limit, offset]
    rows = conn.execute(data_sql, data_params).fetchall()
    conn.close()

    total_count = 0
    if rows:
        total_count = rows[0].get("full_count", 0) if hasattr(rows[0], "get") else rows[0]["full_count"]

    events_list = []
    for r in rows:
        d = dict(r)
        d["scraper_source"] = "Cvent"
        d["email_count"] = d.get("people_count", 0)
        d["organizer_website"] = d.get("company_website") or (f"https://{d['company_domain']}" if d.get("company_domain") else "")
        events_list.append(d)

    return {
        "events": events_list,
        "total": total_count,
        "limit": limit,
        "offset": offset
    }


def get_all_stats():
    """Return top-level KPI metrics across Events and Companies in a single pooled query."""
    conn = get_connection()
    ev_row = conn.execute("""
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN creator_email != '' AND creator_email IS NOT NULL THEN 1 END) as with_emails,
            COUNT(CASE WHEN event_url != '' AND event_url IS NOT NULL THEN 1 END) as with_website,
            COUNT(CASE WHEN organizer_company != '' AND organizer_company IS NOT NULL THEN 1 END) as with_company,
            COUNT(CASE WHEN company_id IS NOT NULL THEN 1 END) as linked,
            COUNT(CASE WHEN status = 'incomplete' THEN 1 END) as incomplete,
            MAX(scrape_date) as latest
        FROM cvent
    """).fetchone()

    comp_row = conn.execute("""
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN email != '' AND email IS NOT NULL THEN 1 END) as with_email,
            COUNT(CASE WHEN domain != '' AND domain IS NOT NULL THEN 1 END) as with_domain
        FROM cvent_companies
    """).fetchone()

    people_row = conn.execute("""
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN is_creator_email = TRUE THEN 1 END) as creators,
            COUNT(CASE WHEN contact_type = '3rd-Party Agency' THEN 1 END) as agencies,
            COUNT(CASE WHEN phone != '-' AND phone != '' AND phone IS NOT NULL THEN 1 END) as with_phone
        FROM cvent_people
    """).fetchone()
    conn.close()

    return {
        "events": {
            "total_events": ev_row["total"] if ev_row else 0,
            "with_emails": ev_row["with_emails"] if ev_row else 0,
            "with_website": ev_row["with_website"] if ev_row else 0,
            "with_company": ev_row["with_company"] if ev_row else 0,
            "linked_events": ev_row["linked"] if ev_row else 0,
            "incomplete": ev_row["incomplete"] if ev_row else 0,
            "latest_scrape": str(ev_row["latest"])[:19] if (ev_row and ev_row["latest"]) else "None"
        },
        "companies": {
            "total": comp_row["total"] if comp_row else 0,
            "with_email": comp_row["with_email"] if comp_row else 0,
            "with_domain": comp_row["with_domain"] if comp_row else 0
        },
        "people": {
            "total": people_row["total"] if people_row else 0,
            "creators": people_row["creators"] if people_row else 0,
            "agencies": people_row["agencies"] if people_row else 0,
            "with_phone": people_row["with_phone"] if people_row else 0
        }
    }


def get_cvent_stats():
    """Returns overview metrics for Cvent events in a single fast query."""
    conn = get_connection()
    row = conn.execute("""
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN creator_email != '' AND creator_email IS NOT NULL THEN 1 END) as with_emails,
            COUNT(CASE WHEN event_url != '' AND event_url IS NOT NULL THEN 1 END) as with_website,
            COUNT(CASE WHEN organizer_company != '' AND organizer_company IS NOT NULL THEN 1 END) as with_company,
            COUNT(CASE WHEN company_id IS NOT NULL THEN 1 END) as linked,
            COUNT(CASE WHEN status = 'incomplete' THEN 1 END) as incomplete,
            MAX(scrape_date) as latest
        FROM cvent
    """).fetchone()
    conn.close()

    if not row:
        return {"total_events": 0, "with_emails": 0, "with_website": 0, "with_company": 0, "linked_events": 0, "incomplete": 0, "latest_scrape": "None"}

    return {
        "total_events": row["total"] or 0,
        "with_emails": row["with_emails"] or 0,
        "with_website": row["with_website"] or 0,
        "with_company": row["with_company"] or 0,
        "linked_events": row["linked"] or 0,
        "incomplete": row["incomplete"] or 0,
        "latest_scrape": str(row["latest"])[:19] if row["latest"] else "None"
    }


def get_cvent_event_by_id(event_id: int):
    """Fetch single event by ID from cvent table."""
    conn = get_connection()
    row = conn.execute("""
        SELECT c.*, comp.name as company_name, comp.domain as company_domain
        FROM cvent c
        LEFT JOIN cvent_companies comp ON c.company_id = comp.id
        WHERE c.id = ?
    """, (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_cvent_event(event_id: int, updates: dict):
    """Update editable fields for a Cvent event."""
    if not updates:
        return False

    allowed_cols = {
        "event_name", "event_url", "start_date", "end_date",
        "venue_name", "city", "state", "country",
        "organizer_company", "creator_email", "status", "company_id"
    }

    set_clauses = []
    params = []

    for k, v in updates.items():
        if k in allowed_cols:
            set_clauses.append(f"{k} = ?")
            params.append(str(v).strip() if v is not None else "")

    if not set_clauses:
        return False

    set_clauses.append("scrape_date = CURRENT_TIMESTAMP")
    sql = f"UPDATE cvent SET {', '.join(set_clauses)} WHERE id = ?"
    params.append(event_id)

    conn = get_connection()
    conn.execute(sql, params)
    conn.commit()
    conn.close()
    return True


def delete_cvent_event(event_id: int):
    """Delete a single event by ID from cvent table."""
    conn = get_connection()
    conn.execute("DELETE FROM cvent WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    return True


def bulk_delete_cvent_events(event_ids: list):
    """Delete multiple events by ID list from cvent table."""
    if not event_ids:
        return 0
    conn = get_connection()
    placeholders = ",".join("?" for _ in event_ids)
    conn.execute(f"DELETE FROM cvent WHERE id IN ({placeholders})", tuple(event_ids))
    conn.commit()
    conn.close()
    return len(event_ids)


# ==============================================================================
# CVENT COMPANIES
# ==============================================================================

def extract_domain_from_email(email: str) -> str:
    """Extract company domain from creator email, ignoring generic providers."""
    if not email or "@" not in email:
        return ""
    generic = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "aol.com", "cvent.com", "cventmail.com", "proton.me", "protonmail.com"
    }
    for em in email.replace(";", ",").split(","):
        em = em.strip()
        if "@" in em:
            dom = em.split("@")[-1].lower().strip()
            if dom and dom not in generic:
                return dom
    return ""


def extract_domain_from_url(url: str) -> str:
    """Extract domain from website URL, ignoring generic event/social sites."""
    if not url:
        return ""
    generic = {
        "cvent.com", "web.cvent.com", "events.cvent.com", "facebook.com",
        "linkedin.com", "twitter.com", "youtube.com", "instagram.com"
    }
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().strip()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        if hostname and hostname not in generic:
            return hostname
    except Exception:
        pass
    return ""


def extract_companies_from_cvent(event_ids: list = None) -> dict:
    """
    Extract unique companies from 'cvent' table into dedicated 'cvent_companies'.
    Deduplicates by company domain (or organizer company name if no domain).
    Creates new records in 'cvent_companies' table and links cvent.company_id.
    """
    conn = get_connection()

    existing_comps = conn.execute("SELECT id, name, domain FROM cvent_companies").fetchall()
    domain_to_id = {}
    name_to_id = {}
    for c in existing_comps:
        cid = c["id"] if hasattr(c, "keys") else c[0]
        cname = (c["name"] if hasattr(c, "keys") else c[1]) or ""
        cdom = (c["domain"] if hasattr(c, "keys") else c[2]) or ""
        if cdom:
            domain_to_id[cdom.lower().strip()] = cid
        if cname:
            name_to_id[cname.lower().strip()] = cid

    conds = ["(organizer_company != '' OR creator_email != '' OR event_url != '')"]
    params = []
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        conds.append(f"id IN ({placeholders})")
        params.extend(event_ids)

    where = " AND ".join(conds)
    rows = conn.execute(f"""
        SELECT id, organizer_company, creator_email, event_url, city, state, country, company_id
        FROM cvent
        WHERE {where}
    """, params).fetchall()

    created = 0
    linked = 0

    for r in rows:
        row_id = r["id"] if hasattr(r, "keys") else r[0]
        org_name = ((r["organizer_company"] if hasattr(r, "keys") else r[1]) or "").strip()
        email = ((r["creator_email"] if hasattr(r, "keys") else r[2]) or "").strip()
        event_url = ((r["event_url"] if hasattr(r, "keys") else r[3]) or "").strip()
        city = ((r["city"] if hasattr(r, "keys") else r[4]) or "").strip()
        state = ((r["state"] if hasattr(r, "keys") else r[5]) or "").strip()
        country = ((r["country"] if hasattr(r, "keys") else r[6]) or "").strip()

        domain = extract_domain_from_email(email) or extract_domain_from_url(event_url)

        if not org_name and not domain:
            continue

        name = org_name or domain.split(".")[0].capitalize()
        company_id = None

        if domain and domain.lower() in domain_to_id:
            company_id = domain_to_id[domain.lower()]
        elif org_name and org_name.lower() in name_to_id:
            company_id = name_to_id[org_name.lower()]

        if not company_id:
            cursor = conn.execute(
                """INSERT INTO cvent_companies (name, domain, website, email, city, state, country)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, domain or None, event_url or None, email or None, city or None, state or None, country or None)
            )
            company_id = cursor.lastrowid
            created += 1
            if domain:
                domain_to_id[domain.lower()] = company_id
            if name:
                name_to_id[name.lower()] = company_id

        conn.execute("UPDATE cvent SET company_id = ? WHERE id = ?", (company_id, row_id))
        linked += 1

    conn.commit()
    total_companies = _get_scalar(conn.execute("SELECT COUNT(*) FROM cvent_companies").fetchone(), 0)
    conn.close()
    return {"created": created, "linked": linked, "total": total_companies}


def get_all_cvent_companies(limit=50, offset=0, search="", filter_type="all"):
    """Get all Cvent companies with event counts and pagination."""
    conn = get_connection()
    conditions = ["1=1"]
    params = []

    if search:
        s = f"%{search.lower()}%"
        conditions.append("(LOWER(c.name) LIKE ? OR LOWER(c.domain) LIKE ? OR LOWER(c.email) LIKE ? OR LOWER(c.city) LIKE ?)")
        params.extend([s, s, s, s])

    if filter_type == "with_email":
        conditions.append("c.email != '' AND c.email IS NOT NULL")
    elif filter_type == "with_domain":
        conditions.append("c.domain != '' AND c.domain IS NOT NULL")

    where_clause = " AND ".join(conditions)

    count_sql = f"SELECT COUNT(*) FROM cvent_companies c WHERE {where_clause}"
    count_row = conn.execute(count_sql, params).fetchone()
    total_count = _get_scalar(count_row, 0)

    data_sql = f"""
        SELECT c.*, COUNT(cv.id) as event_count
        FROM cvent_companies c
        LEFT JOIN cvent cv ON cv.company_id = c.id
        WHERE {where_clause}
        GROUP BY c.id
        ORDER BY event_count DESC, c.name ASC
        LIMIT ? OFFSET ?
    """
    data_params = list(params) + [limit, offset]
    rows = conn.execute(data_sql, data_params).fetchall()
    conn.close()

    return {
        "companies": [dict(r) for r in rows],
        "total": total_count,
        "limit": limit,
        "offset": offset
    }


def get_cvent_company_stats():
    """Return overview stats for cvent_companies in a single fast query."""
    conn = get_connection()
    row = conn.execute("""
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN email != '' AND email IS NOT NULL THEN 1 END) as with_email,
            COUNT(CASE WHEN domain != '' AND domain IS NOT NULL THEN 1 END) as with_domain
        FROM cvent_companies
    """).fetchone()
    conn.close()
    if not row:
        return {"total": 0, "with_email": 0, "with_domain": 0}
    return {
        "total": row["total"] or 0,
        "with_email": row["with_email"] or 0,
        "with_domain": row["with_domain"] or 0
    }


def get_cvent_company_by_id(company_id: int):
    """Fetch company profile along with all associated Cvent events."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM cvent_companies WHERE id = ?", (company_id,)).fetchone()
    if not row:
        conn.close()
        return None
    comp_dict = dict(row)
    events = conn.execute("SELECT id, event_name, start_date, city, country, cvent_url, creator_email FROM cvent WHERE company_id = ?", (company_id,)).fetchall()
    comp_dict["events"] = [dict(e) for e in events]
    conn.close()
    return comp_dict


def update_cvent_company(company_id: int, updates: dict):
    """Update company details."""
    conn = get_connection()
    allowed = ("name", "domain", "website", "email", "city", "state", "country")
    clauses = []
    vals = []
    for k, v in updates.items():
        if k in allowed:
            clauses.append(f"{k} = ?")
            vals.append(v)
    if clauses:
        clauses.append("updated_at = CURRENT_TIMESTAMP")
        vals.append(company_id)
        conn.execute(f"UPDATE cvent_companies SET {', '.join(clauses)} WHERE id = ?", vals)
        conn.commit()
    conn.close()
    return True


def delete_cvent_company(company_id: int):
    """Delete company and unlink from events."""
    conn = get_connection()
    conn.execute("UPDATE cvent SET company_id = NULL WHERE company_id = ?", (company_id,))
    conn.execute("DELETE FROM cvent_companies WHERE id = ?", (company_id,))
    conn.commit()
    conn.close()
    return True


# ==============================================================================
# CSV EXPORT UTILITIES
# ==============================================================================

def export_cvent_events_csv(event_ids: list = None) -> str:
    """Export Cvent events to CSV with exact fields requested."""
    conn = get_connection()
    sql = """
        SELECT c.id, c.event_name, c.event_url, c.cvent_url, c.start_date, c.end_date,
               c.venue_name, c.city, c.state, c.country,
               COALESCE(comp.name, c.organizer_company) as organizer_name,
               COALESCE(comp.website, comp.domain, '') as organizer_website,
               c.creator_email, c.status, c.scrape_date,
               COALESCE(p_counts.people_count, 0) as people_count
        FROM cvent c
        LEFT JOIN cvent_companies comp ON c.company_id = comp.id
        LEFT JOIN (
            SELECT event_id, COUNT(*) as people_count
            FROM cvent_people
            GROUP BY event_id
        ) p_counts ON c.id = p_counts.event_id
    """
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        rows = conn.execute(f"{sql} WHERE c.id IN ({placeholders}) ORDER BY c.id ASC", tuple(event_ids)).fetchall()
    else:
        rows = conn.execute(f"{sql} ORDER BY c.id ASC").fetchall()
    conn.close()

    csv_fields = [
        "id", "scraper_source", "event_name", "event_url", "cvent_url", "event_date",
        "location", "organizer_name", "organizer_website", "no_of_emails_scrapped",
        "creator_email", "status", "scrape_date"
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(csv_fields)
    for r in rows:
        row_dict = dict(r)
        raw_email = row_dict.get("creator_email", "") or ""
        p_count = row_dict.get("people_count", 0)
        loc_parts = [row_dict.get("venue_name"), row_dict.get("city"), row_dict.get("state"), row_dict.get("country")]
        location = ", ".join([p for p in loc_parts if p])
        date_str = f"{row_dict.get('start_date', '')} {('-> ' + row_dict.get('end_date')) if row_dict.get('end_date') else ''}".strip()
        writer.writerow([
            row_dict.get("id"),
            "Cvent",
            row_dict.get("event_name"),
            row_dict.get("event_url"),
            row_dict.get("cvent_url"),
            date_str,
            location,
            row_dict.get("organizer_name"),
            row_dict.get("organizer_website"),
            p_count,
            raw_email,
            row_dict.get("status"),
            row_dict.get("scrape_date")
        ])
    return output.getvalue()


def export_cvent_companies_csv(company_ids: list = None) -> str:
    """Export Cvent companies to CSV."""
    conn = get_connection()
    if company_ids:
        placeholders = ",".join("?" for _ in company_ids)
        rows = conn.execute(f"SELECT * FROM cvent_companies WHERE id IN ({placeholders}) ORDER BY name ASC", tuple(company_ids)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM cvent_companies ORDER BY name ASC").fetchall()
    conn.close()

    fields = ["id", "name", "domain", "website", "email", "city", "state", "country", "created_at"]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(fields)
    for r in rows:
        d = dict(r)
        writer.writerow([d.get(f, "") for f in fields])
    return output.getvalue()


# ==============================================================================
# CVENT PEOPLE PIPELINE & QUERIES
# ==============================================================================

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


def _clean_email_str(raw):
    if not raw:
        return ""
    e = str(raw).strip().lower()
    e = re.sub(r'^(?:u003[ce]|\\u003[ce]|&[a-z]+;|[<>"\'])+', '', e)
    e = re.sub(r'(?:u003[ce]|\\u003[ce]|&[a-z]+;|[<>"\'])+$', '', e)
    e = e.strip()
    if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', e):
        if not any(e.endswith(bad) for bad in ['.png', '.jpg', '.jpeg', '.svg', '.gif']):
            return e
    return ""


def _is_generic_email_str(email):
    if not email or '@' not in email:
        return True
    user = email.split('@')[0].lower()
    clean_user = re.sub(r'[0-9._\-]+$', '', user)
    if clean_user in GENERIC_PREFIXES:
        return True
    if any(user.startswith(p) for p in ['info', 'support', 'contact', 'events', 'admin', 'registration']):
        return True
    return False


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


def _extract_name_from_email_str(email):
    if not email or '@' not in email:
        return "-"
    user = email.split('@')[0].lower()
    clean_user = re.sub(r'[0-9]+', '', user)
    
    # Check for department/functional words
    if any(dw in clean_user for dw in DEPARTMENT_WORDS):
        return "-"

    # 1. dot, underscore, or dash separated
    parts = re.split(r'[._\-]+', user)
    clean_parts = [re.sub(r'[0-9]+', '', p) for p in parts if len(p) >= 2]
    clean_parts = [p for p in clean_parts if p.isalpha() and len(p) >= 2]
    if len(clean_parts) >= 2:
        return f"{clean_parts[0].capitalize()} {clean_parts[1].capitalize()}"

    # 2. Known single first names (e.g. brian -> Brian, hannah -> Hannah)
    if clean_user in COMMON_FIRST_NAMES:
        return clean_user.capitalize()

    # 3. single token: initial + surname (e.g. sgerzina -> S. Gerzina)
    if len(clean_user) >= 5 and clean_user.isalpha():
        initial = clean_user[0]
        remainder = clean_user[1:]
        if remainder in COMMON_FIRST_NAMES or remainder in {'ian', 'annah', 'on', 'in'}:
            return "-"
        return f"{initial.upper()}. {remainder.capitalize()}"

    return "-"


def _match_name_for_email_str(email, raw_contact_name):
    if not email:
        return "-"
    if _is_generic_email_str(email):
        return "-"

    user = email.split('@')[0].lower()
    user_tokens = set(re.split(r'[0-9._\-]+', user))

    if raw_contact_name and raw_contact_name.strip() and raw_contact_name != "-":
        pn_clean = raw_contact_name.strip()
        pn_lower = pn_clean.lower()
        if not any(d in pn_lower for d in DISCARD_NAME_TOKENS):
            name_parts = [p for p in re.split(r'\s+', pn_lower) if len(p) >= 2]
            if any(p in user for p in name_parts) or any(t in pn_lower for t in user_tokens if len(t) >= 3):
                return pn_clean

    derived = _extract_name_from_email_str(email)
    if derived != "-":
        return derived

    return "-"


def sync_event_to_people(event_id, conn=None):
    """
    Given an event_id from the 'cvent' table, relationally derives and upserts
    the primary creator (is_creator_email = True) and any secondary team contacts.
    Guarantees the People table is always kept in sync with the Events table.
    """
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        r = conn.execute("""
            SELECT id, cvent_url, event_url, event_name, organizer_company,
                   creator_email, contact_name, contact_phone, general_emails, contact_type
            FROM cvent WHERE id = ?
        """, (event_id,)).fetchone()
        if not r:
            return

        organizer = (r["organizer_company"] or r["event_name"] or "").strip()
        source_url = (r["cvent_url"] or r["event_url"] or "").strip()
        contact_type = (r["contact_type"] or "In-House").strip()
        raw_contact_name = (r["contact_name"] or "").strip()
        raw_phone = (r["contact_phone"] or "-").strip()
        if not raw_phone:
            raw_phone = "-"

        # Parse Creator Emails
        raw_creator_str = r["creator_email"] or ""
        creator_candidates = [_clean_email_str(e) for e in re.split(r"[,;\s]+", raw_creator_str)]
        creator_emails = [e for e in creator_candidates if e]

        # Parse General Emails
        raw_general_str = r["general_emails"] or ""
        general_candidates = [_clean_email_str(e) for e in re.split(r"[,;\s]+", raw_general_str)]
        general_emails = [e for e in general_candidates if e]

        seen_emails_for_event = set()

        # 1. Process Creator Emails
        for em in creator_emails:
            if em in seen_emails_for_event:
                continue
            seen_emails_for_event.add(em)

            generic = _is_generic_email_str(em)
            is_creator = not generic
            person_name = _match_name_for_email_str(em, raw_contact_name)

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

        # 2. Process General Team Emails
        for em in general_emails:
            if em in seen_emails_for_event:
                continue
            seen_emails_for_event.add(em)

            person_name = _match_name_for_email_str(em, raw_contact_name)

            conn.execute("""
                INSERT INTO cvent_people (
                    event_id, organizer_name, contact_name, designation,
                    is_creator_email, phone, linkedin_url, email,
                    source_url, scraper_source, contact_type
                ) VALUES (?, ?, ?, '-', FALSE, '-', '-', ?, ?, 'Cvent', ?)
                ON CONFLICT (event_id, email) DO NOTHING
            """, (event_id, organizer, person_name, em, source_url, contact_type))

    finally:
        if should_close:
            conn.commit()
            conn.close()


def get_all_cvent_people(limit=50, offset=0, search="", is_creator_filter="all", contact_type_filter="all", event_id=None):
    """
    Fetch paginated, filtered Cvent people from the 'cvent_people' table.
    """
    conn = get_connection()
    conditions = ["1=1"]
    params = []

    if event_id:
        conditions.append("p.event_id = ?")
        params.append(event_id)

    if search:
        s = f"%{search.lower()}%"
        conditions.append("""(
            LOWER(p.contact_name) LIKE ? OR
            LOWER(p.organizer_name) LIKE ? OR
            LOWER(p.email) LIKE ? OR
            LOWER(p.phone) LIKE ?
        )""")
        params.extend([s, s, s, s])

    if is_creator_filter == "creator_only":
        conditions.append("p.is_creator_email = TRUE")
    elif is_creator_filter == "secondary_only":
        conditions.append("p.is_creator_email = FALSE")

    if contact_type_filter == "in_house":
        conditions.append("p.contact_type = 'In-House'")
    elif contact_type_filter == "agency":
        conditions.append("p.contact_type = '3rd-Party Agency'")

    where_clause = " AND ".join(conditions)

    count_sql = f"SELECT COUNT(*) FROM cvent_people p WHERE {where_clause}"
    count_row = conn.execute(count_sql, params).fetchone()
    total_count = _get_scalar(count_row, 0)

    data_sql = f"""
        SELECT p.*, c.event_name
        FROM cvent_people p
        LEFT JOIN cvent c ON p.event_id = c.id
        WHERE {where_clause}
        ORDER BY p.is_creator_email DESC, p.id ASC
        LIMIT ? OFFSET ?
    """
    data_params = list(params) + [limit, offset]
    rows = conn.execute(data_sql, data_params).fetchall()
    conn.close()

    return {
        "people": [dict(r) for r in rows],
        "total": total_count,
        "limit": limit,
        "offset": offset
    }


def export_cvent_people_csv(is_creator_filter="all", contact_type_filter="all", search="", event_id=None) -> str:
    """Export people directly matching the 9 manager-requested columns."""
    res = get_all_cvent_people(
        limit=50000, offset=0, search=search,
        is_creator_filter=is_creator_filter,
        contact_type_filter=contact_type_filter,
        event_id=event_id
    )
    people = res["people"]

    csv_fields = [
        "Organizer Name", "Contact Name", "Designation",
        "Is email creator email?", "Phone", "LinkedIn URL",
        "Email", "Source URL", "Scraper source", "Contact Type", "Event Name"
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(csv_fields)
    for p in people:
        is_creator_str = "YES" if p.get("is_creator_email") else "NO"
        writer.writerow([
            p.get("organizer_name", "-"),
            p.get("contact_name", "-"),
            p.get("designation", "-"),
            is_creator_str,
            p.get("phone", "-"),
            p.get("linkedin_url", "-"),
            p.get("email", ""),
            p.get("source_url", ""),
            p.get("scraper_source", "Cvent"),
            p.get("contact_type", "In-House"),
            p.get("event_name", "")
        ])
    return output.getvalue()
