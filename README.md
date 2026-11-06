# Cvent Intelligence Pipeline (Dedicated & Isolated Engine)

A 100% isolated, dedicated pipeline built strictly for Cvent events and companies.

---

## What This Workspace Contains
* **Events Table (`cvent`):** 542 Cvent events with the 11 verified fields (dates, venue, location, organizer company, organizer email, URLs).
* **Companies Table (`cvent_companies`):** 318 pure Cvent companies deduplicated by corporate domain.
* **Extraction Engine:** Automatic extraction from Cvent events directly into Cvent companies.
* **Scraper Engine:** Live discovery crawls and single-URL extractor.
* **Dashboard:** Clean, focused UI running on port 5005.

---

## Quick Start

### Run the Dashboard
Double click `run.bat` or run:
```bash
python run.py
```
Open **[http://127.0.0.1:5005](http://127.0.0.1:5005)** in your browser.

---

## File Structure
* `app.py`: Standalone Flask dashboard and REST APIs (~200 lines).
* `templates/index.html`: Responsive dashboard UI (Events, Companies, Scraper Engine).
* `cvent_db.py`: Database operations for `cvent` and `cvent_companies`.
* `cvent_engine.py`: Live crawler, Google Serper search, thread pool runner, and real-time logger.
* `cvent_parser.py`: Schema.org JSON-LD and HTML event extractor.
* `enrich_contacts.py`: Deep regex planner and creator email extractor.
* `db_compat.py`: Dual-mode database compatibility layer (Neon PostgreSQL + SQLite fallback).
* `MEMORY.md`: Persistent system memory.
* `BUILDING.md`: Product roadmap and milestone tracking.
