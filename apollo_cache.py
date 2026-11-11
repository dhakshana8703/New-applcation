"""
Persistent Apollo Caching & Checkpoint Engine.
Guarantees zero duplicate API calls, zero credit re-spending, and seamless resumption.

Stores in SQLite (cvent_cache.db):
  1. apollo_org_cache: Caches /organizations/enrich by domain.
  2. apollo_people_cache: Caches /people/bulk_match and /people/match by email or apollo_id.
  3. apollo_event_checkpoints: Tracks which event_ids have already been enriched.
"""

import os
import json
import sqlite3
from datetime import datetime
from typing import Dict, Any, Optional, List

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cvent_cache.db")


def _get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_apollo_cache_tables():
    """Ensure persistent cache and checkpoint tables exist."""
    conn = _get_sqlite_conn()
    with conn:
        # 1. Organization cache (saves 1 credit per company permanently)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS apollo_org_cache (
                domain TEXT PRIMARY KEY,
                org_id TEXT DEFAULT '',
                name TEXT DEFAULT '',
                raw_json TEXT NOT NULL,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. People cache (saves 1 credit per contact match permanently)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS apollo_people_cache (
                lookup_key TEXT PRIMARY KEY,
                apollo_id TEXT DEFAULT '',
                email TEXT DEFAULT '',
                name TEXT DEFAULT '',
                title TEXT DEFAULT '',
                linkedin_url TEXT DEFAULT '',
                raw_json TEXT NOT NULL,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_apollo_people_email ON apollo_people_cache(email)")

        # 3. Event enrichment checkpoint tracker
        conn.execute("""
            CREATE TABLE IF NOT EXISTS apollo_event_checkpoints (
                event_id INTEGER PRIMARY KEY,
                domain TEXT DEFAULT '',
                status TEXT DEFAULT 'completed',
                p1_name TEXT DEFAULT '-',
                p2_name TEXT DEFAULT '-',
                credits_spent INTEGER DEFAULT 0,
                enriched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 4. Company enrichment checkpoint tracker (for Company-Wise Lead Enrichment)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS apollo_company_checkpoints (
                company_id INTEGER,
                domain TEXT PRIMARY KEY,
                company_name TEXT DEFAULT '',
                status TEXT DEFAULT 'completed',
                p1_name TEXT DEFAULT '-',
                p2_name TEXT DEFAULT '-',
                events_linked INTEGER DEFAULT 0,
                credits_spent INTEGER DEFAULT 0,
                enriched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_apollo_comp_domain ON apollo_company_checkpoints(domain)")

        # 5. Free Persona Candidates Directory (0-credit candidate pool for On-Demand 1-Click Enrichment)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS apollo_candidates (
                apollo_id TEXT PRIMARY KEY,
                company_id INTEGER DEFAULT 0,
                company_name TEXT DEFAULT '',
                domain TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                name TEXT DEFAULT '',
                title TEXT DEFAULT '',
                persona TEXT DEFAULT '-',
                is_unlocked BOOLEAN DEFAULT 0,
                email TEXT DEFAULT '',
                linkedin_url TEXT DEFAULT '',
                events_count INTEGER DEFAULT 0,
                discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                unlocked_at TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_apollo_cand_domain ON apollo_candidates(domain)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_apollo_cand_persona ON apollo_candidates(persona)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_apollo_cand_unlocked ON apollo_candidates(is_unlocked)")
    conn.close()


# Initialize tables on import
init_apollo_cache_tables()


# ==============================================================================
# ORGANIZATION CACHING
# ==============================================================================

def get_cached_org(domain: str) -> Optional[Dict[str, Any]]:
    """Retrieve organization from SQLite cache if present."""
    if not domain:
        return None
    conn = _get_sqlite_conn()
    cur = conn.cursor()
    cur.execute("SELECT raw_json FROM apollo_org_cache WHERE domain = ?", (domain.lower().strip(),))
    row = cur.fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["raw_json"])
        except Exception:
            return None
    return None


def set_cached_org(domain: str, org_data: Dict[str, Any]):
    """Persist organization to SQLite cache."""
    if not domain or not org_data:
        return
    clean_dom = domain.lower().strip()
    org_id = org_data.get("id", "")
    name = org_data.get("name", "")
    raw_json = json.dumps(org_data)

    conn = _get_sqlite_conn()
    with conn:
        conn.execute("""
            INSERT INTO apollo_org_cache (domain, org_id, name, raw_json, cached_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(domain) DO UPDATE SET
                org_id = excluded.org_id,
                name = excluded.name,
                raw_json = excluded.raw_json,
                cached_at = CURRENT_TIMESTAMP
        """, (clean_dom, org_id, name, raw_json))
    conn.close()


# ==============================================================================
# PEOPLE CACHING
# ==============================================================================

def get_cached_person(lookup_key: str) -> Optional[Dict[str, Any]]:
    """Retrieve person from cache by email or Apollo ID."""
    if not lookup_key:
        return None
    conn = _get_sqlite_conn()
    cur = conn.cursor()
    cur.execute("SELECT raw_json FROM apollo_people_cache WHERE lookup_key = ?", (lookup_key.lower().strip(),))
    row = cur.fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["raw_json"])
        except Exception:
            return None
    return None


def set_cached_person(lookup_key: str, person_data: Dict[str, Any]):
    """Persist person match to SQLite cache."""
    if not lookup_key or not person_data:
        return
    clean_key = lookup_key.lower().strip()
    apollo_id = person_data.get("id", "")
    email = person_data.get("email", "")
    name = person_data.get("name") or f"{person_data.get('first_name', '')} {person_data.get('last_name', '')}".strip()
    title = person_data.get("title", "")
    linkedin = person_data.get("linkedin_url", "")
    raw_json = json.dumps(person_data)

    conn = _get_sqlite_conn()
    with conn:
        conn.execute("""
            INSERT INTO apollo_people_cache (lookup_key, apollo_id, email, name, title, linkedin_url, raw_json, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(lookup_key) DO UPDATE SET
                apollo_id = excluded.apollo_id,
                email = excluded.email,
                name = excluded.name,
                title = excluded.title,
                linkedin_url = excluded.linkedin_url,
                raw_json = excluded.raw_json,
                cached_at = CURRENT_TIMESTAMP
        """, (clean_key, apollo_id, email, name, title, linkedin, raw_json))
    conn.close()


# ==============================================================================
# CHECKPOINT & RESUMPTION TRACKING
# ==============================================================================

def get_enriched_event_ids() -> List[int]:
    """Return all event IDs that have already been enriched."""
    conn = _get_sqlite_conn()
    cur = conn.cursor()
    cur.execute("SELECT event_id FROM apollo_event_checkpoints")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def record_event_checkpoint(event_id: int, domain: str, p1_name: str = "-", p2_name: str = "-", credits_spent: int = 0):
    """Mark an event as enriched in the persistent checkpoint table."""
    conn = _get_sqlite_conn()
    with conn:
        conn.execute("""
            INSERT INTO apollo_event_checkpoints (event_id, domain, status, p1_name, p2_name, credits_spent, enriched_at)
            VALUES (?, ?, 'completed', ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(event_id) DO UPDATE SET
                p1_name = excluded.p1_name,
                p2_name = excluded.p2_name,
                credits_spent = excluded.credits_spent,
                enriched_at = CURRENT_TIMESTAMP
        """, (event_id, domain, p1_name, p2_name, credits_spent))
    conn.close()


def get_enriched_company_domains() -> List[str]:
    """Return list of company domains that have already been enriched."""
    conn = _get_sqlite_conn()
    cur = conn.cursor()
    cur.execute("SELECT domain FROM apollo_company_checkpoints")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]


def record_company_checkpoint(
    company_id: int,
    domain: str,
    company_name: str = "",
    p1_name: str = "-",
    p2_name: str = "-",
    events_linked: int = 0,
    credits_spent: int = 0
):
    """Mark a company domain as enriched in persistent checkpoints."""
    conn = _get_sqlite_conn()
    with conn:
        conn.execute("""
            INSERT INTO apollo_company_checkpoints (
                company_id, domain, company_name, status, p1_name, p2_name, events_linked, credits_spent, enriched_at
            )
            VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(domain) DO UPDATE SET
                company_id = excluded.company_id,
                company_name = excluded.company_name,
                p1_name = excluded.p1_name,
                p2_name = excluded.p2_name,
                events_linked = excluded.events_linked,
                credits_spent = excluded.credits_spent,
                enriched_at = CURRENT_TIMESTAMP
        """, (company_id, domain.lower().strip(), company_name, p1_name, p2_name, events_linked, credits_spent))
    conn.close()


def get_checkpoint_stats() -> Dict[str, Any]:
    """Get high-level cache and checkpoint metrics."""
    conn = _get_sqlite_conn()
    cur = conn.cursor()
    org_count = cur.execute("SELECT COUNT(*) FROM apollo_org_cache").fetchone()[0]
    people_count = cur.execute("SELECT COUNT(*) FROM apollo_people_cache").fetchone()[0]
    event_count = cur.execute("SELECT COUNT(*) FROM apollo_event_checkpoints").fetchone()[0]
    company_count = cur.execute("SELECT COUNT(*) FROM apollo_company_checkpoints").fetchone()[0]
    cand_count = cur.execute("SELECT COUNT(*) FROM apollo_candidates").fetchone()[0]
    unlocked_cand = cur.execute("SELECT COUNT(*) FROM apollo_candidates WHERE is_unlocked = 1").fetchone()[0]
    total_credits = cur.execute("SELECT SUM(credits_spent) FROM apollo_company_checkpoints").fetchone()[0] or 0
    conn.close()
    return {
        "cached_organizations": org_count,
        "cached_people": people_count,
        "enriched_events": event_count,
        "enriched_companies": company_count,
        "discovered_candidates": cand_count,
        "unlocked_candidates": unlocked_cand,
        "total_checkpoint_credits": total_credits
    }


# ==============================================================================
# CANDIDATES DIRECTORY (0-CREDIT DISCOVERY & ON-DEMAND UNLOCKS)
# ==============================================================================

def upsert_candidate(cand: Dict[str, Any]):
    """Insert or update a discovered P1/P2 candidate."""
    apollo_id = cand.get("apollo_id") or cand.get("id")
    if not apollo_id:
        return

    conn = _get_sqlite_conn()
    with conn:
        conn.execute("""
            INSERT INTO apollo_candidates (
                apollo_id, company_id, company_name, domain, first_name, last_name,
                name, title, persona, is_unlocked, email, linkedin_url, events_count, discovered_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(apollo_id) DO UPDATE SET
                company_id = CASE WHEN excluded.company_id > 0 THEN excluded.company_id ELSE apollo_candidates.company_id END,
                company_name = CASE WHEN excluded.company_name != '' THEN excluded.company_name ELSE apollo_candidates.company_name END,
                domain = CASE WHEN excluded.domain != '' THEN excluded.domain ELSE apollo_candidates.domain END,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE apollo_candidates.title END,
                persona = excluded.persona,
                events_count = CASE WHEN excluded.events_count > 0 THEN excluded.events_count ELSE apollo_candidates.events_count END,
                is_unlocked = CASE WHEN excluded.is_unlocked = 1 THEN 1 ELSE apollo_candidates.is_unlocked END,
                email = CASE WHEN excluded.email != '' THEN excluded.email ELSE apollo_candidates.email END,
                name = CASE WHEN excluded.name != '' THEN excluded.name ELSE apollo_candidates.name END,
                linkedin_url = CASE WHEN excluded.linkedin_url != '' THEN excluded.linkedin_url ELSE apollo_candidates.linkedin_url END
        """, (
            apollo_id,
            cand.get("company_id", 0),
            cand.get("company_name", ""),
            cand.get("domain", "").lower().strip(),
            cand.get("first_name", ""),
            cand.get("last_name", ""),
            cand.get("name", ""),
            cand.get("title", ""),
            cand.get("persona", "-"),
            1 if cand.get("is_unlocked") else 0,
            cand.get("email", ""),
            cand.get("linkedin_url", ""),
            cand.get("events_count", 0)
        ))
    conn.close()


def mark_candidate_unlocked(apollo_id: str, full_name: str, email: str, linkedin_url: str):
    """Mark a candidate as unlocked with their verified email and LinkedIn URL."""
    if not apollo_id:
        return
    conn = _get_sqlite_conn()
    with conn:
        conn.execute("""
            UPDATE apollo_candidates
            SET is_unlocked = 1,
                name = COALESCE(NULLIF(?, ''), name),
                email = COALESCE(NULLIF(?, ''), email),
                linkedin_url = COALESCE(NULLIF(?, ''), linkedin_url),
                unlocked_at = CURRENT_TIMESTAMP
            WHERE apollo_id = ?
        """, (full_name, email, linkedin_url, apollo_id))
    conn.close()


def get_all_candidates_list(
    persona: Optional[str] = None,
    is_unlocked: Optional[bool] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> Dict[str, Any]:
    """Retrieve candidates with filtering and pagination."""
    conn = _get_sqlite_conn()
    cur = conn.cursor()

    conditions = ["1=1"]
    params = []

    if persona and persona != "all":
        conditions.append("persona = ?")
        params.append(persona.upper())

    if is_unlocked is not None:
        conditions.append("is_unlocked = ?")
        params.append(1 if is_unlocked else 0)

    if search:
        s = f"%{search.lower().strip()}%"
        conditions.append("(LOWER(company_name) LIKE ? OR LOWER(domain) LIKE ? OR LOWER(title) LIKE ? OR LOWER(first_name) LIKE ? OR LOWER(name) LIKE ? OR LOWER(email) LIKE ?)")
        params.extend([s, s, s, s, s, s])

    where_clause = " AND ".join(conditions)

    count_sql = f"SELECT COUNT(*) FROM apollo_candidates WHERE {where_clause}"
    total = cur.execute(count_sql, params).fetchone()[0]

    select_sql = f"""
        SELECT apollo_id, company_id, company_name, domain, first_name, last_name, name,
               title, persona, is_unlocked, email, linkedin_url, events_count, discovered_at, unlocked_at
        FROM apollo_candidates
        WHERE {where_clause}
        ORDER BY is_unlocked DESC, events_count DESC, persona ASC, apollo_id ASC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    rows = cur.execute(select_sql, params).fetchall()
    conn.close()

    candidates = [dict(r) for r in rows]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "candidates": candidates
    }


def get_company_candidate_stats() -> Dict[str, Dict[str, Any]]:
    """Get P1 count, P2 count, unlocked count, remaining count, and last enriched date for each company domain."""
    conn = _get_sqlite_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT domain,
               SUM(CASE WHEN persona = 'P1' THEN 1 ELSE 0 END) as p1_count,
               SUM(CASE WHEN persona = 'P2' THEN 1 ELSE 0 END) as p2_count,
               SUM(CASE WHEN is_unlocked = 1 THEN 1 ELSE 0 END) as unlocked_count,
               SUM(CASE WHEN is_unlocked = 0 THEN 1 ELSE 0 END) as locked_count,
               COUNT(*) as total_count,
               MAX(discovered_at) as last_enriched
        FROM apollo_candidates
        WHERE domain != '' AND domain IS NOT NULL
        GROUP BY domain
    """)
    rows = cur.fetchall()
    conn.close()
    return {
        (r[0] or "").lower().strip(): {
            "p1_count": r[1] or 0,
            "p2_count": r[2] or 0,
            "unlocked_count": r[3] or 0,
            "locked_count": r[4] or 0,
            "total_count": r[5] or 0,
            "last_enriched": r[6] or ""
        }
        for r in rows
    }


def get_candidate_domains_summary() -> List[Dict[str, Any]]:
    """Return domains list with company name and count for dropdown."""
    conn = _get_sqlite_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT domain, company_name, COUNT(*) as cnt,
               SUM(CASE WHEN persona = 'P1' THEN 1 ELSE 0 END) as p1,
               SUM(CASE WHEN persona = 'P2' THEN 1 ELSE 0 END) as p2
        FROM apollo_candidates
        WHERE domain != '' AND domain IS NOT NULL
        GROUP BY domain
        ORDER BY cnt DESC, domain ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"domain": r[0], "name": r[1] or r[0], "count": r[2], "p1": r[3], "p2": r[4]}
        for r in rows
    ]
