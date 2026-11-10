"""
Apollo Lead Enrichment Engine with Persistent Cache, Checkpoint Recovery & Domain Guards.
Strictly implements the Candidate Selection Logic & APIs specified in the Nunify Outbound architecture:

APIs Required:
  1. /people/bulk_match: Identify creator's name, title, company and LinkedIn (1 credit)
  2. /organizations/enrich: Identify company by domain (1 credit)
  3. /mixed_people/api_search: Search P1 and P2 candidates by persona job titles (0 credits)
  4. /people/match: Retrieve available work email for selected candidates (1 credit each)

Target Personas:
  P1 (Decision-Maker): Group Show Director, Group Exhibition Director, Portfolio Director,
                       Group Portfolio Director, VP Exhibitions, Vice President Exhibitions,
                       VP Events, Head of Exhibitions, CEO, Chief Executive Officer, COO,
                       Chief Operating Officer, President, Managing Director, Founder
  P2 (Operational Buyer): Show Director, Exhibition Director, Director of Exhibition Operations,
                          Director of Event Operations, Head of Event Operations, Operations Director,
                          Director of Event Technology, Head of Event Technology, Show Manager,
                          Exhibition Manager

Safety & Senior Engineering Constraints:
  - Header auth: Uses X-Api-Key header as strictly required by Apollo.
  - Public Domain Guard: Blocks lookup of gmail, yahoo, hotmail, etc. with smart custom URL fallback.
  - Persistent SQLite Cache: Caches every organization & contact match locally.
  - Checkpoint Engine: Tracks enriched event IDs; subsequent runs resume from next unenriched event.
  - 0-credit search executed before any paid profile match.
  - Persistent real-time audit files: apollo_credit_usage.json & apollo_credit_usage.csv.
  - Strict deduplication: never spend credits on already enriched contacts.
  - Creator enriched and classified as P1, P2 or Neither (Priority: Primary / Secondary / Not Selected).
  - Max credit cap per batch (default 20 credits for 5 events).
  - Rate limiting / backoff between requests.
"""

import os
import re
import time
import threading
import requests
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional, Tuple

import cvent_db
from db_compat import get_db_connection
from credit_tracker import credit_tracker
import apollo_cache

APOLLO_API_BASE = "https://api.apollo.io/v1"
CANDIDATES_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apollo_p1_p2_candidates.csv")
CANDIDATES_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apollo_p1_p2_candidates.json")

# ==============================================================================
# PUBLIC WEBMAIL PROVIDERS BLOCKLIST (IMPROVEMENT 2)
# ==============================================================================

PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "aol.com", "mail.com", "email.com", "proton.me", "protonmail.com",
    "zoho.com", "live.com", "msn.com", "yandex.com", "googlemail.com",
    "comcast.net", "sbcglobal.net", "att.net", "verizon.net",
    "cvent.com", "cventevents.com"
}


def extract_clean_company_domain(email: str, company_domain: str, event_url: str) -> str:
    """
    Extracts a clean, verified corporate domain while strictly filtering out
    public webmail providers (gmail, yahoo, etc.) with smart custom event URL fallback.
    """
    dom = (company_domain or "").lower().strip()

    # If existing company_domain is a public email provider, discard it
    if dom in PUBLIC_EMAIL_DOMAINS:
        dom = ""

    # If no valid domain yet, try email domain
    if not dom and "@" in email:
        email_dom = email.split("@")[-1].lower().strip()
        if email_dom not in PUBLIC_EMAIL_DOMAINS:
            dom = email_dom

    # Smart Fallback: if domain is STILL empty (e.g. planner used gmail.com),
    # check the event URL custom domain (e.g. events.austinmarathon.com -> austinmarathon.com)
    if not dom and event_url:
        try:
            parsed = urlparse(event_url)
            host = parsed.netloc.lower()
            if host and not host.endswith("cvent.com") and host != "cvent.com":
                parts = host.split(".")
                if len(parts) >= 2:
                    dom = ".".join(parts[-2:])
        except Exception:
            pass

    return dom


# ==============================================================================
# TARGET PERSONAS AND TITLES (FROM MANAGER DOCUMENT)
# ==============================================================================

P1_TITLES = [
    "group show director",
    "group exhibition director",
    "group portfolio director",
    "portfolio director",
    "vp exhibitions",
    "vice president exhibitions",
    "vp events",
    "vice president events",
    "head of exhibitions",
    "chief executive officer",
    "ceo",
    "chief operating officer",
    "coo",
    "president",
    "managing director",
    "founder",
    "co-founder"
]

P2_TITLES = [
    "show director",
    "exhibition director",
    "director of exhibition operations",
    "director of event operations",
    "head of event operations",
    "operations director",
    "director of event technology",
    "head of event technology",
    "show manager",
    "exhibition manager"
]


NOISY_TITLE_SUBSTRINGS = [
    "looking", "seeking", "former", "ex-", "past president", "past ", "formerly",
    "student", "intern", "aspiring", "candidate for", "open to", "transitioning", 
    "between roles", "retired", "emeritus", "alumni", "volunteer", "aide"
]

def is_valid_persona_title(title: str) -> bool:
    """Strict title validation to reject fragments, job seekers, and ex-employees."""
    if not title:
        return False
    t = title.strip().lower()
    if len(t) < 4:
        return False
    if t.endswith(" of") or t.endswith(" for") or t.endswith(" at"):
        return False
    for noisy in NOISY_TITLE_SUBSTRINGS:
        if noisy in t:
            return False
    return True

