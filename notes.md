# Nunify Outbound Tool — Cvent Scraper & Apollo Enrichment Engine
> **Project Status & Engineering Changelog**  
> *Last Updated: November 10, 2026*  
> *Maintained for: Engineering Team & Management*

---

## 1. Executive Summary

This application is an automated outbound intelligence pipeline designed to find high-value event planners and decision-makers on **Cvent** and enrich them via **Apollo.io**.

* **Stage 1 (Discovery & Scraping):** Scrapes live Cvent events, company websites, and the internal creator email of the event builder.
* **Stage 2 (Apollo Lead Enrichment):** Takes the creator email and company domain, queries Apollo for true **Decision-Makers (P1)** and **Operational Buyers (P2)**, unlocks verified work emails and LinkedIn profiles, and saves them into the People table without wasting API credits.

---

## 2. The 10-Column People Table Specification

All enriched contacts strictly map to the 10 target fields specified for the outbound sales team:

| # | Column Name | Source | Description | Example Value |
| :-: | :--- | :--- | :--- | :--- |
| **1** | **Name** | Apollo | Person's verified full name | Scott Frisch |
| **2** | **Designation** | Apollo | Verified job title | EVP, Chief Operating Officer & CFO |
| **3** | **Company** | Cvent / Apollo | Employer / Host Organization | AARP |
| **4** | **Email** | Apollo / Cvent | Verified corporate work email | sfrisch@aarp.org |
| **5** | **LinkedIn** | Apollo | Direct profile link | linkedin.com/in/scottfrisch |
| **6** | **Persona** | Apollo | `P1` (Decision-Maker) or `P2` (Operational) | P1 |
| **7** | **Cvent Creator** | Scraper | `YES` (if creator) / `NO` (if discovered via Apollo) | NO |
| **8** | **Outreach Priority**| Engine | `Primary` / `Secondary` / `Not Selected` | Primary |
| **9** | **Related Event(s)**| Cvent | Associated event name & link | AARP WY 2026 Volunteer Summit |
| **10**| **Source** | System | Origin scraper tag | Apollo/Cvent |

---

## 3. Engineering Improvements Completed

### ✅ Improvement 1: Apollo Security Header Authentication
* **Problem:** Apollo deprecated API keys in request bodies and URL queries, returning `422 INVALID_API_KEY_LOCATION`.
* **Solution:** Updated all 4 Apollo API endpoints (`/people/bulk_match`, `/organizations/enrich`, `/mixed_people/api_search`, `/people/match`) to pass credentials strictly inside the `X-Api-Key` HTTP header.

---

### ✅ Improvement 2: Public Email Filter & Smart Fallback (No Wasted Google Credits)
* **Problem:** If a planner used `sarah@gmail.com`, searching Apollo for `gmail.com` returned **Sundar Pichai (Google CEO)** and burned 3 credits on Google executives.
* **Solution:**
  1. Built a blocklist of public webmail providers (`gmail`, `yahoo`, `hotmail`, `outlook`, `icloud`, `zoho`, etc.).
  2. Implemented smart fallback: If an email is Gmail, the system extracts the domain from the event's custom URL (e.g. `events.austinmarathon.com` $\rightarrow$ `austinmarathon.com`) and enriches the actual host company.
  3. If no corporate domain exists, company search is skipped safely (0 credits wasted).

---

