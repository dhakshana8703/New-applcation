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
    total_credits = cur.execute("SELECT SUM(credits_spent) FROM apollo_event_checkpoints").fetchone()[0] or 0
    conn.close()
    return {
        "cached_organizations": org_count,
        "cached_people": people_count,
        "enriched_events": event_count,
        "total_checkpoint_credits": total_credits
    }
