"""Headless full rebuild of data/cache.json.

Runs the same scrape as the app's Refresh button (sitemap -> concurrent enrich ->
dedup -> capture ratios), then writes the snapshot. Used by the daily GitHub Actions
cron. Needs NO API key — scraping only, the agent isn't involved.

    python scripts/build_cache.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import app  # noqa: E402  (importing runs no server; build() does the work)

if __name__ == "__main__":
    app.build()
    if app.STATE.get("error"):
        print("Build error:", app.STATE["error"], file=sys.stderr)
        sys.exit(1)
    print("Rebuilt cache.json with", len(app.STATE["funds"]), "funds.")
