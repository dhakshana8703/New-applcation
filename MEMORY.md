# Project Memory: Dedicated Cvent Intelligence Pipeline

*Workspace: C:\Users\hp\Desktop\cvent-scrapper*  
*Last Updated: 2026-11-11*  
*Current Commit: ee15b7c (Synced to both origin/main and manager/main)*  
*Server Port: 5005 (http://127.0.0.1:5005) & Live on Render*

---

## 1. System Mission & Core Pipeline Flow

The Cvent Intelligence Pipeline is a dedicated, production outbound lead engine designed for Nunify.  
The core architecture follows the manager-approved 3-stage hierarchy:

$$\mathbf{Events} \longrightarrow \mathbf{Companies} \longrightarrow \mathbf{People}$$

1. **Events Tab:** 761 Cvent event registrations with 13 discrete columns (including dedicated separate columns for **Organizer Name** and **Website**).
2. **Companies Tab:** 393 verified corporate, academic, and government organizations extracted from organizer domains.
3. **People Tab:** 2,891 unified outbound targets combining Cvent Event Creators (PostgreSQL) and Apollo-discovered P1/P2 Decision-Makers (SQLite cache).

---

## 2. Apollo Lead Enrichment & Credit Guardrails

To strictly protect outbound budgets and prevent accidental credit burn:

* **0-Credit Company Discovery (`/api/companies/<id>/enrich`):**
  - Uses Apollo mixed search to identify company leadership without unlocking contact details.
  - Cost: **0 Credits**.
  - Discovers **P1 Decision-Makers** (VP, Director, Head of Events, CMO, CXO) and **P2 Operational Buyers** (Managers, Coordinators, Specialists).
* **1-Credit On-Demand Unlock (`/api/apollo/unlock-candidate`):**
  - Unlocking emails/LinkedIn is strictly on-demand per row.
  - Cost: **1 Apollo Credit** per lead.
  - Automatically deduplicated: once unlocked, the verified email and LinkedIn are permanently cached in SQLite (`cvent_cache.db`) and PostgreSQL (`cvent_people`) so the pipeline **never charges twice** for the same person.
* **Credit Usage Auditing:**
  - Live expenditure logged to both `apollo_credit_usage.json` and `apollo_credit_usage.csv`.
  - Accessible via the UI button **"📊 Credit Log (CSV)"**.

---

## 3. Strict Title Sanitization & Noise Rejection

To ensure zero low-quality or irrelevant contacts enter the pipeline, `apollo_enricher.py` and `apollo_cache.py` enforce strict regex title sanitization (`is_valid_persona_title()`):

* **Banned Substrings (Automatically Discarded):**
  - Job seekers: `"looking for"`, `"seeking"`, `"open to"`, `"candidate for"`, `"aspiring"`, `"transitioning"`, `"between roles"`
  - Former personnel: `"former"`, `"ex-"`, `"past president"`, `"past "`, `"formerly"`, `"retired"`, `"emeritus"`, `"alumni"`
  - Non-decision makers: `"student"`, `"intern"`, `"volunteer"`, `"aide"`
* Clean, active decision-makers are classified into **P1** (VP, Director, Head, Chief), **P2** (Manager, Specialist, Lead), or **Creator** (Scraped Cvent Organizer).

---

## 4. Audited Database Entities (100% Verified)

In November 2026, an exhaustive programmatic audit was performed across all records:
* **Total Companies:** **393**
* **Dummy / Test Domains Eliminated:** Purged QA events from Cvent bots (`no.mail`, `email.com`), placeholder domains (`example.com`), and personal mailboxes (`live.com`, `icloud.com`).
* **Merged Duplicates:** OpenText World 2026 was cleanly consolidated under **OpenText** (`opentext.com`).
* **Entity Expansion:** All 2- and 3-letter abbreviations were normalized to full institutional names:
  - `jmu.edu` &rarr; **James Madison University**
  - `nih.gov` &rarr; **National Institutes of Health (NIH)**
  - `osu.edu` &rarr; **Ohio State University**
  - `psu.edu` &rarr; **Penn State University**
  - `unc.edu` &rarr; **University of North Carolina**
  - `usc.edu` &rarr; **University of Southern California**
  - `aa.org` &rarr; **Alcoholics Anonymous**
  - `acc.com` &rarr; **Association of Corporate Counsel (ACC)**
  - `aha.org` &rarr; **American Hospital Association (AHA)**
  - `nswspc.org.au` &rarr; **NSW Secondary Principals' Council (NSWSPC)**
  - `state.mn.us` &rarr; **Explore Minnesota**

---

## 5. Unified People Schema & UI Controls

The `People` tab (`templates/index.html` + `cvent_db.get_unified_people()`) includes:

| Control | Function | Options / State |
| :--- | :--- | :--- |
| **Search Input** | Real-time text search | Name, Designation, Email, Domain |
| **Company Dropdown** | Isolates team members | All 393 verified companies |
| **Persona Filter** | Role-based filtering | All Personas, P1 Decision-Makers, P2 Operational Buyers, Creators |
| **Unlock Status Filter** | Pipeline readiness | All Statuses, Unlocked Leads, Locked (Ready to Unlock) |
| **Contact Type Filter** | Scraped source | All Types, In-House Only, 3rd-Party Agency Only |
| **Rows Per Page** | Pagination control | 25, 50, 100, All Contacts |
| **Export People CSV** | Direct file download | Downloads `cvent_people_<domain>.csv` with 13 unified columns |

---

## 6. Git & Deployment Synchronizations

* **Local Workspace:** `c:\Users\hp\Desktop\cvent-scrapper`
* **Remotes (Both synced to `main`):**
  - `origin`: `https://github.com/dhakshana8703/New-applcation.git`
  - `manager`: `https://github.com/silverbu11et/Nunify-Outbound.git`
* **Cloud Hosting:** Render Web Service deploying via `Procfile` (`gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4`).
