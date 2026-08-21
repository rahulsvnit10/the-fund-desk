"""Offline Langfuse experiment for the Fund Desk agent — set_weights capability.

Runs the real agent over a small dataset of natural-language weighting requests,
scores each with the weight_intent evaluator, and uploads the run (traces + scores)
to Langfuse.

Needs, in ../.env:  ANTHROPIC_API_KEY  +  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST

    ./.venv/bin/python evals/run_evals.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# load ../.env so both the agent (Anthropic) and Langfuse get their keys
envf = ROOT / ".env"
if envf.exists():
    import os
    for line in envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import engine          # noqa: E402
import agent           # noqa: E402
from langfuse import get_client  # noqa: E402
from evaluators import weight_intent, numbers_faithful, llm_faithful  # noqa: E402

# rank the cached universe once (weights here don't affect set_weights answers)
FUNDS = json.loads((ROOT / "data" / "cache.json").read_text())["funds"]
RANKED = engine.rank([dict(f) for f in FUNDS], {k: 1 for k in engine.METRIC_KEYS})

# dataset: a sentence -> which metrics should end up weighted high vs low
DATA = [
    {"input": "I want safety and low fees, with a little growth",
     "expected_output": {"high": ["stdDev", "downCap", "ter"], "low": ["upCap"]}},
    {"input": "Maximize growth, I don't care about fees or volatility",
     "expected_output": {"high": ["rr", "upCap"], "low": ["ter", "stdDev"]}},
    {"input": "Lowest cost funds, nothing else really matters",
     "expected_output": {"high": ["ter"], "low": ["rr", "aum"]}},
    {"input": "Best risk-adjusted returns above all",
     "expected_output": {"high": ["sharpe"], "low": []}},
    {"input": "Steady, low-volatility funds that don't crash",
     "expected_output": {"high": ["stdDev", "downCap"], "low": ["upCap"]}},
    {"input": "Large, well-established funds with low expense ratios",
     "expected_output": {"high": ["aum", "ter"], "low": []}},
    {"input": "I want safety and low fees, with a high growth",
     "expected_output": {"high": ["rr", "upCap", "stdDev", "downCap", "ter"], "low": ["aum"]}},
]


DATASET_NAME = "set-weights"


def _name_trace(label):
    try:
        get_client().update_current_trace(name=label)
    except Exception:  # noqa: BLE001
        pass


def task(*, item, **kwargs):
    # dataset-run items expose .input / .expected_output as attributes
    sentence = item.input if hasattr(item, "input") else item["input"]
    _name_trace(f"set_weights: {sentence[:50]}")
    res = agent.handle(sentence, RANKED)
    action = res.get("action") or {}
    return {"weights": action.get("weights"), "reply": res.get("reply")}


def ensure_dataset(lf):
    """Create the dataset + items (idempotent — stable ids upsert on re-run)."""
    lf.create_dataset(name=DATASET_NAME,
                      description="NL preference sentences -> expected weight intent")
    for i, row in enumerate(DATA):
        lf.create_dataset_item(dataset_name=DATASET_NAME, id=f"sw-{i}",
                               input=row["input"], expected_output=row["expected_output"])


# ---- explain_fund faithfulness ------------------------------------------------
EXPL_DATASET = "fund-explanations"
_SNAP = ("name", "category", "rank", "catRank", "catSize", "score",
         "rr", "sharpe", "stdDev", "upCap", "downCap", "aum", "ter")


_EQUITY_PRIO = ["large cap", "flexi", "mid cap", "small cap", "elss", "multi cap", "value", "focused"]


def _is_equity(cat):
    return any(k in (cat or "").lower() for k in ("cap", "flexi", "multi", "elss", "value", "focused"))


def _eq_priority(fund):
    c = (fund.get("category") or "").lower()
    for i, p in enumerate(_EQUITY_PRIO):
        if p in c and not (p == "mid cap" and "large" in c):  # "mid cap" must not match "large & mid cap"
            return i
    return 99


def _pick_explain_funds(n_equity=6, n_other=2):
    """The #1 fund in each category (with full data): a spread of equity categories
    plus a couple of debt/hybrid for contrast — so faithfulness generalises."""
    best = {}
    for f in RANKED:
        c = f.get("category")
        if not c or f.get("upCap") is None:
            continue
        if c not in best or f.get("catRank", 99) < best[c].get("catRank", 99):
            best[c] = f
    reps = list(best.values())
    equity = sorted([f for f in reps if _is_equity(f["category"])], key=_eq_priority)[:n_equity]
    other = [f for f in reps if not _is_equity(f["category"])][:n_other]
    return [{k: f.get(k) for k in _SNAP} for f in (equity + other)]


def explain_task(*, item, **kwargs):
    snap = item.input if hasattr(item, "input") else item["input"]
    _name_trace(f"explain: {snap['name']}")
    # Ask for a data-grounded description, NOT "why it ranks" — the agent has no ranking
    # methodology, so a "why" question forces unsupported claims (an unfair test).
    res = agent.handle(f"Describe {snap['name']}'s strengths and weaknesses based only on "
                       f"its metrics. State the numbers; don't guess why it ranks.", RANKED)
    # pass the agent's grounding through so faithfulness is judged against what it actually saw
    return {"reply": res.get("reply"), "grounding": res.get("grounding")}


def ensure_explain_dataset(lf):
    lf.create_dataset(name=EXPL_DATASET, description="Fund explanations graded for faithfulness")
    for i, snap in enumerate(_pick_explain_funds()):
        # input carries the fund name; expected_output carries the real metrics (ground truth)
        lf.create_dataset_item(dataset_name=EXPL_DATASET, id=f"ex-{i}",
                               input={"name": snap["name"]}, expected_output=snap)


if __name__ == "__main__":
    import sys as _sys
    which = _sys.argv[1] if len(_sys.argv) > 1 else "all"   # weights | explain | all
    lf = get_client()

    if which in ("weights", "all"):
        ensure_dataset(lf)
        r1 = lf.get_dataset(DATASET_NAME).run_experiment(
            name="set_weights",
            description="NL preference -> the 7 ranking weights (directional intent check)",
            task=task, evaluators=[weight_intent])
        print(r1.format())

    if which in ("explain", "all"):
        ensure_explain_dataset(lf)
        # rebuild expected_output-aware task: evaluators need the fund metrics, which
        # the runner passes as expected_output from each dataset item automatically.
        r2 = lf.get_dataset(EXPL_DATASET).run_experiment(
            name="explain_faithfulness",
            description="Fund explanations checked for invented numbers + unsupported claims",
            task=explain_task, evaluators=[numbers_faithful, llm_faithful])
        print(r2.format())

    lf.flush()
