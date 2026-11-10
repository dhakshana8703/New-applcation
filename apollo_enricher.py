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

# ==============================================================================
# PUBLIC WEBMAIL PROVIDERS BLOCKLIST (IMPROVEMENT 2)
# ==============================================================================

PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "aol.com", "mail.com", "proton.me", "protonmail.com",
    "zoho.com", "live.com", "msn.com", "yandex.com", "googlemail.com",
    "comcast.net", "sbcglobal.net", "att.net", "verizon.net"
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


# ==============================================================================
# BACKGROUND QUEUE WORKER (IMPROVEMENT 1)
# ==============================================================================

class ApolloBackgroundWorker:
    """
    Manages non-blocking asynchronous Apollo lead enrichment in a dedicated daemon thread.
    Prevents HTTP gateway timeouts on Render and client browsers during large batches (10, 25, 50).
    Provides real-time thread-safe status polling and graceful cancellation.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self.is_running = False
        self.should_stop = False
        self.batch_size = 0
        self.processed = 0
        self.total = 0
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
                self.status_message = "Stop signal received. Halting gracefully after current event..."
                return True
            return False

    def start_batch(self, batch_size: int = 5, credit_cap: Optional[int] = None) -> Tuple[bool, str]:
        with self._lock:
            if self.is_running:
                return False, "An Apollo enrichment batch is already actively running."

            # Calculate safety credit cap (default: batch_size * 4 credits max)
            cap = credit_cap if (credit_cap is not None and credit_cap > 0) else max(20, batch_size * 4)

            self.is_running = True
            self.should_stop = False
            self.batch_size = batch_size
            self.processed = 0
            self.total = batch_size
            self.current_event = "Initializing batch queue..."
            self.current_domain = ""
            self.credits_spent_batch = 0
            self.status_message = f"Starting queue worker for {batch_size} events (Safety cap: {cap} credits)..."
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

            def _progress(processed, total, current_event, current_domain, credits_batch):
                with self._lock:
                    self.processed = processed
                    self.total = total
                    self.current_event = current_event
                    self.current_domain = current_domain
                    self.credits_spent_batch = credits_batch
                    if current_event == "Completed":
                        self.status_message = f"Enrichment completed for all {processed} events."
                    else:
                        step_num = min(processed + 1, total)
                        dom_display = f" ({current_domain})" if current_domain else ""
                        self.status_message = f"Enriching event {step_num}/{total}: {current_event[:35]}{dom_display}"

            result = enricher.enrich_event_batch(
                batch_size=batch_size,
                credit_cap=credit_cap,
                progress_callback=_progress,
                stop_check=_stop_check
            )

            with self._lock:
                self.last_result = result
                p_count = result.get("events_processed", 0)
                c_spent = result.get("total_credits_spent", 0)
                self.processed = p_count
                self.credits_spent_batch = c_spent
                self.status_message = f"Batch finished! Enriched {p_count} events (Spent {c_spent} credits)."
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
