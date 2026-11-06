# BUILDING.md — What We Are Creating & Making

*Workspace: C:\Users\hp\Desktop\cvent-scrapper*  
*Last Updated: 2026-11-05*

---

## 1. Product Vision: Dedicated Cvent Intelligence Engine

A completely isolated, autonomous pipeline built exclusively for Cvent event intelligence. It crawls live Cvent registration pages, parses event schedules and metadata, extracts organizer companies and direct planner emails, and manages the data in a dedicated high-speed dashboard.

---

## 2. Workspace File Architecture

```
cvent-scrapper/
├── .env                  # Neon PostgreSQL & Serper API credentials
├── .gitignore            # Git hygiene guardrail
├── MEMORY.md             # Persistent memory of architecture & table truths
├── BUILDING.md           # Product roadmap, in-flight work & milestones
├── README.md             # User onboarding & quickstart guide
├── db_compat.py          # Dual backend layer (Neon PostgreSQL + SQLite fallback)
├── cvent_db.py           # Dedicated DB operations for cvent and cvent_companies (Zero db_manager)
├── cvent_parser.py       # High-fidelity schema.org & metadata HTML parser
├── enrich_contacts.py    # Planner & creator email extractor
├── cvent_engine.py       # Live Serper discovery, thread-pooled crawler & SSE logger
├── app.py                # Standalone Flask dashboard & REST APIs (port 5005)
├── run.py / run.bat      # One-click Windows launch scripts
└── templates/
    └── index.html        # Clean 3-tab UI (Events, Companies, Scraper Engine)
```

---

## 3. Milestones & Progress

| Milestone | Description | Priority | Status |
| :--- | :--- | :--- | :--- |
| **M1: Isolated Workspace** | Set up clean `cvent-scrapper` workspace with `.env` and `db_compat.py` | High | **Done** |
| **M2: Pure Data Schema** | Wire `cvent` (542 rows) and `cvent_companies` (318 rows) | High | **Done** |
| **M3: Company Extraction** | High-performance batch extraction linking Cvent Events → Cvent Companies | High | **Done** |
| **M4: Live Scraper Engine** | Serper Google discovery with time filters (24h, 1w, 1m) and single-URL tester | High | **Done** |
| **M5: Standalone UI** | Dedicated dashboard on port 5005 for Events, Companies, and Scraper Engine | High | **Done** |
| **M6: CSV Exports** | Clean CSV export for Cvent events and Cvent companies | High | **Done** |
| **M7: Direct Contact Ingestion** | Turn Cvent `creator_email` directly into outreach-ready contacts | High | **Next** |
| **M8: Automated Discovery Cron** | Automated recurring discovery runs (daily/weekly Serper query sweeps) | Medium | Planned |
| **M9: Direct Webhook Integration** | Webhook listener or push to outreach sequences directly from `cvent-scrapper` | Medium | Planned |

---

## 4. Current Operational Metrics (Live from Neon DB)
* **Events Ingested:** 542 total
  * With Verified Email: 507 (93.5%)
  * Linked to Company: 506 (93.4%)
  * With Official External Website: 137
  * Requiring Review / Incomplete: 33
* **Companies Extracted:** 318 unique companies
  * With Corporate Domain: 316
  * With Direct Contact Email: 313
