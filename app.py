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
from flask import Flask, render_template, request, jsonify, Response

import cvent_db
import cvent_engine

app = Flask(__name__)
app.secret_key = "cvent-secret-key-isolated-engine"
app.config["TEMPLATES_AUTO_RELOAD"] = True


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
    is_creator = request.args.get("is_creator", "all")
    contact_type = request.args.get("contact_type", "all")
    event_id = request.args.get("event_id")
    if event_id:
        try:
            event_id = int(event_id)
        except ValueError:
            event_id = None

    data = cvent_db.get_all_cvent_people(
        limit=limit,
        offset=offset,
        search=search,
        is_creator_filter=is_creator,
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
    time_filter = data.get("time_filter", "qdr:m3")
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

    is_cvent = "cvent.com" in netloc or "/event/" in parsed.path.lower()
    if not is_cvent:
        return jsonify({"status": "error", "message": "Target must be a valid Cvent event registration URL."}), 400

    record = cvent_engine.process_and_save_event(url)
    if record:
        return jsonify({"status": "ok", "record": record})
    else:
        return jsonify({"status": "error", "message": "Failed to scrape or extract valid metadata from the given URL."}), 422


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
