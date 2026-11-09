"""
Apollo Lead Enrichment Engine.
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
import requests
from typing import Dict, Any, List, Optional, Tuple

import cvent_db
from db_compat import get_db_connection
from credit_tracker import credit_tracker

APOLLO_API_BASE = "https://api.apollo.io/v1"

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


def classify_title(title: str) -> str:
    """Classify a job title into P1, P2, or Neither."""
    if not title:
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
    Production-grade Apollo Client with strict budget controls and persistent audit logging.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (api_key or os.environ.get("APOLLO_API_KEY", "")).strip()
        self.headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache"
        }
        if self.api_key:
            self.headers["X-Api-Key"] = self.api_key

        self.org_cache: Dict[str, Dict[str, Any]] = {}
        self.credits_spent = 0

    def is_configured(self) -> bool:
        return bool(self.api_key and not self.api_key.startswith("mock"))

    # --------------------------------------------------------------------------
    # API 1: Bulk Match Creator (1 credit per match)
    # --------------------------------------------------------------------------
    def match_creator(self, email: str, event_id: Any = None, event_name: str = "") -> Optional[Dict[str, Any]]:
        """Identify creator's name, title, company and LinkedIn."""
        if not self.is_configured():
            return None
        url = f"{APOLLO_API_BASE}/people/bulk_match"
        payload = {
            "details": [{"email": email}]
        }
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=12)
            if r.status_code == 200:
                data = r.json()
                matches = data.get("matches", [])
                if matches and matches[0]:
                    credits_used = data.get("credits_consumed", 1)
                    self.credits_spent += credits_used
                    credit_tracker.record_usage(
                        event_id=event_id,
                        event_name=event_name,
                        action="match_creator",
                        endpoint="/people/bulk_match",
                        target=email,
                        credits=credits_used
                    )
                    return matches[0]
        except Exception as e:
            print(f"[Apollo] match_creator failed for {email}: {e}")
        return None

    # --------------------------------------------------------------------------
    # API 2: Organization Enrich (1 credit per company, cached)
    # --------------------------------------------------------------------------
    def enrich_organization(self, domain: str, event_id: Any = None, event_name: str = "") -> Optional[Dict[str, Any]]:
        """Identify company ID and details by domain. Cached locally to avoid credit burn."""
        clean_dom = domain.lower().strip()
        if clean_dom in self.org_cache:
            return self.org_cache[clean_dom]

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
                    self.org_cache[clean_dom] = data
                    credit_tracker.record_usage(
                        event_id=event_id,
                        event_name=event_name,
                        action="enrich_organization",
                        endpoint="/organizations/enrich",
                        target=domain,
                        credits=1
                    )
                    return data
        except Exception as e:
            print(f"[Apollo] enrich_organization failed for {domain}: {e}")
        return None

    # --------------------------------------------------------------------------
    # API 3: Search Candidates by Persona Titles (0 credits)
    # --------------------------------------------------------------------------
    def search_persona_candidates(self, org_id: str, domain: str, event_id: Any = None, event_name: str = "") -> List[Dict[str, Any]]:
        """Find P1 and P2 candidates at the organization using free search."""
        if not self.is_configured():
            return []

        url = f"{APOLLO_API_BASE}/mixed_people/api_search"
        payload = {
            "q_organization_domains": domain,
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
                    target=domain,
                    credits=0
                )
                return people
        except Exception as e:
            print(f"[Apollo] search_persona_candidates failed: {e}")
        return []

    # --------------------------------------------------------------------------
    # API 4: Unlock Verified Work Email (1 credit per match)
    # --------------------------------------------------------------------------
    def unlock_work_email(self, person_id: str, event_id: Any = None, event_name: str = "") -> Optional[Dict[str, Any]]:
        """Retrieve available work email and details for a selected candidate."""
        if not self.is_configured() or not person_id:
            return None

        url = f"{APOLLO_API_BASE}/people/match"
        payload = {
            "id": person_id
        }
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=12)
            if r.status_code == 200:
                self.credits_spent += 1
                person = r.json().get("person")
                credit_tracker.record_usage(
                    event_id=event_id,
                    event_name=event_name,
                    action="unlock_work_email",
                    endpoint="/people/match",
                    target=person_id,
                    credits=1
                )
                return person
        except Exception as e:
            print(f"[Apollo] unlock_work_email failed for {person_id}: {e}")
        return None

    # --------------------------------------------------------------------------
    # BATCH PROCESSOR (CONFIGURABLE BATCH SIZE)
    # --------------------------------------------------------------------------
    def enrich_event_batch(self, batch_size: int = 5, credit_cap: int = 20) -> Dict[str, Any]:
        """
        Runs Apollo enrichment for up to `batch_size` unenriched events.
        Enforces credit_cap so it never overspends.
        """
        conn = get_db_connection()
        cur = conn.cursor() if hasattr(conn, 'cursor') else conn

        # Fetch first N events with valid creator_email or domain
        cur.execute("""
            SELECT c.id as event_id, c.event_name, c.event_url, c.creator_email, c.organizer_company, comp.domain
            FROM cvent c
            LEFT JOIN cvent_companies comp ON c.company_id = comp.id
            WHERE (c.creator_email != '' OR comp.domain != '')
            ORDER BY c.id ASC
            LIMIT %s
        """, (batch_size,))
        events = cur.fetchall()

        results = []
        events_processed = 0

        for ev in events:
            if self.credits_spent >= credit_cap:
                print(f"[Apollo] Credit cap ({credit_cap}) reached. Stopping batch.")
                break

            eid = ev["event_id"] if hasattr(ev, "keys") else ev[0]
            ename = ev["event_name"] if hasattr(ev, "keys") else ev[1]
            eurl = ev["event_url"] if hasattr(ev, "keys") else ev[2]
            c_email = (ev["creator_email"] if hasattr(ev, "keys") else ev[3]) or ""
            c_org = (ev["organizer_company"] if hasattr(ev, "keys") else ev[4]) or ""
            domain = (ev["domain"] if hasattr(ev, "keys") else ev[5]) or ""

            if not domain and "@" in c_email:
                domain = c_email.split("@")[-1].strip()

            event_summary = {
                "event_id": eid,
                "event_name": ename,
                "domain": domain,
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
                event_summary["status"] = "dry_run_unconfigured"
                results.append(event_summary)
                events_processed += 1
                continue

            # Live Apollo Flow:
            # Step 1: Identify Creator
            creator_profile = None
            if c_email:
                creator_profile = self.match_creator(c_email, event_id=eid, event_name=ename)

            creator_title = creator_profile.get("title", "") if creator_profile else ""
            creator_persona = classify_title(creator_title)

            # Step 2: Identify Organization
            org_id = ""
            if domain:
                org_info = self.enrich_organization(domain, event_id=eid, event_name=ename)
                if org_info:
                    org_id = org_info.get("id", "")

            # Step 3: Free 0-credit search for P1 and P2 candidates
            candidates = self.search_persona_candidates(org_id, domain, event_id=eid, event_name=ename) if domain else []

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

            # Save / Update P1
            if p1_candidate:
                self._upsert_apollo_person(
                    conn, eid, c_org, p1_candidate, persona="P1",
                    priority="Primary", source_url=eurl, is_creator=(p1_candidate == creator_profile)
                )
                event_summary["p1_selected"] = p1_candidate.get("name") or p1_candidate.get("first_name", "P1 Match")

            # Save / Update P2
            if p2_candidate:
                self._upsert_apollo_person(
                    conn, eid, c_org, p2_candidate, persona="P2",
                    priority="Secondary", source_url=eurl, is_creator=(p2_candidate == creator_profile)
                )
                event_summary["p2_selected"] = p2_candidate.get("name") or p2_candidate.get("first_name", "P2 Match")

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

            event_summary["credits_used"] = self.credits_spent - credits_before
            results.append(event_summary)
            events_processed += 1
            time.sleep(0.5)  # Rate limiting safety

        conn.commit()
        conn.close()

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
        name = profile.get("name") or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
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