def classify_title(title: str) -> str:
    """Classify a job title into P1, P2, or Neither with strict quality filters."""
    if not is_valid_persona_title(title):
        return "Neither"
    t = title.lower().strip()
    for p1 in P1_TITLES:
        if re.search(rf"\b{re.escape(p1)}\b", t):
            return "P1"
    for p2 in P2_TITLES:
        if re.search(rf"\b{re.escape(p2)}\b", t):
            return "P2"
    return "Neither"


class ApolloEnricher:
    """
    Production-grade Apollo Client with domain guards, persistent caching,
    checkpoint recovery, strict budget controls and audit logging.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (api_key or os.environ.get("APOLLO_API_KEY", "")).strip()
        self.headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache"
        }
        if self.api_key:
            self.headers["X-Api-Key"] = self.api_key

        self.credits_spent = 0

    def is_configured(self) -> bool:
        return bool(self.api_key and not self.api_key.startswith("mock"))

    # --------------------------------------------------------------------------
    # API 1: Bulk Match Creator (1 credit per match, cached in SQLite)
    # --------------------------------------------------------------------------
    def match_creator(self, email: str, event_id: Any = None, event_name: str = "") -> Optional[Dict[str, Any]]:
        """Identify creator's name, title, company and LinkedIn with persistent cache."""
        clean_email = email.lower().strip()
        if not clean_email or "@" not in clean_email:
            return None

        # 1. Check local persistent SQLite cache first
        cached = apollo_cache.get_cached_person(clean_email)
        if cached:
            print(f"[Apollo Cache] HIT creator: {clean_email} (0 credits)")
            return cached

        if not self.is_configured():
            return None

        url = f"{APOLLO_API_BASE}/people/bulk_match"
        payload = {"details": [{"email": clean_email}]}
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=12)
            if r.status_code == 200:
                data = r.json()
                matches = data.get("matches", [])
                if matches and matches[0]:
                    match_record = matches[0]
                    credits_used = data.get("credits_consumed", 1)
                    self.credits_spent += credits_used
                    credit_tracker.record_usage(
                        event_id=event_id,
                        event_name=event_name,
                        action="match_creator",
                        endpoint="/people/bulk_match",
                        target=clean_email,
                        credits=credits_used
                    )
                    # Persist to SQLite cache permanently
                    apollo_cache.set_cached_person(clean_email, match_record)
                    return match_record
        except Exception as e:
            print(f"[Apollo] match_creator failed for {clean_email}: {e}")
        return None

    # --------------------------------------------------------------------------
    # API 2: Organization Enrich (1 credit per company, cached in SQLite)
    # --------------------------------------------------------------------------
    def enrich_organization(self, domain: str, event_id: Any = None, event_name: str = "") -> Optional[Dict[str, Any]]:
        """Identify company ID and details by domain with public provider guard & cache."""
        clean_dom = (domain or "").lower().strip()
        if not clean_dom or clean_dom in PUBLIC_EMAIL_DOMAINS:
            return None

        # 1. Check persistent SQLite cache first
        cached_org = apollo_cache.get_cached_org(clean_dom)
        if cached_org:
            print(f"[Apollo Cache] HIT org domain: {clean_dom} (0 credits)")
            return cached_org

        if not self.is_configured():
            return None

        url = f"{APOLLO_API_BASE}/organizations/enrich"
        params = {"domain": clean_dom}
        try:
            r = requests.get(url, params=params, headers=self.headers, timeout=12)
            if r.status_code == 200:
                data = r.json().get("organization")
                if data:
                    self.credits_spent += 1
                    credit_tracker.record_usage(
                        event_id=event_id,
                        event_name=event_name,
                        action="enrich_organization",
                        endpoint="/organizations/enrich",
                        target=clean_dom,
                        credits=1
                    )
                    # Persist to SQLite cache permanently
                    apollo_cache.set_cached_org(clean_dom, data)
                    return data
        except Exception as e:
            print(f"[Apollo] enrich_organization failed for {clean_dom}: {e}")
        return None

    # --------------------------------------------------------------------------
    # API 3: Search Candidates by Persona Titles (0 credits)
    # --------------------------------------------------------------------------
    def search_persona_candidates(self, org_id: str, domain: str, event_id: Any = None, event_name: str = "") -> List[Dict[str, Any]]:
        """Find P1 and P2 candidates at the organization with public provider guard."""
        clean_dom = (domain or "").lower().strip()
        if not clean_dom or clean_dom in PUBLIC_EMAIL_DOMAINS:
            return []

        if not self.is_configured():
            return []

        url = f"{APOLLO_API_BASE}/mixed_people/api_search"
        payload = {
            "q_organization_domains": clean_dom,
            "person_titles": P1_TITLES + P2_TITLES,
            "page": 1,
            "per_page": 10
        }
        if org_id:
            payload["organization_ids"] = [org_id]

        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=15)
            if r.status_code == 200:
                people = r.json().get("people", [])
                credit_tracker.record_usage(
                    event_id=event_id,
                    event_name=event_name,
                    action="search_persona_candidates",
                    endpoint="/mixed_people/api_search",
                    target=clean_dom,
                    credits=0
                )
                return people
        except Exception as e:
            print(f"[Apollo] search_persona_candidates failed: {e}")
        return []

    # --------------------------------------------------------------------------
    # API 4: Unlock Verified Work Email (1 credit per match, cached in SQLite)
    # --------------------------------------------------------------------------
    def unlock_work_email(self, person_id: str, event_id: Any = None, event_name: str = "") -> Optional[Dict[str, Any]]:
        """Retrieve available work email with persistent cache."""
        if not person_id:
            return None

        # 1. Check local persistent SQLite cache
        cached_person = apollo_cache.get_cached_person(person_id)
        if cached_person and cached_person.get("email"):
            print(f"[Apollo Cache] HIT person ID: {person_id} (0 credits)")
            return cached_person

        if not self.is_configured():
            return None

        url = f"{APOLLO_API_BASE}/people/match"
        payload = {"id": person_id}
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=12)
            if r.status_code == 200:
                self.credits_spent += 1
                person = r.json().get("person")
                if person:
                    credit_tracker.record_usage(
                        event_id=event_id,
                        event_name=event_name,
                        action="unlock_work_email",
                        endpoint="/people/match",
                        target=person_id,
                        credits=1
                    )
                    # Persist to SQLite cache
                    apollo_cache.set_cached_person(person_id, person)
                    if person.get("email"):
                        apollo_cache.set_cached_person(person.get("email"), person)
                    return person
        except Exception as e:
            print(f"[Apollo] unlock_work_email failed for {person_id}: {e}")
        return None

    # --------------------------------------------------------------------------
    # 0-CREDIT CANDIDATE DISCOVERY & ON-DEMAND ENRICHMENT
    # --------------------------------------------------------------------------
    def export_candidates_files(self) -> Tuple[str, str]:
        """Export all discovered candidates to CSV and JSON."""
        import csv
        import json
        from datetime import datetime

        data = apollo_cache.get_all_candidates_list(limit=2000, offset=0)
        candidates = data.get("candidates", [])

        # 1. Export CSV
        with open(CANDIDATES_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Apollo ID", "Company Name", "Domain", "First Name", "Last Name",
                "Full Name", "Title", "Persona (P1/P2)", "Status (Unlocked/Preview)",
                "Work Email", "LinkedIn URL", "Linked Events Count"
            ])
            for c in candidates:
                status = "Unlocked" if c.get("is_unlocked") else "Preview (0 credits)"
                full_name = c.get("name") or f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
                writer.writerow([
                    c.get("apollo_id", ""),
                    c.get("company_name", ""),
                    c.get("domain", ""),
                    c.get("first_name", ""),
                    c.get("last_name", ""),
                    full_name,
                    c.get("title", ""),
                    c.get("persona", ""),
                    status,
                    c.get("email", ""),
                    c.get("linkedin_url", ""),
                    c.get("events_count", 0)
                ])

        # 2. Export JSON
        p1_cnt = sum(1 for c in candidates if c.get("persona") == "P1")
        p2_cnt = sum(1 for c in candidates if c.get("persona") == "P2")
        unlocked_cnt = sum(1 for c in candidates if c.get("is_unlocked"))

        with open(CANDIDATES_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "total_candidates": len(candidates),
                "p1_count": p1_cnt,
                "p2_count": p2_cnt,
                "unlocked_count": unlocked_cnt,
                "candidates": candidates
            }, f, indent=2)

        return CANDIDATES_CSV_PATH, CANDIDATES_JSON_PATH

    def enrich_single_company(
        self,
        domain: str,
        company_id: Optional[int] = None,
        company_name: str = "",
        event_count: int = 0
    ) -> Dict[str, Any]:
        """
        Enrich a single company domain for 0 CREDITS.
        Fetches P1 & P2 candidates, applies strict quality filters,
        upserts into SQLite candidate pool, and records checkpoint.
        """
        clean_dom = (domain or "").lower().strip()
        if not clean_dom or clean_dom in PUBLIC_EMAIL_DOMAINS:
            return {"error": f"Invalid or public domain: {domain}", "candidates": [], "p1_count": 0, "p2_count": 0}

        raw_candidates = self.search_persona_candidates("", clean_dom, event_id=company_id, event_name=company_name)
        p1_list = []
        p2_list = []
        valid_candidates = []

        for cand in raw_candidates:
            cid = cand.get("id")
            if not cid:
                continue

            title = cand.get("title") or ""
            persona = classify_title(title)
            if persona not in ("P1", "P2"):
                continue

            fname = cand.get("first_name") or ""
            lname = cand.get("last_name") or ""
            name = cand.get("name") or (f"{fname} {lname}".strip() or fname)

            cand_dict = {
                "apollo_id": cid,
                "company_id": company_id or 0,
                "company_name": company_name or clean_dom,
                "domain": clean_dom,
                "first_name": fname,
                "last_name": lname,
                "name": name,
                "title": title,
                "persona": persona,
                "is_unlocked": False,
                "email": cand.get("email") or "",
                "linkedin_url": cand.get("linkedin_url") or "",
                "events_count": event_count
            }

            apollo_cache.upsert_candidate(cand_dict)
            valid_candidates.append(cand_dict)

            if persona == "P1":
                p1_list.append(cand_dict)
            elif persona == "P2":
                p2_list.append(cand_dict)

        # Record company checkpoint
        p1_top = p1_list[0]["name"] if p1_list else "-"
        p2_top = p2_list[0]["name"] if p2_list else "-"
        try:
            apollo_cache.record_company_checkpoint(
                company_id=company_id or 0,
                domain=clean_dom,
                company_name=company_name,
                events_linked=event_count,
                p1_name=p1_top,
                p2_name=p2_top,
                credits_spent=0
            )
        except Exception as e:
            print(f"[Apollo] Failed to record company checkpoint: {e}")

        return {
            "success": True,
            "domain": clean_dom,
            "company_name": company_name or clean_dom,
            "p1_count": len(p1_list),
            "p2_count": len(p2_list),
            "total_candidates": len(valid_candidates),
            "candidates": valid_candidates
        }

    def discover_p1_p2_candidates(
        self,
        max_companies: Optional[int] = None,
        progress_callback = None,
        stop_check = None
    ) -> Dict[str, Any]:
        """
        Discovers ALL matching P1 & P2 candidates across unique companies for 0 CREDITS.
        Populates the local SQLite candidate pool and exports to CSV and JSON.
        """
        conn = None
        for _ in range(5):
            try:
                conn = get_db_connection()
                break
            except Exception:
                time.sleep(1)

        if not conn:
            return {"error": "Could not connect to database"}

        cur = conn.cursor() if hasattr(conn, 'cursor') else conn

        # Query all unique companies with events
        cur.execute("""
            SELECT 
                comp.id as company_id,
                comp.name as company_name,
                comp.domain,
                COUNT(c.id) as event_count
            FROM cvent_companies comp
            JOIN cvent c ON c.company_id = comp.id
            WHERE comp.domain != '' AND comp.domain IS NOT NULL
            GROUP BY comp.id, comp.name, comp.domain
            ORDER BY event_count DESC, comp.id ASC
        """)
        all_companies = cur.fetchall()
        conn.close()

        # Filter out public email domains
        valid_companies = []
        for c in all_companies:
            dom = (c["domain"] if hasattr(c, "keys") else c[2] or "").lower().strip()
            if dom and dom not in PUBLIC_EMAIL_DOMAINS:
                valid_companies.append(c)

        if max_companies:
            valid_companies = valid_companies[:max_companies]

        total_comps = len(valid_companies)
        candidates_found = 0
        p1_found = 0
        p2_found = 0

        for idx, c in enumerate(valid_companies):
            if stop_check and stop_check():
                break

            comp_id = c["company_id"] if hasattr(c, "keys") else c[0]
            comp_name = c["company_name"] if hasattr(c, "keys") else c[1]
            dom = (c["domain"] if hasattr(c, "keys") else c[2] or "").lower().strip()
            ev_cnt = c["event_count"] if hasattr(c, "keys") else c[3]

            if progress_callback:
                progress_callback(
                    processed=idx + 1,
                    total=total_comps,
                    current_company=comp_name,
                    domain=dom,
                    candidates_so_far=candidates_found
                )

            # Free 0-credit candidate search by domain
            cands = self.search_persona_candidates("", dom, event_id=comp_id, event_name=comp_name)
            for cand in cands:
                cand_id = cand.get("id")
                if not cand_id:
                    continue

                title = cand.get("title") or ""
                persona = classify_title(title)
                if persona not in ("P1", "P2"):
                    continue

                if persona == "P1":
                    p1_found += 1
                elif persona == "P2":
                    p2_found += 1

                # Check if already unlocked in local cache
                cached = apollo_cache.get_cached_person(cand_id)
                is_unlocked = bool(cached and cached.get("email"))

                cand_record = {
                    "apollo_id": cand_id,
                    "company_id": comp_id,
                    "company_name": comp_name,
                    "domain": dom,
                    "first_name": cand.get("first_name", ""),
                    "last_name": cand.get("last_name", "") if is_unlocked else "",
                    "name": cached.get("name", "") if cached else "",
                    "title": title,
                    "persona": persona,
                    "is_unlocked": is_unlocked,
                    "email": cached.get("email", "") if cached else "",
                    "linkedin_url": cached.get("linkedin_url", "") if cached else "",
                    "events_count": ev_cnt
                }
                apollo_cache.upsert_candidate(cand_record)
                candidates_found += 1

            time.sleep(0.35)  # Rate limiting safety

        # Generate CSV & JSON exports
        csv_file, json_file = self.export_candidates_files()

        return {
            "status": "completed",
            "companies_scanned": total_comps,
            "total_candidates_found": candidates_found,
            "p1_count": p1_found,
            "p2_count": p2_found,
            "csv_path": csv_file,
            "json_path": json_file
        }

    def unlock_candidate_on_demand(self, apollo_id: str, domain: Optional[str] = None) -> Dict[str, Any]:
        """
        On-demand 1-click unlock for a specific candidate (1 credit).
        Retrieves verified work email & LinkedIn, updates apollo_candidates,
        and links lead across all company events in cvent_people!
        """
        if not apollo_id:
            return {"error": "Missing apollo_id"}

        # 1. Unlock work email via Apollo /people/match (1 credit)
        profile = self.unlock_work_email(apollo_id)
        if not profile:
            return {"error": "Could not unlock profile via Apollo or no email available"}

        fname = (profile.get("first_name") or "").strip()
        lname = (profile.get("last_name") or "").strip()
        if fname.lower() == "none": fname = ""
        if lname.lower() == "none": lname = ""
        raw_name = profile.get("name") or f"{fname} {lname}".strip()
        if not raw_name or raw_name.lower() in ("none none", "none"):
            cand_info = apollo_cache.get_all_candidates_list(search=apollo_id, limit=1)
            cands = cand_info.get("candidates", [])
            name = (cands[0].get("name") if cands else "") or fname or "Executive"
        else:
            name = raw_name
        email = profile.get("email") or ""
        linkedin = profile.get("linkedin_url") or "-"
        title = profile.get("title") or "-"
        persona = classify_title(title)
        priority = "Primary" if persona == "P1" else ("Secondary" if persona == "P2" else "Other")

        # 2. Update local SQLite candidate cache
        apollo_cache.mark_candidate_unlocked(apollo_id, full_name=name, email=email, linkedin_url=linkedin)

        # 3. Propagate to ALL linked events in Neon PostgreSQL cvent_people
        conn = None
        for _ in range(5):
            try:
                conn = get_db_connection()
                break
            except Exception:
                time.sleep(1)

        events_linked = 0
        if conn:
            cur = conn.cursor() if hasattr(conn, 'cursor') else conn
            dom = domain or ""
            if not dom and email and "@" in email:
                dom = email.split("@")[-1].lower().strip()

            if dom:
                cur.execute("""
                    SELECT id, organizer_company, event_url
                    FROM cvent
                    WHERE lower(split_part(creator_email, '@', 2)) = %s
                       OR company_id IN (SELECT id FROM cvent_companies WHERE lower(domain) = %s)
                """, (dom, dom))
                events = cur.fetchall()

                for ev in events:
                    eid = ev["id"] if hasattr(ev, "keys") else ev[0]
                    org = (ev["organizer_company"] if hasattr(ev, "keys") else ev[1]) or dom
                    eurl = (ev["event_url"] if hasattr(ev, "keys") else ev[2]) or ""
                    self._upsert_apollo_person(
                        conn, eid, org, profile, persona=persona, priority=priority, source_url=eurl, is_creator=False
                    )
                    events_linked += 1

            conn.commit()
            conn.close()

        # Update candidate sheet & json
        try:
            self.export_candidates_files()
        except Exception:
            pass

        return {
            "status": "unlocked",
            "apollo_id": apollo_id,
            "name": name,
            "title": title,
            "email": email,
            "linkedin_url": linkedin,
            "persona": persona,
            "priority": priority,
            "events_linked": events_linked
        }

    # --------------------------------------------------------------------------
    # COMPANY-WISE LEAD ENRICHMENT ENGINE (IMPROVEMENT 3)
    # --------------------------------------------------------------------------
    def enrich_company_batch(
        self,
        batch_size: int = 5,
        credit_cap: int = 20,
        progress_callback = None,
        stop_check = None
    ) -> Dict[str, Any]:
        """
        Runs Apollo enrichment COMPANY-WISE rather than event-by-event.
        Enriches each unique corporate domain ONCE and links the verified P1/P2
        decision-makers across ALL events belonging to that company.
        Completely eliminates duplicate credit burn across multi-event organizers.
        """
        conn = None
        for _ in range(5):
            try:
                conn = get_db_connection()
                break
            except Exception:
                time.sleep(1)

        if not conn:
            return {"error": "Could not connect to database", "companies_processed": 0, "total_credits_spent": 0}

        cur = conn.cursor() if hasattr(conn, 'cursor') else conn

        # 1. Retrieve all already-enriched company domains from SQLite checkpoints
        enriched_domains = set(apollo_cache.get_enriched_company_domains())
        blocked_domains = PUBLIC_EMAIL_DOMAINS | enriched_domains

        # 2. Query unique companies from Neon DB with their associated event counts
        # Prioritize companies with the highest number of events for maximum outbound impact!
        cur.execute("""
            SELECT 
                comp.id as company_id,
                comp.name as company_name,
                comp.domain,
                COUNT(c.id) as event_count
            FROM cvent_companies comp
            JOIN cvent c ON c.company_id = comp.id
            WHERE comp.domain != '' AND comp.domain IS NOT NULL
            GROUP BY comp.id, comp.name, comp.domain
            ORDER BY event_count DESC, comp.id ASC
        """)
        all_companies = cur.fetchall()

        companies_to_process = []
        for c in all_companies:
            dom = (c["domain"] if hasattr(c, "keys") else c[2] or "").lower().strip()
            if dom and dom not in blocked_domains:
                companies_to_process.append(c)
                if len(companies_to_process) >= batch_size:
                    break

        total_companies = len(companies_to_process)
        results = []
        companies_processed = 0
        total_events_updated = 0

        for c in companies_to_process:
            if stop_check and stop_check():
                print("[Apollo] Stop requested during company batch. Stopping.")
                break
            if self.credits_spent >= credit_cap:
                print(f"[Apollo] Credit cap ({credit_cap}) reached. Stopping.")
                break

            comp_id = c["company_id"] if hasattr(c, "keys") else c[0]
            comp_name = c["company_name"] if hasattr(c, "keys") else c[1]
            domain = (c["domain"] if hasattr(c, "keys") else c[2] or "").lower().strip()
            event_count = c["event_count"] if hasattr(c, "keys") else c[3]

            if progress_callback:
                progress_callback(
                    processed=companies_processed,
                    total=total_companies,
                    current_event=f"{comp_name} ({event_count} events)",
                    current_domain=domain,
                    credits_batch=self.credits_spent,
                    events_linked_so_far=total_events_updated
                )

            credits_before = self.credits_spent

            # Query all linked events for this company
            cur.execute("""
                SELECT id, event_name, event_url, organizer_company, creator_email
                FROM cvent
                WHERE company_id = %s OR lower(split_part(creator_email, '@', 2)) = %s
                ORDER BY id ASC
            """, (comp_id, domain))
            linked_events = cur.fetchall()

            # 1. Free 0-credit persona candidate search directly by domain (Bypasses /organizations/enrich)
            candidates = self.search_persona_candidates("", domain, event_id=comp_id, event_name=comp_name) if domain else []

            p1_candidate = None
            p2_candidate = None

            for cand in candidates:
                c_title = cand.get("title", "")
                persona = classify_title(c_title)
                if persona == "P1" and not p1_candidate:
                    p1_candidate = cand
                elif persona == "P2" and not p2_candidate:
                    p2_candidate = cand

            # 2. Unlock work email for P1 Decision-Maker (1 credit)
            if p1_candidate and not p1_candidate.get("email"):
                unlocked = self.unlock_work_email(p1_candidate.get("id"), event_id=comp_id, event_name=comp_name)
                if unlocked:
                    p1_candidate.update(unlocked)

            # 3. P1-First Strategy: Only unlock P2 if P1 does NOT exist or has no verified email
            if not p1_candidate or not p1_candidate.get("email"):
                if p2_candidate and not p2_candidate.get("email"):
                    unlocked = self.unlock_work_email(p2_candidate.get("id"), event_id=comp_id, event_name=comp_name)
                    if unlocked:
                        p2_candidate.update(unlocked)

            # 4. Propagate P1 and P2 across ALL linked events in cvent_people
            p1_name = "-"
            p2_name = "-"
            events_updated_this_company = 0

            for ev in linked_events:
                eid = ev["id"] if hasattr(ev, "keys") else ev[0]
                ename = ev["event_name"] if hasattr(ev, "keys") else ev[1]
                eurl = ev["event_url"] if hasattr(ev, "keys") else ev[2]
                org_disp = (ev["organizer_company"] if hasattr(ev, "keys") else ev[3]) or comp_name
                c_email = (ev["creator_email"] if hasattr(ev, "keys") else ev[4]) or ""

                if p1_candidate and p1_candidate.get("email"):
                    self._upsert_apollo_person(
                        conn, eid, org_disp, p1_candidate, persona="P1",
                        priority="Primary", source_url=eurl, is_creator=(c_email.lower() == p1_candidate.get("email", "").lower())
                    )
                    p1_name = p1_candidate.get("name") or p1_candidate.get("first_name", "P1 Match")

                if p2_candidate and p2_candidate.get("email"):
                    self._upsert_apollo_person(
                        conn, eid, org_disp, p2_candidate, persona="P2",
                        priority="Secondary", source_url=eurl, is_creator=(c_email.lower() == p2_candidate.get("email", "").lower())
                    )
                    p2_name = p2_candidate.get("name") or p2_candidate.get("first_name", "P2 Match")

                # Record event checkpoint
                apollo_cache.record_event_checkpoint(eid, domain, p1_name, p2_name, 0)
                events_updated_this_company += 1

            credits_used_this_company = self.credits_spent - credits_before

            # Atomic commit to Neon DB per company
            conn.commit()

            # Record persistent company checkpoint in SQLite
            apollo_cache.record_company_checkpoint(
                company_id=comp_id,
                domain=domain,
                company_name=comp_name,
                p1_name=p1_name,
                p2_name=p2_name,
                events_linked=events_updated_this_company,
                credits_spent=credits_used_this_company
            )

            results.append({
                "company_id": comp_id,
                "company_name": comp_name,
                "domain": domain,
                "events_linked": events_updated_this_company,
                "p1_selected": p1_name,
                "p2_selected": p2_name,
                "credits_used": credits_used_this_company
            })

            companies_processed += 1
            total_events_updated += events_updated_this_company
            time.sleep(0.4)

        conn.close()

        if progress_callback:
            progress_callback(
                processed=companies_processed,
                total=total_companies,
                current_event="Completed",
                current_domain="",
                credits_batch=self.credits_spent,
                events_linked_so_far=total_events_updated
            )

        summary = credit_tracker.get_summary()

        return {
            "companies_processed": companies_processed,
            "total_events_updated": total_events_updated,
            "total_credits_spent": self.credits_spent,
            "cumulative_credits_spent": summary.get("total_credits_spent", 0),
            "results": results
        }

    # --------------------------------------------------------------------------
    # BATCH PROCESSOR WITH CHECKPOINT RESUMPTION & DOMAIN GUARDS
    # --------------------------------------------------------------------------
    def enrich_event_batch(
        self,
        batch_size: int = 5,
        credit_cap: int = 20,
        progress_callback = None,
        stop_check = None
    ) -> Dict[str, Any]:
        """
        Runs Apollo enrichment for up to `batch_size` unenriched events.
        Automatically resumes from the next unenriched event using SQLite checkpoints.
        Enforces credit_cap so it never overspends.
        Supports cooperative cancellation via stop_check() and progress updates.
        Commits per event so no work is lost on restart or early stop.
        """
        conn = get_db_connection()
        cur = conn.cursor() if hasattr(conn, 'cursor') else conn

        # Check completed checkpoints to resume seamlessly
        enriched_ids = apollo_cache.get_enriched_event_ids()

        if enriched_ids:
            placeholders = ",".join(str(eid) for eid in enriched_ids)
            sql = f"""
                SELECT c.id as event_id, c.event_name, c.event_url, c.creator_email, c.organizer_company, comp.domain
                FROM cvent c
                LEFT JOIN cvent_companies comp ON c.company_id = comp.id
                WHERE (c.creator_email != '' OR comp.domain != '')
                  AND c.id NOT IN ({placeholders})
                ORDER BY c.id ASC
                LIMIT %s
            """
        else:
            sql = """
                SELECT c.id as event_id, c.event_name, c.event_url, c.creator_email, c.organizer_company, comp.domain
                FROM cvent c
                LEFT JOIN cvent_companies comp ON c.company_id = comp.id
                WHERE (c.creator_email != '' OR comp.domain != '')
                ORDER BY c.id ASC
                LIMIT %s
            """

        cur.execute(sql, (batch_size,))
        events = cur.fetchall()
        total_events = len(events)

        results = []
        events_processed = 0

        for idx, ev in enumerate(events):
            if stop_check and stop_check():
                print(f"[Apollo] Stop requested. Halting batch gracefully.")
                break

            if self.credits_spent >= credit_cap:
                print(f"[Apollo] Credit cap ({credit_cap}) reached. Stopping batch.")
                break

            eid = ev["event_id"] if hasattr(ev, "keys") else ev[0]
            ename = ev["event_name"] if hasattr(ev, "keys") else ev[1]
            eurl = ev["event_url"] if hasattr(ev, "keys") else ev[2]
            c_email = (ev["creator_email"] if hasattr(ev, "keys") else ev[3]) or ""
            c_org = (ev["organizer_company"] if hasattr(ev, "keys") else ev[4]) or ""
            comp_domain = (ev["domain"] if hasattr(ev, "keys") else ev[5]) or ""

            # Apply Domain Guard & Smart Fallback (Improvement 2)
            domain = extract_clean_company_domain(c_email, comp_domain, eurl)

            if progress_callback:
                progress_callback(
                    processed=events_processed,
                    total=total_events,
                    current_event=ename,
                    current_domain=domain,
                    credits_batch=self.credits_spent
                )

            event_summary = {
                "event_id": eid,
                "event_name": ename,
                "domain": domain or "(Personal/No Domain)",
                "status": "processed",
                "p1_selected": None,
                "p2_selected": None,
                "credits_used": 0
            }

            credits_before = self.credits_spent

            if not self.is_configured():
                # Dry run / Unconfigured mode: Ensure default values are '-'
                cur.execute("""
                    UPDATE cvent_people
                    SET persona = COALESCE(NULLIF(persona, ''), '-'),
                        outreach_priority = COALESCE(NULLIF(outreach_priority, ''), '-')
                    WHERE event_id = %s
                """, (eid,))
                conn.commit()
                event_summary["status"] = "dry_run_unconfigured"
                apollo_cache.record_event_checkpoint(eid, domain, "-", "-", 0)
                results.append(event_summary)
                events_processed += 1
                continue

            # Live Apollo Flow:
            # Step 1: Identify Creator (always safe, uses the exact email)
            creator_profile = None
            if c_email:
                creator_profile = self.match_creator(c_email, event_id=eid, event_name=ename)

            creator_title = creator_profile.get("title", "") if creator_profile else ""
            creator_persona = classify_title(creator_title)

            # Step 2: Skip /organizations/enrich (Saves 1 credit per event!)
            # We already have company info from Cvent scraping; mixed_people/api_search
            # queries Apollo directly using q_organization_domains for 0 credits.
            candidates = self.search_persona_candidates("", domain, event_id=eid, event_name=ename) if domain else []

            p1_candidate = None
            p2_candidate = None

            # Compare creator first (Scenario 4: Prefer creator if equally ranked)
            if creator_persona == "P1":
                p1_candidate = creator_profile
            elif creator_persona == "P2":
                p2_candidate = creator_profile

            # Search results comparison
            for cand in candidates:
                c_title = cand.get("title", "")
                persona = classify_title(c_title)
                if persona == "P1" and not p1_candidate:
                    p1_candidate = cand
                elif persona == "P2" and not p2_candidate:
                    p2_candidate = cand

            # Step 4: Unlock work email for selected candidates (1 credit each)
            if p1_candidate and not p1_candidate.get("email"):
                unlocked = self.unlock_work_email(p1_candidate.get("id"), event_id=eid, event_name=ename)
                if unlocked:
                    p1_candidate.update(unlocked)

            if p2_candidate and not p2_candidate.get("email"):
                unlocked = self.unlock_work_email(p2_candidate.get("id"), event_id=eid, event_name=ename)
                if unlocked:
                    p2_candidate.update(unlocked)

            p1_name = "-"
            p2_name = "-"

            # Save / Update P1
            if p1_candidate:
                self._upsert_apollo_person(
                    conn, eid, c_org, p1_candidate, persona="P1",
                    priority="Primary", source_url=eurl, is_creator=(p1_candidate == creator_profile)
                )
                p1_name = p1_candidate.get("name") or p1_candidate.get("first_name", "P1 Match")
                event_summary["p1_selected"] = p1_name

            # Save / Update P2
            if p2_candidate:
                self._upsert_apollo_person(
                    conn, eid, c_org, p2_candidate, persona="P2",
                    priority="Secondary", source_url=eurl, is_creator=(p2_candidate == creator_profile)
                )
                p2_name = p2_candidate.get("name") or p2_candidate.get("first_name", "P2 Match")
                event_summary["p2_selected"] = p2_name

            # Scenario 3 & 4: If creator was identified but was neither P1 nor P2, update their profile with real title & mark 'Not Selected'
            if creator_profile and c_email:
                if p1_candidate != creator_profile and p2_candidate != creator_profile:
                    self._upsert_apollo_person(
                        conn, eid, c_org, creator_profile,
                        persona=creator_persona,
                        priority="Not Selected",
                        source_url=eurl,
                        is_creator=True
                    )

            credits_used_this_event = self.credits_spent - credits_before
            event_summary["credits_used"] = credits_used_this_event

            # Atomic commit per event to Neon DB
            conn.commit()

            # Record persistent checkpoint to SQLite
            apollo_cache.record_event_checkpoint(
                event_id=eid,
                domain=domain or "no_domain",
                p1_name=p1_name,
                p2_name=p2_name,
                credits_spent=credits_used_this_event
            )

            results.append(event_summary)
            events_processed += 1
            time.sleep(0.5)  # Rate limiting safety

        conn.close()

        if progress_callback:
            progress_callback(
                processed=events_processed,
                total=total_events,
                current_event="Completed",
                current_domain="",
                credits_batch=self.credits_spent
            )

        summary = credit_tracker.get_summary()

        return {
            "events_processed": events_processed,
            "total_credits_spent": self.credits_spent,
            "cumulative_credits_spent": summary.get("total_credits_spent", 0),
            "results": results
        }

    def _upsert_apollo_person(
        self, conn, event_id: int, organizer: str, profile: Dict[str, Any],
        persona: str, priority: str, source_url: str, is_creator: bool
    ):
        """Insert or update person record with Apollo enriched data."""
        fname = (profile.get("first_name") or "").strip()
        lname = (profile.get("last_name") or "").strip()
        if fname.lower() == "none": fname = ""
        if lname.lower() == "none": lname = ""
        raw_name = profile.get("name") or f"{fname} {lname}".strip()
        if not raw_name or raw_name.lower() in ("none none", "none"):
            cand_info = apollo_cache.get_all_candidates_list(search=apollo_id, limit=1)
            cands = cand_info.get("candidates", [])
            name = (cands[0].get("name") if cands else "") or fname or "Executive"
        else:
            name = raw_name
        title = profile.get("title") or "-"
        email = profile.get("email") or ""
        linkedin = profile.get("linkedin_url") or "-"
        phone = profile.get("sanitized_phone") or "-"
        apollo_id = profile.get("id") or ""

        if not email:
            return

        cur = conn.cursor() if hasattr(conn, 'cursor') else conn
        cur.execute("""
            INSERT INTO cvent_people (
                event_id, organizer_name, contact_name, designation,
                is_creator_email, phone, linkedin_url, email, source_url,
                scraper_source, contact_type, persona, outreach_priority, apollo_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Apollo/Cvent', 'In-House', %s, %s, %s)
            ON CONFLICT (event_id, email) DO UPDATE SET
                contact_name = CASE WHEN EXCLUDED.contact_name != '' THEN EXCLUDED.contact_name ELSE cvent_people.contact_name END,
                designation = CASE WHEN EXCLUDED.designation != '-' THEN EXCLUDED.designation ELSE cvent_people.designation END,
                linkedin_url = CASE WHEN EXCLUDED.linkedin_url != '-' THEN EXCLUDED.linkedin_url ELSE cvent_people.linkedin_url END,
                persona = EXCLUDED.persona,
                outreach_priority = EXCLUDED.outreach_priority,
                apollo_id = EXCLUDED.apollo_id
        """, (
            event_id, organizer, name, title, is_creator, phone, linkedin, email,
            source_url, persona, priority, apollo_id
        ))


