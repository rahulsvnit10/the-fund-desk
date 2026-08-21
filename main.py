"""Vercel entrypoint — exposes the FastAPI `app` for the native Python runtime.

backend/app.py uses sibling imports (`import engine`, `import agent`), so backend/
must be on sys.path before we import it. app.py auto-detects serverless via VERCEL=1.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
os.environ.setdefault("FUND_DESK_SERVERLESS", "1")

from app import app  # noqa: E402  (FastAPI instance from backend/app.py)
