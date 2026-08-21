"""
The Fund Desk - local API server.

Endpoints
  GET  /                -> the dashboard UI
  GET  /api/funds       -> tracked universe with raw metrics + last-updated
  GET  /api/lookup?q=   -> resolve any typed/pasted fund name, fetch + rank it
  POST /api/refresh     -> rebuild the universe now (runs in background)

Ranking is done in the browser from these raw metrics so weight tweaks are
instant. Data is cached to data/cache.json and auto-refreshed every 24h.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

import engine

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "cache.json"
FRONTEND = ROOT / "frontend" / "index.html"
DATA.parent.mkdir(exist_ok=True)

# Load a local .env (KEY=VALUE per line) so ANTHROPIC_API_KEY can live in a file
# instead of the shell environment. Env vars already set win over the file.
_envfile = ROOT / ".env"
if _envfile.exists():
    for _line in _envfile.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

MAX_FUNDS = int(os.environ.get("FUND_DESK_MAX", "0"))   # 0 = whole universe (~1500)
REFRESH_SECS = 24 * 3600
# Serverless hosts (Vercel) can't run background threads or the scrape. In that mode we
# just serve the committed cache.json snapshot; the agent still works (per-request API call).
# Vercel sets VERCEL=1 in the deployment runtime, so this auto-enables there.
SERVERLESS = os.environ.get("FUND_DESK_SERVERLESS") == "1" or bool(os.environ.get("VERCEL"))

STATE = {"updated": None, "funds": [], "directory": [], "building": False,
         "error": None, "count": 0, "target": 0}
_bench_cache = {}   # category -> monthly-return series (for on-demand lookups)
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc).isoformat()


def load_cache():
    if DATA.exists():
        try:
            d = json.loads(DATA.read_text())
            STATE.update({k: d.get(k) for k in ("updated", "funds", "directory") if k in d})
            STATE["count"] = len(STATE["funds"])
        except Exception as e:  # noqa: BLE001
            STATE["error"] = f"cache load: {e}"


def save_cache():
    DATA.write_text(json.dumps({"updated": STATE["updated"], "funds": STATE["funds"],
                                "directory": STATE["directory"]}, indent=1))


def _benchmark_returns(label):
    """Monthly-return series for a category's benchmark index fund (cached)."""
    if label in _bench_cache:
        return _bench_cache[label]
    code = engine.BENCHMARK_CODE.get(label)
    series = None
    try:
        series = engine.monthly_returns(code) if code else None
    except Exception:  # noqa: BLE001
        series = None
    _bench_cache[label] = series
    return series


def enrich(slug, bench_by_code):
    """Fetch one fund's Tickertape metrics and compute its capture ratios."""
    rec = engine.parse_fund(slug)
    up = dn = None
    code = engine.scheme_for(rec)
    bench = bench_by_code.get(engine.benchmark_for_category(rec.get("category")))
    if code and bench:
        try:
            up, dn = engine.capture_from_series(engine.monthly_returns(code), bench)
        except Exception:  # noqa: BLE001
            pass
    rec["upCap"], rec["downCap"], rec["scheme"] = up, dn, code
    return rec