### ✅ Improvement 3: Persistent SQLite Caching & Checkpoint Recovery (`cvent_cache.db`)
* **Problem:** If the server reboots or crashes, you should never have to re-enrich from scratch or pay twice for the same company.
* **Solution:**
  1. **`apollo_org_cache`:** Saves company domains locally. Multiple events from the same company cost 0 additional credits (e.g., AARP's multiple events reused 1 lookup).
  2. **`apollo_people_cache`:** Caches every unlocked contact permanently.
  3. **`apollo_event_checkpoints`:** Tracks which event IDs have been completed. When clicking *"Run Batch"*, the system automatically resumes from the next unenriched event.

---

### ✅ Improvement 4: Real-Time Credit Usage Tracker (`.csv` & `.json`)
* **Problem:** Management needs full transparency on Apollo credit burn and cost per lead.
* **Solution:**
  1. Built **`apollo_credit_usage.csv`**: A live spreadsheet recording timestamp, event name, endpoint called, target looked up, and running credit total.
  2. Built **`apollo_credit_usage.json`**: Machine-readable state file storing action breakdown.
  3. Added a live counter on the dashboard UI with a direct **`📊 Credit Log (CSV)`** download button.
  4. Added re-entrant lock handling (`threading.RLock`) with automatic retry loops to prevent file contention on Windows.

---

### ✅ Improvement 5: Priority-First Dashboard Sorting
* **Problem:** New P1 executives discovered via Apollo were being pushed to the end of the table (page 40+) because older contacts were sorted by `is_creator_email = YES`.
* **Solution:** Re-indexed the database query so **`Primary` (P1 Decision-Makers)** and **`Secondary` (P2 Operational Buyers)** immediately sort to **Row #1 on Page 1**.

---

### ✅ Improvement 6: Non-Destructive Ingestion (Original Emails Preserved)
* The original creator emails scraped from Cvent (`equinlivan@aarp.org`, `matt@lhca.us`, `justine.kalmar@msupply.com`) are **100% preserved** in the database.
* When a creator is an operational coordinator, their real title is retrieved and priority is marked `Not Selected`.
* Newly discovered executives are added as separate rows under the same event, giving sales reps both the event builder and the executive decision-maker.

---

### ✅ Improvement 7: Asynchronous Background Queue Worker & Real-Time Telemetry (v2.0)
* **Problem:** Running large batches (10, 25, 50 events) in a synchronous HTTP request triggered gateway timeouts (504 Gateway Timeout on Render/reverse proxies and browser drops after 30–60 seconds).
* **Solution:**
  1. Built **`ApolloBackgroundWorker`**: A dedicated daemon thread runner in `apollo_enricher.py` with thread-safe telemetry (`processed`, `total`, `current_event`, `current_domain`, `credits_spent_batch`).
  2. Implemented non-blocking API endpoints:
     * `POST /api/apollo/start-batch`: Returns `<50ms` confirmation and launches queue.
     * `GET /api/apollo/batch-status`: Real-time polling endpoint (polled every 1.2s by dashboard).
     * `POST /api/apollo/stop-batch`: Cooperative cancellation that cleanly finishes the active event and stops.
  3. Added batch size selector dropdown on dashboard: `5 Events`, `10 Events`, `25 Events`, `50 Events`.
  4. Built real-time animated gradient progress bar (`0%` to `100%`) showing active event and live credit burn.
  5. Implemented **atomic per-event DB commits**: Even if the server stops or the user cancels, every processed event is committed immediately with zero data loss.
  6. Added page reload auto-resume: If the user reloads the dashboard while a queue is running, the progress bar and polling automatically resume.

---

### ✅ Improvement 8: Bypassed Redundant `/organizations/enrich` (Zero Company Credit Waste)
* **Problem:** Step 2 of the original manager diagram specified `/organizations/enrich` to find the company. However, Cvent scraping already extracts the company name, website, and domain. Spending 1 credit per company to obtain Apollo's internal company ID was unnecessary.
* **Solution:**
  1. Completely bypassed `/organizations/enrich`.
  2. Routed candidate search directly through `/mixed_people/api_search` using `q_organization_domains: "<domain>"`.
  3. Cost per event dropped from ~3 credits to **1–2 credits** (~40% credit savings across the board).
  4. Verified on live event: Amwins (`amwins.com`) enriched Creator Ellie Nielsen (Event Coordinator) and found P1 Caroline Branch (COO) for just 2 credits total.

---

### ✅ Improvement 9: Company-Wise Lead Enrichment Engine (v3.0)
* **Problem:** The system was running **event-wise** (761 events). Multi-event organizers like AARP (68 events) and ABA (25 events) were being evaluated repeatedly event-by-event, causing redundant processing and wasted time/credits.
* **Solution:**
  1. Completely re-architected the batch engine to run **Company-Wise**:
     * Prioritizes high-event companies first (ranked by `event_count DESC`).
     * Enriches the verified P1 Decision-Maker directly by domain for 0 credits search + 1 credit unlock.
     * Automatically propagates that P1 executive across **every event belonging to that company** in `cvent_people`.
  2. Built **`apollo_company_checkpoints`** in SQLite to ensure no company is ever evaluated twice.
  3. **Verified Live Impact:**
     * **AARP (68 events) + American Bar Association (25 events)**:
     * **93 events were fully enriched and linked for just 2 credits total!**
  4. **Macro Economic Impact:**
     * Total pipeline load cut from **761 event iterations down to 393 company lookups**.
     * Total credit consumption for the entire 1-month Cvent database cut from ~1,800 credits down to **~450–550 credits**!

---

## 4. Live Verification Output

Live testing with verified Cvent events confirms the engine successfully identifies and extracts verified decision-makers:

| Enriched Contact | Real Job Title | Organization | Work Email | Persona & Priority |
| :--- | :--- | :--- | :--- | :---: |
| **Scott Frisch** | EVP, Chief Operating Officer & CFO | AARP | `sfrisch@aarp.org` | **P1 / Primary** |
| **Kevin Latner** | Senior Vice President | US Leather | `kevin@usleather.org` | **P1 / Primary** |
| **Chris McAllister** | Vice President Sourcing | Marcone | `chris.mcallister@marcone.com` | **P1 / Primary** |
| **Jennie Bryan** | Regional Vice President | MSupply | `jennie.bryan@msupply.com` | **P1 / Primary** |
| **Tom Leighton** | Co-founder & CEO | Akamai Technologies | `ftl@akamai.com` | **P1 / Primary** |
| **Reggie Aggarwal** | CEO & Founder | Cvent / MEDC | `raggarwal@cvent.com` | **P1 / Primary** |

*All runs strictly obeyed the 0-credit search step before unlocking work emails.*

---

## 5. Upcoming Engineering Roadmap

* [x] **Background Asynchronous Worker:** Decouple batch runs into background worker threads so batches of 50+ events run without HTTP timeouts. *(Completed in Improvement 7)*
* [ ] **Weighted Title Priority Scoring:** Rank event-specific executives (`VP of Events`) higher than generic corporate officers (`CEO`).
* [ ] **Outbound CRM Integration:** Add a direct 1-click webhook to push Primary leads into Smartlead / Instantly / HubSpot.
