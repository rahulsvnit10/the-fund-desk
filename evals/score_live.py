"""Online eval: grade live Ask Fund Agent chats (reference-free faithfulness).

Production records each chat as an OpenTelemetry span named "live-chat" (see
app._trace_live_chat). This reads recent ones through Langfuse's v2 observations
API, runs the two faithfulness checks against the grounding the agent actually
used, and attaches the scores to each trace. Chats already graded are skipped
(v3 scores API), so it's safe and cheap to re-run.

Langfuse removed its v1 list/ingestion APIs on 2026-11-16; this uses only the
supported v2 observations, v3 scores, and score-create paths.

    ./.venv/bin/python evals/score_live.py        # grade chats from the last 3 days
    ./.venv/bin/python evals/score_live.py 7      # look back 7 days
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "evals"))

envf = ROOT / ".env"
if envf.exists():
    for line in envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from langfuse import get_client               # noqa: E402
from evaluators import numbers_faithful, llm_faithful  # noqa: E402

HOST = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
_auth = base64.b64encode(
    f'{os.environ["LANGFUSE_PUBLIC_KEY"]}:{os.environ["LANGFUSE_SECRET_KEY"]}'.encode()).decode()
HEADERS = {"Authorization": f"Basic {_auth}"}


def _get(path, params):
    url = HOST + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
        return json.loads(r.read().decode())


def live_chats(days):
    """Every live-chat span from the last `days` days (paginated, newest first)."""
    end = datetime.now(timezone.utc) + timedelta(minutes=5)
    params = {
        "fromStartTime": (end - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toStartTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "name": "live-chat",
        "fields": "core,basic,io,metadata,trace_context",
        "expandMetadata": "grounding",
        "limit": "1000",
    }
    rows = []
    while True:
        d = _get("/api/public/v2/observations", params)
        rows += d.get("data", [])
        cursor = (d.get("meta") or {}).get("cursor")
        if not cursor:
            return rows
        params["cursor"] = cursor


def already_graded(trace_ids):
    """Trace ids that already carry an llm_faithful score — don't re-spend on them."""
    done = set()
    for i in range(0, len(trace_ids), 50):
        params = {"traceId": ",".join(trace_ids[i:i + 50]), "fields": "subject", "limit": "100"}  # v3 max
        while True:
            d = _get("/api/public/v3/scores", params)
            for s in d.get("data", []):
                if s.get("name") == "llm_faithful":
                    done.add((s.get("subject") or {}).get("id"))
            cursor = (d.get("meta") or {}).get("cursor")
            if not cursor:
                break
            params["cursor"] = cursor
    return done


def _grounding(meta):
    """Metadata may arrive as a dict or a JSON string; grounding inside it likewise."""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            return None
    g = (meta or {}).get("grounding")
    if isinstance(g, str):
        try:
            g = json.loads(g)
        except json.JSONDecodeError:
            return None
    return g


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    chats = live_chats(days)
    done = already_graded([c["traceId"] for c in chats])
    lf = get_client()
    graded = 0
    for c in chats:
        if c["traceId"] in done:
            continue
        output = {"reply": c.get("output") or "", "grounding": _grounding(c.get("metadata"))}
        results = {}
        for fn in (numbers_faithful, llm_faithful):     # reference-free: no expected_output
            ev = fn(input=c.get("input"), output=output, expected_output={})
            lf.create_score(name=ev.name, trace_id=c["traceId"], data_type="NUMERIC",
                            value=float(ev.value) if ev.value is not None else 0.0,
                            comment=(ev.comment or "")[:400])
            results[ev.name] = ev.value
        graded += 1
        print(f"  {(c.get('traceName') or '')[:55]:55s} numbers={results.get('numbers_faithful')} "
              f"llm={results.get('llm_faithful')}")
    lf.flush()
    print(f"\nGraded {graded} new live chat(s); {len(chats) - graded} already scored "
          f"(last {days} day(s), {len(chats)} found).")
