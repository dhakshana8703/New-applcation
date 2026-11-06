# Project Memory: Dedicated Cvent Intelligence Pipeline

*Workspace: C:\Users\hp\Desktop\cvent-scrapper*  
*Last Updated: 2026-11-05*  
*Status: Active, Fully Isolated, Zero db_manager Dependency*

---

## 1. System Mission & Architecture
* **Goal:** A pure, fully isolated, end-to-end intelligence pipeline built strictly for Cvent event discovery, parsing, and organizer extraction.
* **Complete Independence:**
  * **Zero `db_manager.py` dependency:** All legacy managers, monolithic multi-source logic, and legacy tables (`events`, `companies`, `people`, `personas`, `campaigns`) are completely removed.
  * **Zero unrelated scrapers:** No EventsEye, 10times, or AllConferenceAlert logic.
  * **Self-Contained Engine:** Dedicated crawler (`cvent_engine.py`), parser (`cvent_parser.py`), contact enricher (`enrich_contacts.py`), and database layer (`cvent_db.py`).

---

## 2. Dedicated Data Schema (Neon PostgreSQL)
The pipeline operates strictly on dedicated tables in Neon branch `ep-sparkling-breeze-aodc615z-pooler`:

| Table | Total Rows | Description |
| :--- | :--- | :--- |
| **`cvent`** | **542** | Verified Cvent events with normalized `contact_name`, `contact_phone`, `creator_email`, `general_emails`, `contact_type`. |
| **`cvent_people`** | **1,106** | Discrete, relational contacts matching manager's 9 required columns. 100% verified with zero identity mismatches. |
| **`cvent_companies`** | **318** | Pure Cvent organizer companies extracted from corporate domains. |

*Ground-Truth Scraper & Audit Status (2026-11-05):*
* `cvent_truth_scraper.py`: Fetched 100% live SSR hydration state across all 542 events in 173s.
* `Total Contacts in Database`: 1,106
* `Creators (Personal Accounts)`: 613
* `Secondary / Team Contacts`: 493
* `In-House`: 1,075 | `3rd-Party Agency`: 31
* `Unicode/HTML Entity Errors`: 0
* `Generic Inboxes Marked as Creator`: 0
* `Name-to-Email Identity Mismatches`: 0

---

## 3. Dedicated Data Pipeline Flow

$$\text{1. Serper Discovery} \longrightarrow \text{2. JSON-LD / HTML Parse} \longrightarrow \text{3. Contact Extraction} \longrightarrow \text{4. Upsert 'cvent'} \longrightarrow \text{5. Dedup into 'cvent_companies'}$$

1. **Discovery & Crawling (`cvent_engine.py`):**
   * Discovers active `site:web.cvent.com/event` registration pages via Serper with time windows (`24h`, `1w`, `1m`, `all`).
   * Fetches event pages and parses schema.org JSON-LD and HTML (`cvent_parser.py`).
   * Extracts organizer company names and `createdBy` planner emails (`enrich_contacts.py`).
   * Upserts into the `cvent` table.
2. **Company Extraction & Linking (`cvent_db.extract_companies_from_cvent`):**
   * Isolates clean corporate domains from creator emails and event websites (skips consumer domains like `@gmail.com`, `@yahoo.com`, etc.).
   * Deduplicates companies by domain and clean name into `cvent_companies`.
   * Links each event: `cvent.company_id = cvent_companies.id`.

---

## 4. Standalone Dashboard & Service
* **Port:** **5005** (`python run.py` / `run.bat`).
* **Backend:** [`app.py`](file:///C:/Users/hp/Desktop/cvent-scrapper/app.py) (~200 lines, standalone Flask, zero legacy imports).
* **Frontend:** [`templates/index.html`](file:///C:/Users/hp/Desktop/cvent-scrapper/templates/index.html):
  * **Events Tab:** Browse 542 Cvent events, search, filter by status, inline edit, delete, and CSV export.
  * **Companies Tab:** Browse 318 Cvent companies with event counts, website/domain links, organizer emails, one-click re-extraction, and CSV export.
  * **Scraper & Diagnostics Tab:** Run live discovery crawls, test any individual Cvent URL on demand, and monitor real-time crawling logs via SSE.

---

## 5. Workspace Rules & Guardrails
1. **Never import `db_manager`:** Keep this workspace 100% isolated.
2. **Windows CRLF Awareness:** Files in this repository use CRLF (`\r\n`). Always maintain line ending integrity.
3. **Never run destructive git rollbacks:** Preserve all working files.
4. **Keep documentation in sync:** Update `MEMORY.md` and `BUILDING.md` after every milestone.
