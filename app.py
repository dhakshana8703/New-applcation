"""
Cvent Intelligence Engine - Standalone Dedicated Flask Application.
Completely isolated from legacy code, manager monoliths, or unrelated tables.
Strictly serves:
  - Events (cvent table)
  - Companies (cvent_companies table)
  - Scraper & Pipeline Engine
"""

import os
import re
import csv
import io
import secrets
from flask import Flask, render_template, request, jsonify, Response

import cvent_db
import cvent_engine

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cvent-secret-key-isolated-engine")
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ==============================================================================
# AUTHENTICATION (HTTP Basic Auth)
# ==============================================================================

@app.before_request
def require_basic_auth():
    """Protect the dashboard if APP_USERNAME and APP_PASSWORD are set."""
    auth_user = os.environ.get("APP_USERNAME")
    auth_pass = os.environ.get("APP_PASSWORD")

    if not auth_user or not auth_pass:
        return None

    if request.path == "/healthz":
        return None

    auth = request.authorization
    if not auth or not (
        secrets.compare_digest(auth.username or "", auth_user) and
        secrets.compare_digest(auth.password or "", auth_pass)
    ):
        return Response(
            "Access Denied: Please provide valid credentials to access the Cvent Intelligence Dashboard.\n",
            401,
            {"WWW-Authenticate": 'Basic realm="Cvent Intelligence Portal"'}
        )


@app.route("/healthz")
def healthz():
    """Unauthenticated health check endpoint for monitoring."""
    return jsonify({"status": "healthy"}), 200


@app.route("/")
def index():
    return render_template("index.html")


# ==============================================================================
# STATS API
# ==============================================================================

@app.route("/api/stats")
def api_stats():
    """Return top-level KPI metrics across Events and Companies."""
    return jsonify(cvent_db.get_all_stats())


# ==============================================================================
# EVENTS API
# ==============================================================================

@app.route("/api/events", methods=["GET"])
def api_get_events():
    limit = int(request.args.get("limit", 25))
    offset = int(request.args.get("offset", 0))
    search = request.args.get("search", "")
    status_filter = request.args.get("status", "all")
    sort_by = request.args.get("sort_by", "default")
    sort_dir = request.args.get("sort_dir", "asc")

    data = cvent_db.get_all_cvent_events(
        limit=limit,
        offset=offset,
        search=search,
        status_filter=status_filter,
        sort_by=sort_by,
        sort_dir=sort_dir
    )
    return jsonify(data)


@app.route("/api/events/<int:event_id>", methods=["GET"])
def api_get_event(event_id):
    ev = cvent_db.get_cvent_event_by_id(event_id)
    if not ev:
        return jsonify({"error": "Event not found"}), 404
    return jsonify(ev)


@app.route("/api/events/<int:event_id>", methods=["PUT"])
def api_update_event(event_id):
    updates = request.get_json() or {}
    success = cvent_db.update_cvent_event(event_id, updates)
    return jsonify({"success": success})


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def api_delete_event(event_id):
    success = cvent_db.delete_cvent_event(event_id)
    return jsonify({"success": success})


@app.route("/api/events", methods=["DELETE"])
def api_bulk_delete_events():
    payload = request.get_json() or {}
    ids = payload.get("ids", [])
    deleted = cvent_db.bulk_delete_cvent_events(ids)
    return jsonify({"deleted": deleted})


@app.route("/api/events/export", methods=["GET"])
def api_export_events_csv():
    csv_data = cvent_db.export_cvent_events_csv()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=cvent_events_export.csv"}
    )


# ==============================================================================
# COMPANIES API
# ==============================================================================

@app.route("/api/companies", methods=["GET"])
def api_get_companies():
    limit = int(request.args.get("limit", 25))
    offset = int(request.args.get("offset", 0))
    search = request.args.get("search", "")
    filter_type = request.args.get("filter", "all")

    data = cvent_db.get_all_cvent_companies(
        limit=limit,
        offset=offset,
        search=search,
        filter_type=filter_type
    )
    return jsonify(data)


@app.route("/api/companies/<int:company_id>", methods=["GET"])
def api_get_company(company_id):
    comp = cvent_db.get_cvent_company_by_id(company_id)
    if not comp:
        return jsonify({"error": "Company not found"}), 404
    return jsonify(comp)


