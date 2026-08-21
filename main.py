"""Vercel entrypoint — exposes the FastAPI `app` for the native Python runtime.

backend/app.py uses sibling imports (`import engine`, `import agent`), so backend/
must be on sys.path before we import it. app.py auto-detects serverless via VERCEL=1.

If the import fails, we fall back to a tiny raw-ASGI app that returns the traceback
as plain text — so an otherwise-opaque FUNCTION_INVOCATION_FAILED becomes readable.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                       # so 'backend' package resolves
sys.path.insert(0, os.path.join(_HERE, "backend"))  # so app.py's 'import engine' works
os.environ.setdefault("FUND_DESK_SERVERLESS", "1")

try:
    from app import app  # noqa: E402  (FastAPI instance from backend/app.py)
except Exception:  # noqa: BLE001
    _TB = "IMPORT FAILED\n\n" + traceback.format_exc()
    _TB += "\n\nsys.path:\n" + "\n".join(sys.path)
    try:
        _TB += "\n\nfiles at root: " + ", ".join(sorted(os.listdir(_HERE)))
        _TB += "\nfiles in backend: " + ", ".join(sorted(os.listdir(os.path.join(_HERE, "backend"))))
    except Exception as _e:  # noqa: BLE001
        _TB += "\n(listdir failed: %s)" % _e

    async def app(scope, receive, send):  # noqa: F811  (raw ASGI fallback, no deps)
        if scope["type"] != "http":
            return
        body = _TB.encode()
        await send({"type": "http.response.start", "status": 500,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": body})
