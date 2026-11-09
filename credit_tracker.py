"""
Persistent Credit Tracker & Audit Logger for Apollo Lead Enrichment.
Maintains real-time audit files:
  1. apollo_credit_usage.json  (Structured JSON state)
  2. apollo_credit_usage.csv   (Human-readable spreadsheet audit log)
"""

import os
import csv
import json
from datetime import datetime
from typing import Dict, Any, Optional

JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apollo_credit_usage.json")
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apollo_credit_usage.csv")


class CreditTracker:
    """Thread-safe persistent usage and credit tracker."""

    def __init__(self):
        self._ensure_files()

    def _ensure_files(self):
        # 1. Ensure JSON file exists
        if not os.path.exists(JSON_PATH):
            initial_state = {
                "total_credits_spent": 0,
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "breakdown": {
                    "match_creator": 0,
                    "enrich_organization": 0,
                    "search_candidates": 0,
                    "unlock_work_email": 0
                },
                "history": []
            }
            with open(JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(initial_state, f, indent=2)

        # 2. Ensure CSV file exists with headers
        if not os.path.exists(CSV_PATH):
            with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Timestamp", "Event ID", "Event Name", "Action",
                    "Endpoint", "Target", "Credits Spent", "Cumulative Total"
                ])

    def get_summary(self) -> Dict[str, Any]:
        """Read current credit usage summary."""
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {
                "total_credits_spent": 0,
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "breakdown": {},
                "history": []
            }

    def record_usage(
        self,
        event_id: Any,
        event_name: str,
        action: str,
        endpoint: str,
        target: str,
        credits: int = 1
    ) -> int:
        """
        Record a credit expenditure, append to CSV audit log, update JSON,
        and return the new cumulative total.
        """
        timestamp = datetime.utcnow().isoformat() + "Z"
        data = self.get_summary()

        data["total_credits_spent"] = data.get("total_credits_spent", 0) + credits
        data["last_updated"] = timestamp

        breakdown = data.get("breakdown", {})
        breakdown[action] = breakdown.get(action, 0) + credits
        data["breakdown"] = breakdown

        entry = {
            "timestamp": timestamp,
            "event_id": event_id,
            "event_name": event_name,
            "action": action,
            "endpoint": endpoint,
            "target": target,
            "credits": credits,
            "cumulative_total": data["total_credits_spent"]
        }

        # Keep last 1000 history entries in JSON to prevent bloat
        history = data.get("history", [])
        history.append(entry)
        if len(history) > 1000:
            history = history[-1000:]
        data["history"] = history

        # Update JSON
        try:
            with open(JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[CreditTracker] Failed to write JSON: {e}")

        # Append to CSV audit log
        try:
            with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp,
                    event_id,
                    event_name,
                    action,
                    endpoint,
                    target,
                    credits,
                    data["total_credits_spent"]
                ])
        except Exception as e:
            print(f"[CreditTracker] Failed to write CSV: {e}")

        return data["total_credits_spent"]


# Global tracker instance
credit_tracker = CreditTracker()