@app.route("/api/companies/<int:company_id>", methods=["PUT"])
def api_update_company(company_id):
    updates = request.get_json() or {}
    success = cvent_db.update_cvent_company(company_id, updates)
    return jsonify({"success": success})


@app.route("/api/companies/<int:company_id>", methods=["DELETE"])
def api_delete_company(company_id):
    success = cvent_db.delete_cvent_company(company_id)
    return jsonify({"success": success})


@app.route("/api/companies/extract", methods=["POST"])
def api_extract_companies():
    result = cvent_db.extract_companies_from_cvent()
    return jsonify(result)


@app.route("/api/companies/export", methods=["GET"])
def api_export_companies_csv():
    csv_data = cvent_db.export_cvent_companies_csv()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=cvent_companies_export.csv"}
    )


# ==============================================================================
# PEOPLE API
# ==============================================================================

@app.route("/api/people", methods=["GET"])
def api_get_people():
    limit = int(request.args.get("limit", 25))
    offset = int(request.args.get("offset", 0))
    search = request.args.get("search", "")
    company_domain = request.args.get("company_domain", "all")
    persona = request.args.get("persona", "all")
    status = request.args.get("status", "all")
    is_creator = request.args.get("is_creator", "all")
    contact_type = request.args.get("contact_type", "all")
    event_id = request.args.get("event_id")
    if event_id:
        try:
            event_id = int(event_id)
        except ValueError:
            event_id = None

    data = cvent_db.get_unified_people(
        limit=limit,
        offset=offset,
        search=search,
        company_domain=company_domain,
        persona_filter=persona,
        status_filter=status,
        contact_type_filter=contact_type,
        event_id=event_id
    )
    return jsonify(data)


@app.route("/api/people/export", methods=["GET"])
def api_export_people_csv():
    search = request.args.get("search", "")
    is_creator = request.args.get("is_creator", "all")
    contact_type = request.args.get("contact_type", "all")
    event_id = request.args.get("event_id")
    if event_id:
        try:
            event_id = int(event_id)
        except ValueError:
            event_id = None

    csv_data = cvent_db.export_cvent_people_csv(
        is_creator_filter=is_creator,
        contact_type_filter=contact_type,
        search=search,
        event_id=event_id
    )
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=cvent_people_export.csv"}
    )


# ==============================================================================
# SCRAPER ENGINE API
# ==============================================================================

@app.route("/api/scraper/start", methods=["POST"])
def api_start_scraper():
    data = request.get_json() or {}
    time_filter = data.get("time_filter", "qdr:m6")
    max_pages = int(data.get("max_pages", 5))

    started = cvent_engine.runner.run_discovery_pipeline(
        time_filter=time_filter,
        max_pages=max_pages
    )
    if started:
        return jsonify({"status": "started", "message": "Discovery pipeline started in background."})
    else:
        return jsonify({"status": "already_running", "message": "Pipeline is already running."}), 409


@app.route("/api/scraper/stop", methods=["POST"])
def api_stop_scraper():
    cvent_engine.runner.stop()
    return jsonify({"status": "stopping", "message": "Stop signal sent to scraper."})


@app.route("/api/scraper/status", methods=["GET"])
def api_scraper_status():
    return jsonify(cvent_engine.runner.get_status())


@app.route("/api/scraper/test-url", methods=["POST"])
def api_test_url():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"status": "error", "message": "URL is required."}), 400

    # Strict URL validation to prevent SSRF and arbitrary ingestion
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return jsonify({"status": "error", "message": "Only HTTP and HTTPS URLs are permitted."}), 400

    netloc = parsed.netloc.lower()
    # Reject private/loopback/metadata IP addresses
    if any(netloc.startswith(p) for p in ["localhost", "127.", "169.254.", "10.", "192.168.", "172.16.", "0.0.0.0"]):
        return jsonify({"status": "error", "message": "Internal/private network addresses are blocked."}), 400

    is_cvent = (netloc == "cvent.com" or netloc.endswith(".cvent.com")) and "/event/" in parsed.path.lower()
    if not is_cvent:
        return jsonify({"status": "error", "message": "Target must be a valid Cvent event registration URL (e.g. web.cvent.com/event/...)."} ), 400

    record = cvent_engine.process_and_save_event(url)
    if record:
        return jsonify({"status": "ok", "record": record})
    else:
        return jsonify({"status": "error", "message": "Failed to scrape or extract valid metadata from the given URL."}), 422


