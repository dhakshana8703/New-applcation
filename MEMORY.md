# Project Memory: Dedicated Cvent Intelligence Pipeline

*Workspace: C:\Users\hp\Desktop\cvent-scrapper*  
*Last Updated: 2026-11-11*  
*Current Commit: a6371ff (Synced to both origin/main and manager/main)*  
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

---

## 7. Role-Based Access Control (RBAC) & Team Company Allocation

Implemented enterprise-grade authentication, role-based permissions, and company partitioning:

* **Roles:**
  - `admin`: Full unrestricted access to all 393 companies, 761 events, and 2,891 people. Access to Admin Team Management modal, company allocation modal, password reset, user provisioning/deprovisioning, and scraper diagnostics.
  - `user` (Normal User / SDR): Strictly partitioned access. Only sees companies explicitly allocated to them by an admin, along with the corresponding people and events. The Scraper tab and Admin management actions are completely hidden in the UI and protected on backend endpoints with 403 Forbidden.
* **Database Schema (Neon PostgreSQL):**
  - `cvent_users`: User directory (`id`, `username`, `password_hash`, `full_name`, `role`, `is_active`, `created_at`, `updated_at`).
  - `cvent_user_company_assignments`: Many-to-many allocation (`id`, `user_id`, `company_id`, `assigned_at`, `UNIQUE(user_id, company_id)`).
* **Company Allocation Engine:**
  - Admin modal with real-time text search across 393 companies.
  - Batch assignment buttons: "Select All Filtered", "Deselect All", "Select Top 20", "Select Top 50".
  - Live selection counter and instant save.
* **Default Seeded Accounts:**
  - Admin: `admin` / `admin123`
  - SDR User: `sdr_user` / `user123`

---

## 8. Per-User Credit Auditing & Production Security Hardening

* **Per-User Credit Tracking (`cvent_credit_transactions` in Neon PostgreSQL):**
  - Records every on-demand contact unlock with `user_id`, `username`, `target_name`, `company_domain`, `apollo_id`, `credits_spent`, and timestamp.
  - SDR Dashboard displays personal live credit counter in the header badge and targeted outreach banner.
  - Admin Team Dashboard features a dedicated **⚡ Credits Used** column per user with an interactive **🔍 Audit Log Modal** detailing all individual unlocks.
* **Strict Backend Security Gatekeeping (`@require_admin`):**
  - Apollo API Key configuration, queue management, batch enrichment, candidate sheets/JSON downloads, and audit logs are strictly restricted to Administrators. Non-admin access is rejected with `403 Forbidden`.
  - Database deletion (bulk event delete, single event delete, company deletion) and company re-extraction are strictly locked behind `@require_admin`.
  - Outreach SDRs are strictly restricted to unlocking leads belonging to their assigned company domains.
* **Clean SDR Outreach Workspace (UI Hardening):**
  - Apollo batch engine controls, API key previews, global credit spend, and raw candidate export buttons are completely hidden from SDR views.
  - Replaced with a modern, distraction-free **Outbound Outreach Pipeline** header displaying assigned account counts and personal unlocked lead counts.

---

## 9. CRM Outbound Portal & Lead Progress Tracking

* **Real-Time CRM Lead Funnel Metrics (`/api/crm/stats` & `cvent_db.get_crm_pipeline_stats()`):**
  - For each SDR rep, dynamically calculates across their allotted accounts:
    - **Total Leads:** Combined Apollo candidate decision-makers + Cvent event creators.
    - **Unlocked Leads:** Contacts with verified work email & LinkedIn ready for active email/calling campaigns.
    - **Remaining Leads:** High-value prospects waiting for on-demand 1-click unlock.
    - **Unlock Progress Percentage:** Dynamic % completed of the rep's allocated total target pool.
* **SDR Interactive Command Center:**
  - **4-Card KPI Funnel:** Target Accounts | Total Leads | Unlocked & Ready | Remaining to Unlock.
  - **Segmented Visual Progress Bar:** Dual-colored progress bar dynamically displaying unlocked % vs remaining %.
  - **Quick-Filter Action Pills:** 1-click instant switching between *All Leads*, *✓ Unlocked Only*, and *🔒 Remaining Only*.
* **Company-Level CRM Lead Badging:**
  - In the Companies tab, each account row explicitly displays `✓ Unlocked` and `🔒 Remaining` counts alongside a direct `👥 Leads (X)` action button.
* **Admin Executive Overview:**
  - In `👥 Team & Allocations`, Admin monitors real-time SDR progress through a dedicated **CRM Lead Pipeline** column displaying exact unlocked vs remaining counts for every rep.
