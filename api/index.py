"""Vercel serverless entrypoint (ASGI).

Vercel routes every request here (see vercel.json rewrites). We add the backend to
the path, flag serverless mode (no background scrape), and expose the FastAPI app.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("FUND_DESK_SERVERLESS", "1")

from app import app  # noqa: E402  (FastAPI ASGI app)