# ==============================================================================
# BACKGROUND QUEUE WORKER (IMPROVEMENTS 1 & 3: COMPANY-WISE)
# ==============================================================================

class ApolloBackgroundWorker:
    """
    Manages non-blocking asynchronous Company-Wise Apollo lead enrichment in a dedicated daemon thread.
    Prevents HTTP gateway timeouts on Render and client browsers during large batches.
    Deduplicates across multi-event companies and provides real-time thread-safe status polling.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self.is_running = False
        self.should_stop = False
        self.batch_size = 0
        self.processed = 0
        self.total = 0
        self.events_linked = 0
        self.current_event = ""
        self.current_domain = ""
        self.credits_spent_batch = 0
        self.status_message = "Idle"
        self.error = None
        self.last_result = None

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            from credit_tracker import credit_tracker
            summary = credit_tracker.get_summary()
            return {
                "is_running": self.is_running,
                "should_stop": self.should_stop,
                "batch_size": self.batch_size,
                "processed": self.processed,
                "total": self.total,
                "events_linked": self.events_linked,
                "current_event": self.current_event,
                "current_domain": self.current_domain,
                "credits_spent_batch": self.credits_spent_batch,
                "cumulative_credits_spent": summary.get("total_credits_spent", 0),
                "status_message": self.status_message,
                "error": self.error,
                "last_result": self.last_result
            }

    def stop(self) -> bool:
        with self._lock:
            if self.is_running:
                self.should_stop = True
                self.status_message = "Stop signal received. Halting gracefully after current company..."
                return True
            return False

    def start_batch(self, batch_size: int = 5, credit_cap: Optional[int] = None) -> Tuple[bool, str]:
        with self._lock:
            if self.is_running:
                return False, "An Apollo enrichment batch is already actively running."

            # Calculate safety credit cap (default: batch_size * 3 credits max)
            cap = credit_cap if (credit_cap is not None and credit_cap > 0) else max(15, batch_size * 3)

            self.is_running = True
            self.should_stop = False
            self.batch_size = batch_size
            self.processed = 0
            self.total = batch_size
            self.events_linked = 0
            self.current_event = "Initializing company queue..."
            self.current_domain = ""
            self.credits_spent_batch = 0
            self.status_message = f"Starting Company-Wise queue worker for {batch_size} companies (Safety cap: {cap} credits)..."
            self.error = None

        thread = threading.Thread(
            target=self._worker,
            args=(batch_size, cap),
            daemon=True
        )
        thread.start()
        return True, "Enrichment worker started in background."

    def _worker(self, batch_size: int, credit_cap: int):
        try:
            enricher = ApolloEnricher()

            def _stop_check():
                with self._lock:
                    return self.should_stop

            def _progress(processed, total, current_event, current_domain, credits_batch, events_linked_so_far):
                with self._lock:
                    self.processed = processed
                    self.total = total
                    self.current_event = current_event
                    self.current_domain = current_domain
                    self.credits_spent_batch = credits_batch
                    self.events_linked = events_linked_so_far
                    if current_event == "Completed":
                        self.status_message = f"Completed! Enriched {processed} companies ({events_linked_so_far} events linked)."
                    else:
                        step_num = min(processed + 1, total)
                        dom_display = f" ({current_domain})" if current_domain else ""
                        self.status_message = f"Enriching company {step_num}/{total}: {current_event[:35]}{dom_display}"

            # Execute Company-Wise enrichment (deduplicates multi-event companies)
            result = enricher.enrich_company_batch(
                batch_size=batch_size,
                credit_cap=credit_cap,
                progress_callback=_progress,
                stop_check=_stop_check
            )

            with self._lock:
                self.last_result = result
                p_count = result.get("companies_processed", 0)
                e_count = result.get("total_events_updated", 0)
                c_spent = result.get("total_credits_spent", 0)
                self.processed = p_count
                self.events_linked = e_count
                self.credits_spent_batch = c_spent
                self.status_message = f"Batch finished! Enriched {p_count} companies ({e_count} events linked with P1 leads, {c_spent} credits spent)."
        except Exception as e:
            with self._lock:
                self.error = str(e)
                self.status_message = f"Worker encountered error: {e}"
        finally:
            with self._lock:
                self.is_running = False
                self.should_stop = False


# Global singleton worker instance
apollo_worker = ApolloBackgroundWorker()