def build():
    """Fetch the whole universe (sitemap) concurrently, rank-ready. Threaded."""
    with _lock:
        if STATE["building"]:
            return
        STATE["building"] = True
        STATE["error"] = None
        STATE["count"] = 0
    try:
        engine.amfi_map()  # warm the ISIN->scheme map once
        slugs = engine.sitemap_slugs()
        if MAX_FUNDS:
            slugs = slugs[:MAX_FUNDS]
        STATE["target"] = len(slugs)

        # precompute the handful of benchmark series once
        bench_by_code = {}
        for c in {engine._NIFTY50, engine._NIFTY500, engine._MIDCAP150, engine._SMALLCAP250,
                  engine._GSEC, engine._SHORTDEBT, engine._GOLD}:
            try:
                bench_by_code[c] = engine.monthly_returns(c)
            except Exception:  # noqa: BLE001
                bench_by_code[c] = None

        results = []
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(enrich, s, bench_by_code) for s in slugs]
            for fut in as_completed(futs):
                try:
                    rec = fut.result()
                    if rec and rec.get("name"):
                        results.append(rec)
                except Exception:  # noqa: BLE001
                    pass
                STATE["count"] = len(results)

        # dedupe to one entry per fund, keeping the Direct plan (lowest TER)
        best = {}
        for r in results:
            key = engine._norm(r["name"])
            cur = best.get(key)
            if cur is None or (r.get("ter") is not None and
                               (cur.get("ter") is None or r["ter"] < cur["ter"])):
                best[key] = r
        funds = list(best.values())

        STATE["funds"] = funds
        STATE["count"] = len(funds)
        STATE["directory"] = []          # the universe itself is now the lookup directory
        STATE["updated"] = _now()
        save_cache()
    except Exception as e:  # noqa: BLE001
        STATE["error"] = str(e)
    finally:
        STATE["building"] = False


def start_build_if_needed():
    stale = True
    if STATE["updated"]:
        try:
            age = time.time() - datetime.fromisoformat(STATE["updated"]).timestamp()
            stale = age > REFRESH_SECS
        except Exception:  # noqa: BLE001
            stale = True
    if (stale or not STATE["funds"]) and not STATE["building"]:
        threading.Thread(target=build, daemon=True).start()


def _scheduler():
    while True:
        time.sleep(3600)
        start_build_if_needed()


# --------------------------------------------------------------------------- #
app = FastAPI(title="The Fund Desk")

# Serverless cold starts may not fire ASGI startup events — eager-load the snapshot on import.
if SERVERLESS and not STATE["funds"]:
    load_cache()


@app.on_event("startup")
def _startup():
    load_cache()
    if SERVERLESS:
        return   # no background scrape/scheduler on serverless — serve the shipped snapshot
    start_build_if_needed()
    threading.Thread(target=_scheduler, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(FRONTEND)


@app.get("/api/funds")
def api_funds():
    return {
        "updated": STATE["updated"],
        "building": STATE["building"],
        "count": STATE["count"],
        "target": STATE["target"],
        "error": STATE["error"],
        "metrics": engine.METRICS,
        "universe": len(STATE["funds"]),
        "funds": STATE["funds"],
    }


@app.get("/api/lookup")
def api_lookup(q: str):
    """Match a typed/pasted name against the full universe and return that fund.

    Every fund is already in the universe, so the browser just highlights it and
    shows its rank (even when that rank sits outside the displayed top 100).
    """
    match = engine.best_directory_match(q, STATE["funds"])
    if not match:
        return JSONResponse(
            {"error": f'Couldn’t find a fund matching "{q}". Try its fuller name.'},
            status_code=404)
    return {"fund": {**match, "inUniverse": True}}


@app.post("/api/refresh")
def api_refresh():
    if SERVERLESS:
        return {"building": False, "updated": STATE["updated"], "disabled": True,
                "message": "Live refresh is off on the hosted demo — showing the last snapshot."}
    start_build_if_needed()
    return {"building": STATE["building"], "updated": STATE["updated"]}


@app.post("/api/agent")
def api_agent(body: dict):
    """Ask Fund Desk — grounded, tool-using chat over the live universe."""
    message = (body or {}).get("message", "").strip()
    if not message:
        return JSONResponse({"error": "Empty message."}, status_code=400)
    if not STATE["funds"]:
        return {"reply": "The fund universe is still building — give it a moment and try again."}
    weights = (body or {}).get("weights") or None
    import agent  # imported lazily so the app runs even without the anthropic package
    try:
        ranked = engine.rank([dict(f) for f in STATE["funds"]], weights)
        return agent.handle(message, ranked)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if isinstance(e, agent.anthropic.AuthenticationError) or "authentication" in msg or "api_key" in msg:
            return {"reply": "The agent needs an Anthropic API key. Set ANTHROPIC_API_KEY in the server's "
                             "environment (or run `ant auth login`) and restart, then try again."}
        return {"reply": f"Agent error: {e}"}
