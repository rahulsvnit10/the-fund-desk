"""Online eval: grade live Ask Fund Agent chats (reference-free faithfulness).

Pulls recent production traces tagged 'live-chat' from Langfuse, runs the two
faithfulness checks against the grounding the agent actually used, and attaches
the scores back to each trace. Skips chats already graded, so it's safe to re-run
(and cheap to schedule). Reference-free — no golden answer needed for live traffic.

    ./.venv/bin/python evals/score_live.py            # grade all new live chats
    ./.venv/bin/python evals/score_live.py 100        # look back over the last 100 traces
"""
import os
import sys
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

lf = get_client()


def _reply_and_grounding(full):
    md = full.metadata or {}
    out = full.output
    reply = out if isinstance(out, str) else (out.get("reply") if isinstance(out, dict) else str(out))
    return {"reply": reply or "", "grounding": md.get("grounding")}


def score_one(trace):
    full = lf.api.trace.get(trace.id)
    if "llm_faithful" in {s.name for s in (full.scores or [])}:
        return None                        # already graded — don't re-spend
    output = _reply_and_grounding(full)
    results = {}
    for fn in (numbers_faithful, llm_faithful):     # reference-free: expected_output empty
        ev = fn(input=full.input, output=output, expected_output={})
        lf.create_score(name=ev.name, trace_id=trace.id, data_type="NUMERIC",
                        value=float(ev.value) if ev.value is not None else 0.0,
                        comment=(ev.comment or "")[:400])
        results[ev.name] = ev.value
    return results


if __name__ == "__main__":
    look_back = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    traces = [t for t in lf.api.trace.list(limit=look_back).data if "live-chat" in (t.tags or [])]
    graded = 0
    for t in traces:
        r = score_one(t)
        if r is not None:
            graded += 1
            print(f"  {(t.name or '')[:55]:55s} numbers={r.get('numbers_faithful')} llm={r.get('llm_faithful')}")
    lf.flush()
    print(f"\nGraded {graded} new live chat(s); {len(traces) - graded} already scored.")