# ==============================================================================
# APOLLO ENRICHMENT API (WITH BACKGROUND QUEUE WORKER)
# ==============================================================================

@app.route("/api/apollo/status", methods=["GET"])
def api_apollo_status():
    from apollo_enricher import ApolloEnricher, apollo_worker
    from credit_tracker import credit_tracker
    enricher = ApolloEnricher()
    key = os.environ.get("APOLLO_API_KEY", "").strip()
    masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else ("Set" if key else "Not Set")
    summary = credit_tracker.get_summary()
    return jsonify({
        "configured": enricher.is_configured(),
        "has_api_key": bool(key),
        "masked_key": masked,
        "total_credits_spent": summary.get("total_credits_spent", 0),
        "breakdown": summary.get("breakdown", {}),
        "worker": apollo_worker.get_status()
    })


@app.route("/api/companies/domains-list", methods=["GET"])
def api_get_companies_domains_list():
    """Return all unique company domains with candidate counts for dropdown."""
    data = cvent_db.get_company_domains_list()
    return jsonify(data)


@app.route("/api/companies/<int:company_id>/enrich", methods=["POST"])
def api_enrich_single_company(company_id):
    """Enrich a single company domain for 0 CREDITS using Apollo candidate search."""
    body = request.get_json() or {}
    comp = cvent_db.get_cvent_company_by_id(company_id)
    if not comp:
        return jsonify({"error": "Company not found"}), 404

    domain = comp.get("domain") or body.get("domain", "")
    company_name = comp.get("name") or body.get("name", "")

    from apollo_enricher import ApolloEnricher
    enricher = ApolloEnricher()
    result = enricher.enrich_single_company(
        domain=domain,
        company_id=company_id,
        company_name=company_name
    )
    return jsonify(result)

@app.route("/api/apollo/start-batch", methods=["POST"])
def api_apollo_start_batch():
    """Starts background Apollo enrichment queue worker."""
    from apollo_enricher import apollo_worker
    payload = request.get_json() or {}
    batch_size = int(payload.get("batch_size", 5))
    credit_cap = payload.get("credit_cap")
    if credit_cap is not None:
        credit_cap = int(credit_cap)

    success, message = apollo_worker.start_batch(batch_size=batch_size, credit_cap=credit_cap)
    if success:
        return jsonify({
            "status": "started",
            "message": message,
            "batch_size": batch_size
        })
    else:
        return jsonify({
            "status": "already_running",
            "message": message
        }), 409


@app.route("/api/apollo/batch-status", methods=["GET"])
def api_apollo_batch_status():
    """Polls real-time worker progress and credit expenditures."""
    from apollo_enricher import apollo_worker
    return jsonify(apollo_worker.get_status())


@app.route("/api/apollo/stop-batch", methods=["POST"])
def api_apollo_stop_batch():
    """Sends graceful stop signal to active Apollo queue worker."""
    from apollo_enricher import apollo_worker
    stopped = apollo_worker.stop()
    if stopped:
        return jsonify({"status": "stopping", "message": "Stop signal sent to Apollo worker."})
    else:
        return jsonify({"status": "not_running", "message": "No active Apollo enrichment queue is running."})


@app.route("/api/apollo/credit-log", methods=["GET"])
def api_apollo_download_credit_log():
    from credit_tracker import CSV_PATH
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, "r", encoding="utf-8") as f:
            data = f.read()
        return Response(
            data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=apollo_credit_usage.csv"}
        )
    return jsonify({"error": "No log generated yet"}), 404


@app.route("/api/apollo/set-key", methods=["POST"])
def api_apollo_set_key():
    payload = request.get_json() or {}
    key = payload.get("api_key", "").strip()
    if not key:
        return jsonify({"error": "Empty API key provided"}), 400
    os.environ["APOLLO_API_KEY"] = key
    return jsonify({"status": "ok", "message": "Apollo API key successfully saved!"})


