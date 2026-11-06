"""
Quick launcher for the Cvent Intelligence Engine.
"""

import sys
import os

if __name__ == "__main__":
    from app import app
    port = int(os.environ.get("PORT", 5005))
    print("=" * 65)
    print("CVENT INTELLIGENCE ENGINE -- DEDICATED PIPELINE")
    print("=" * 65)
    print(f"Local Dashboard URL: http://127.0.0.1:{port}")
    print(f"Isolated Database:   Neon PostgreSQL (cvent, cvent_companies)")
    print("=" * 65)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)

