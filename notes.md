# Nunify Outbound Tool — Cvent Scraper & Apollo Enrichment Engine
> **Project Status & Engineering Changelog**  
> *Last Updated: November 9, 2026*  
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
  1. **`apollo_org_cache`:** Saves company domains locally. Multiple events from the same company cost 0 additional credits.
  2. **`apollo_people_cache`:** Caches every unlocked contact permanently.
  3. **`apollo_event_checkpoints`:** Tracks which event IDs have been completed. When clicking *"Run Batch"*, the system automatically resumes from the next unenriched event.

---

### ✅ Improvement 4: Real-Time Credit Usage Tracker (`.csv` & `.json`)
* **Problem:** Management needs full transparency on Apollo credit burn and cost per lead.
* **Solution:**
  1. Built **`apollo_credit_usage.csv`**: A live spreadsheet recording timestamp, event name, endpoint called, target looked up, and running credit total.
  2. Built **`apollo_credit_usage.json`**: Machine-readable state file storing action breakdown.
  3. Added a live counter on the dashboard UI with a direct **`📊 Credit Log (CSV)`** download button.

---

### ✅ Improvement 5: Priority-First Dashboard Sorting
* **Problem:** New P1 executives discovered via Apollo were being pushed to the end of the table (page 40+) because older contacts were sorted by `is_creator_email = YES`.
* **Solution:** Re-indexed the database query so **`Primary` (P1 Decision-Makers)** and **`Secondary` (P2 Operational Buyers)** immediately sort to **Row #1 on Page 1**.

---

### ✅ Improvement 6: Non-Destructive Ingestion (Original Emails Preserved)
* The original creator emails scraped from Cvent (`equinlivan@aarp.org`, `matt@lhca.us`, `justine.kalmar@msupply.com`) are **100% preserved** in the database.
* Newly discovered executives are added as separate rows under the same event, giving sales reps both the event builder and the executive decision-maker.

---

## 4. Live Verification Output

Testing with real Cvent events verified that the engine successfully extracts real decision-makers:

| Enriched Contact | Real Job Title | Organization | Work Email | Persona & Priority |
| :--- | :--- | :--- | :--- | :---: |
| **Scott Frisch** | EVP, Chief Operating Officer & CFO | AARP | `sfrisch@aarp.org` | **P1 / Primary** |
| **Kevin Latner** | Senior Vice President | US Leather | `kevin@usleather.org` | **P1 / Primary** |
| **Chris McAllister** | Vice President Sourcing | Marcone | `chris.mcallister@marcone.com` | **P1 / Primary** |

---

## 5. Upcoming Engineering Roadmap

* [ ] **Background Asynchronous Worker:** Decouple batch runs into background worker threads so batches of 50+ events run without HTTP timeouts.
* [ ] **Weighted Title Priority Scoring:** Rank event-specific executives (`VP of Events`) higher than generic corporate officers (`CEO`).
* [ ] **Outbound CRM Integration:** Add a direct 1-click webhook to push Primary leads into Smartlead / Instantly / HubSpot.
