"""
Core Cvent Scraping & Intelligence Engine.
Dedicated and isolated for Cvent pipeline.
Coordinates live Google Serper discovery, high-fidelity parsing,
and enrichment to yield the exact 11 fields:
  1. cvent_url (unique identifier)
  2. event_url (external official website)
  3. event_name
  4. start_date (exact start date)
  5. end_date (exact end date)
  6. venue_name
  7. city
  8. state
  9. country
  10. organizer_company
  11. creator_email
"""

import os
import re
import json
import time
import threading
import urllib.request
import ssl
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import cvent_db
from cvent_parser import parse_cvent_page
from enrich_contacts import process_single_event

AGGREGATOR_BLACKLIST = {
    'cvent.com', 'web.cvent.com', 'cventevents.com',
    '10times.com', 'eventseye.com', 'allconferencealert.com',
    'eventbrite.com', 'facebook.com', 'twitter.com', 'instagram.com',
    'linkedin.com', 'youtube.com', 'wikipedia.org', 'google.com'
}


# ==============================================================================
# LOCAL SQLITE CACHE (Prevents Starting From Scratch)
# ==============================================================================

CACHE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cvent_cache.db")


def init_cache_db():
    import sqlite3
    try:
        conn = sqlite3.connect(CACHE_DB_PATH, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scraped_cache (
                cvent_url TEXT PRIMARY KEY,
                event_name TEXT DEFAULT '',
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'ok'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_url ON scraped_cache(cvent_url)")
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_cached_urls() -> set:
    init_cache_db()
    import sqlite3
    try:
        conn = sqlite3.connect(CACHE_DB_PATH, timeout=15.0)
        rows = conn.execute("SELECT cvent_url FROM scraped_cache").fetchall()
        conn.close()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def cache_scraped_url(cvent_url: str, event_name: str = ""):
    init_cache_db()
    import sqlite3
    try:
        conn = sqlite3.connect(CACHE_DB_PATH, timeout=15.0)
        conn.execute("""
            INSERT INTO scraped_cache (cvent_url, event_name, scraped_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(cvent_url) DO UPDATE SET scraped_at = CURRENT_TIMESTAMP
        """, (cvent_url, event_name))
        conn.commit()
        conn.close()
    except Exception:
        pass


def clean_external_url(url: str) -> str:
    """Validate external event website and discard blacklisted directories."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith("http"):
        return ""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()
        if any(b in domain for b in AGGREGATOR_BLACKLIST):
            return ""
        return url
    except Exception:
        return ""


def get_serper_key() -> Optional[str]:
    """Retrieve Serper API key from environment or .env."""
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if key:
        return key
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("SERPER_API_KEY="):
                    k = line.strip().split("=", 1)[1].strip()
                    if k:
                        return k
    return None


def discover_live_urls(time_filter: str = "qdr:m6", max_pages: int = 5, progress_callback=None) -> List[str]:
    """
    Discovers live web.cvent.com event URLs using Serper API.
    Supports time filters:
      - 'qdr:d' (past 24 hours)
      - 'qdr:w' (past 1 week)
      - 'qdr:m' (past 1 month)
      - 'qdr:m3' (past 3 months)
      - 'qdr:m6' (past 6 months - default)
      - 'qdr:y' (past 1 year)
      - '' (all-time / any time)
    """
    api_key = get_serper_key()
    if not api_key:
        msg = "[!] No SERPER_API_KEY available for discovery."
        print(msg)
        if progress_callback:
            progress_callback(msg)
        return []

    # 100% Category-Agnostic Discovery Queries based on Cvent's Universal Structural UI
    queries = [
        "site:web.cvent.com/event",
        'site:web.cvent.com/event "Register"',
        'site:web.cvent.com/event "Online registration by Cvent"',
        'site:web.cvent.com/event "Summary"',
        'site:web.cvent.com/event "When"',
        'site:web.cvent.com/event "Contact Us"',
        "site:web.cvent.com/event 2026",
        "site:web.cvent.com/event 2027"
    ]
    seen_urls = set()
    results = []

    for q_idx, q in enumerate(queries):
        if progress_callback:
            progress_callback(f"Query {q_idx+1}/{len(queries)}: '{q}' (time filter: {time_filter or 'any'})...")
        for page in range(1, max_pages + 1):
            success = False
            for attempt in range(3):
                try:
                    payload_data = {"q": q, "page": page, "num": 10}
                    if time_filter:
                        payload_data["tbs"] = time_filter

                    payload = json.dumps(payload_data).encode("utf-8")
                    req = urllib.request.Request(
                        "https://google.serper.dev/search",
                        data=payload,
                        headers={
                            "X-API-KEY": api_key,
                            "Content-Type": "application/json"
                        }
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode("utf-8"))

                    organic = data.get("organic", [])
                    if not organic:
                        success = True
                        break

                    page_new = 0
                    for item in organic:
                        link = item.get("link", "")
                        m = re.match(r"(https?://web\.cvent\.com/event/[a-f0-9\-]+)", link, re.I)
                        if m:
                            canonical = m.group(1)
                            if canonical not in seen_urls:
                                seen_urls.add(canonical)
                                results.append(canonical)
                                page_new += 1
                    if progress_callback:
                        progress_callback(f"  Page {page}: found {len(organic)} results ({page_new} new Cvent URLs, total: {len(results)})")
                    success = True
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(1.5)
                        continue
                    print(f"[!] Error discovering URLs (page {page}): {e}")
                    if progress_callback:
                        progress_callback(f"  Page {page} error: {e}")
            if not success or (not organic and success):
                break

    return results


def process_and_save_event(cvent_url: str) -> Optional[Dict[str, Any]]:
    """
    Parses a single Cvent URL, extracts contacts & creator email,
    and writes the 11 verified fields into 'cvent' table.
    Also extracts/links company to 'cvent_companies'.
    """
    cvent_url = cvent_url.strip()
    if not cvent_url:
        return None

    parsed = parse_cvent_page(cvent_url)
    if not parsed or parsed.get("event_name") == "Error fetching page":
        return None

    # Contact & creator email enrichment
    row_stub = {
        'event_uuid': '',
        'resolved_url': parsed.get('resolved_url', cvent_url),
        'cvent_url': cvent_url,
        'organizer': parsed.get('organizer', ''),
        'client_domain': parsed.get('client_domain', '')
    }
    enrich_result = process_single_event(row_stub)

    creator_email = enrich_result.get('creator_email', '')
    if not creator_email and enrich_result.get('contact_email'):
        creator_email = enrich_result.get('contact_email')

    # Clean official external website
    event_url = clean_external_url(parsed.get('event_website') or parsed.get('resolved_url', ''))

    record = {
        'cvent_url': cvent_url,
        'event_url': event_url,
        'event_name': parsed.get('event_name', '') or "Untitled Cvent Event",
        'start_date': parsed.get('start_date', ''),
        'end_date': parsed.get('end_date', ''),
        'venue_name': parsed.get('venue_name', ''),
        'city': parsed.get('city', ''),
        'state': parsed.get('state_region', ''),
        'country': parsed.get('country', ''),
        'organizer_company': parsed.get('organizer', ''),
        'creator_email': creator_email,
        'contact_name': enrich_result.get('contact_name', ''),
        'contact_phone': enrich_result.get('creator_phone') or enrich_result.get('contact_phone', ''),
        'general_emails': enrich_result.get('general_emails', ''),
        'contact_type': enrich_result.get('contact_type', 'In-House')
    }

    event_id = cvent_db.upsert_cvent_event(record)
    record["id"] = event_id

    # Update local SQLite cache
    cache_scraped_url(cvent_url, record.get('event_name', ''))

    # Automatically extract company & link if company or email is present
    if event_id:
        try:
            cvent_db.extract_companies_from_cvent([event_id])
        except Exception:
            pass

    return record


# ==============================================================================
# PIPELINE BACKGROUND RUNNER & STATUS
# ==============================================================================

class CventScraperRunner:
    """Manages background discovery & scraping runs with real-time UI logging."""

    def __init__(self):
        self._lock = threading.RLock()
        self.is_running = False
        self.should_stop = False
        self.status_message = "Idle"
        self.scraped_count = 0
        self.total_discovered = 0
        self.errors_count = 0
        self.logs = []
        self.recent_events = []

    def log(self, msg: str):
        with self._lock:
            timestamp = time.strftime("%H:%M:%S")
            line = f"[{timestamp}] {msg}"
            self.logs.append(line)
            if len(self.logs) > 500:
                self.logs.pop(0)
            self.status_message = msg

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "is_running": self.is_running,
                "status_message": self.status_message,
                "scraped_count": self.scraped_count,
                "total_discovered": self.total_discovered,
                "errors_count": self.errors_count,
                "logs": list(self.logs[-50:]),
                "recent_events": list(self.recent_events[-10:])
            }

    def stop(self):
        with self._lock:
            self.should_stop = True
            self.status_message = "Stopping scraper..."

    def run_discovery_pipeline(self, time_filter: str = "qdr:m6", max_pages: int = 5, max_workers: int = 5):
        """Execute discovery and scraping in a background thread."""
        with self._lock:
            if self.is_running:
                return False
            self.is_running = True
            self.should_stop = False
            self.scraped_count = 0
            self.total_discovered = 0
            self.errors_count = 0
            self.logs = []
            self.recent_events = []

        thread = threading.Thread(
            target=self._worker,
            args=(time_filter, max_pages, max_workers),
            daemon=True
        )
        thread.start()
        return True

    def _worker(self, time_filter: str, max_pages: int, max_workers: int):
        try:
            self.log(f"Starting Cvent discovery (time filter: {time_filter or 'any'}, max pages: {max_pages})...")
            urls = discover_live_urls(time_filter=time_filter, max_pages=max_pages, progress_callback=self.log)

            with self._lock:
                self.total_discovered = len(urls)

            if not urls:
                self.log("No new Cvent URLs found on Google. Discovery complete.")
                return

            # Check SQLite Cache and Neon PostgreSQL to avoid scraping from scratch
            existing_db_urls = set()
            try:
                existing_db_urls = cvent_db.get_existing_cvent_urls()
            except Exception:
                pass
            cached_urls = get_cached_urls() | existing_db_urls

            new_urls = [u for u in urls if u not in cached_urls]
            cached_count = len(urls) - len(new_urls)

            if cached_count > 0:
                self.log(f"[SQLite Cache] {cached_count}/{len(urls)} URLs already cached in database. Skipping redundant scraping.")

            if not new_urls:
                self.log(f"All {len(urls)} discovered events are already up to date in cache! Zero duplicate requests made.")
                return

            self.log(f"Found {len(new_urls)} brand new Cvent URLs to scrape. Beginning extraction...")

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_url = {executor.submit(process_and_save_event, url): url for url in new_urls}

                for future in as_completed(future_to_url):
                    if self.should_stop:
                        self.log("Scraping cancelled by user.")
                        break

                    url = future_to_url[future]
                    try:
                        record = future.result()
                        if record:
                            with self._lock:
                                self.scraped_count += 1
                                self.recent_events.append({
                                    "id": record.get("id"),
                                    "name": record.get("event_name"),
                                    "email": record.get("creator_email"),
                                    "company": record.get("organizer_company"),
                                    "city": record.get("city")
                                })
                                count = self.scraped_count
                            has_email = "✓ Email found" if record.get("creator_email") else "No email"
                            self.log(f"[{count}/{len(urls)}] Scraped: {record.get('event_name')[:40]} ({has_email})")
                        else:
                            with self._lock:
                                self.errors_count += 1
                    except Exception as e:
                        with self._lock:
                            self.errors_count += 1
                        self.log(f"Error scraping {url}: {e}")

            # Re-sync companies after batch run
            self.log("Syncing extracted Cvent companies...")
            try:
                cvent_db.extract_companies_from_cvent()
            except Exception as ce:
                self.log(f"Company sync notice: {ce}")
            self.log(f"Pipeline complete! Successfully processed {self.scraped_count} events.")

        except Exception as e:
            self.log(f"Fatal error in pipeline: {e}")
        finally:
            with self._lock:
                self.is_running = False


# Global engine instance
runner = CventScraperRunner()