@app.route("/api/apollo/enrich-batch", methods=["POST"])
def api_apollo_enrich_batch():
    from apollo_enricher import ApolloEnricher
    payload = request.get_json() or {}
    batch_size = int(payload.get("batch_size", 5))
    credit_cap = int(payload.get("credit_cap", 20))

    enricher = ApolloEnricher()
    res = enricher.enrich_company_batch(batch_size=batch_size, credit_cap=credit_cap)
    return jsonify(res)


# ==============================================================================
# ON-DEMAND CANDIDATE ENRICHMENT & DIRECTORY ENDPOINTS
# ==============================================================================

@app.route("/api/apollo/candidates", methods=["GET"])
def api_apollo_get_candidates():
    """Retrieve discovered P1/P2 candidates with filtering and pagination."""
    import apollo_cache
    persona = request.args.get("persona")
    unlocked_param = request.args.get("unlocked")
    is_unlocked = None
    if unlocked_param == "true" or unlocked_param == "1":
        is_unlocked = True
    elif unlocked_param == "false" or unlocked_param == "0":
        is_unlocked = False

    search = request.args.get("search", "").strip()
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))

    data = apollo_cache.get_all_candidates_list(
        persona=persona,
        is_unlocked=is_unlocked,
        search=search,
        limit=limit,
        offset=offset
    )
    return jsonify(data)


@app.route("/api/apollo/discover-candidates", methods=["POST"])
def api_apollo_discover_candidates():
    """Trigger 0-credit discovery of all P1 & P2 candidates across company domains."""
    from apollo_enricher import ApolloEnricher
    payload = request.get_json() or {}
    max_companies = payload.get("max_companies")
    if max_companies:
        max_companies = int(max_companies)

    enricher = ApolloEnricher()
    res = enricher.discover_p1_p2_candidates(max_companies=max_companies)
    return jsonify(res)


@app.route("/api/apollo/unlock-candidate", methods=["POST"])
def api_apollo_unlock_candidate():
    """On-demand 1-click unlock for a specific candidate (costs 1 credit)."""
    from apollo_enricher import ApolloEnricher
    payload = request.get_json() or {}
    apollo_id = payload.get("apollo_id", "").strip()
    domain = payload.get("domain", "").strip()

    if not apollo_id:
        return jsonify({"error": "Missing apollo_id"}), 400

    enricher = ApolloEnricher()
    res = enricher.unlock_candidate_on_demand(apollo_id=apollo_id, domain=domain)
    if "error" in res:
        return jsonify(res), 422
    return jsonify(res)


@app.route("/api/apollo/download-candidates-csv", methods=["GET"])
def api_apollo_download_candidates_csv():
    """Download the discovered candidates spreadsheet as CSV."""
    from apollo_enricher import CANDIDATES_CSV_PATH, ApolloEnricher
    if not os.path.exists(CANDIDATES_CSV_PATH):
        # Generate on the fly
        ApolloEnricher().export_candidates_files()

    if os.path.exists(CANDIDATES_CSV_PATH):
        with open(CANDIDATES_CSV_PATH, "r", encoding="utf-8") as f:
            data = f.read()
        return Response(
            data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=apollo_p1_p2_candidates.csv"}
        )
    return jsonify({"error": "No candidates spreadsheet generated yet"}), 404


@app.route("/api/apollo/download-candidates-json", methods=["GET"])
def api_apollo_download_candidates_json():
    """Download the discovered candidates directory as JSON."""
    from apollo_enricher import CANDIDATES_JSON_PATH, ApolloEnricher
    if not os.path.exists(CANDIDATES_JSON_PATH):
        ApolloEnricher().export_candidates_files()

    if os.path.exists(CANDIDATES_JSON_PATH):
        with open(CANDIDATES_JSON_PATH, "r", encoding="utf-8") as f:
            data = f.read()
        return Response(
            data,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=apollo_p1_p2_candidates.json"}
        )
    return jsonify({"error": "No candidates JSON generated yet"}), 404


@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Bad request", "message": str(e)}), 400


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Resource not found"}), 404
    return render_template("index.html"), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error", "message": "An unexpected error occurred."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5005))
    print(f"[*] Cvent Intelligence Engine running on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
