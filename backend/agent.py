"""
The Fund Desk agent — "Ask Fund Desk".

A small tool-using agent (Claude / Anthropic SDK) with four capabilities:
  1. set_weights        — natural language -> the seven ranking weights (drives the UI)
  2. get_fund           — one fund's real metrics + rank, for grounded explanations
  3. get_category_stats — category aggregates, for grounded summaries
  4. compare_funds      — two funds side by side, for balanced comparisons

Every answer is grounded in tool results built from the live dataset, so the model
can only cite numbers the backend actually returned. Each capability is a distinct,
evaluable task (faithfulness / weight-vector distance) — the basis for evals later.

Needs ANTHROPIC_API_KEY in the environment (or an `ant auth login` profile).
"""

import json
import re

import anthropic
import engine

MODEL = "claude-haiku-4-5"   # cheapest tier; ~$0.008 per question
MAX_STEPS = 5

TOOLS = [
    {
        "name": "set_weights",
        "description": ("Set the ranking weights when the user describes what they value "
                        "(e.g. safety, low fees, steady growth). Give each of the seven metrics "
                        "a relative weight from 0 to 3. Higher = matters more; 0 = ignore it."),
        "input_schema": {
            "type": "object",
            "properties": {
                "rr": {"type": "number", "description": "Rolling 3Y return (higher is better)"},
                "sharpe": {"type": "number", "description": "Sharpe ratio (higher is better)"},
                "stdDev": {"type": "number", "description": "Std deviation / volatility (lower is better)"},
                "upCap": {"type": "number", "description": "Upside capture (higher is better)"},
                "downCap": {"type": "number", "description": "Downside capture (lower is better)"},
                "aum": {"type": "number", "description": "Assets under management (higher is better)"},
                "ter": {"type": "number", "description": "Total expense ratio / cost (lower is better)"},
                "rationale": {"type": "string", "description": "One short sentence on the weighting"},
            },
            "required": ["rr", "sharpe", "stdDev", "upCap", "downCap", "aum", "ter"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_fund",
        "description": "Look up one fund's real metrics, rank, and category rank. Call this before explaining or discussing any specific fund.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                         "required": ["name"], "additionalProperties": False},
    },
    {
        "name": "get_category_stats",
        "description": "Aggregate statistics for a fund category (e.g. 'Mid Cap'). Call this before summarizing a category.",
        "input_schema": {"type": "object", "properties": {"category": {"type": "string"}},
                         "required": ["category"], "additionalProperties": False},
    },
    {
        "name": "compare_funds",
        "description": "Get two funds' metrics side by side. Call this before comparing two funds.",
        "input_schema": {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                         "required": ["a", "b"], "additionalProperties": False},
    },
]

SYSTEM = """You are the Fund Desk agent, embedded in a mutual-fund screener for Indian funds.

Metrics per fund (direction in parentheses):
- Rolling 3Y Return % (higher better)
- Sharpe ratio (higher better)
- Std Deviation % (lower better — steadier)
- Upside Capture % (higher better — grabs more of the market's gains)
- Downside Capture % (lower better — loses less in downturns)
- AUM in ₹ crore (higher better — scale/trust)
- TER % (lower better — cost)

Hard rules:
- Only state numbers that appear in a tool result. Never invent or estimate a figure. If a metric is "—" (missing), say it's not available rather than guessing.
- No comparative or superlative claims unless a tool result actually contains the data to back the comparison. Words like "lowest", "cheapest", "highest", "best", "top", "beats the market", "one of the strongest" claim you compared against other funds — you usually only have THIS fund's numbers. State the fund's own figure instead (e.g. "its TER is 0.45%", not "it's the lowest-cost fund"). Rank and category rank ARE in the tool result, so you may state those exactly (e.g. "ranks #3 of 42 in its category").
- State what the numbers say; do not interpret what they imply. Don't read AUM as "trust", "confidence", or "maturity". Don't label a metric "low", "high", "reasonable", "typical", "exceptional", or "modest" unless a tool result gives you comparison data (e.g. a category average). Don't describe or guess how the ranking or composite score is calculated — you don't have the methodology. Only compare a fund to its category if you actually called get_category_stats for it.
  Examples — ALLOWED: "Its TER is 0.10%." / "Its 3Y return of 18% is above the category average of 15%." (only if you fetched that average). NOT ALLOWED: "Its low cost and scale drive its #1 rank" (guesses the formula) / "Large AUM signals investor confidence" (interprets a number) / "Volatility is within the typical range" (no comparison data).
- Use the fund's real name and its real numbers. Be concise and specific.
- Educational only. Never tell the user to buy, sell, or hold, and never give personalized investment advice.
- When the user describes what they care about, call set_weights with relative 0–3 weights: raise the metrics they value, lower the rest, keep unmentioned ones moderate (around 1). For "safety" raise low-volatility and downside protection (Std Dev, Downside Capture) and lower TER; for "growth" raise Rolling Return and Upside Capture.
- For a comparison, be balanced: give each fund its genuine strengths and weaknesses from the numbers."""


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _fund_view(f):
    """Compact, tool-result-safe view of a fund's real data."""
    return {
        "name": f.get("name"), "category": f.get("category"),
        "overall_rank": f.get("rank"), "category_rank": f.get("catRank"),
        "composite_score": f.get("score"),
        "rolling_3y_return_pct": _num(f.get("rr")),
        "sharpe": _num(f.get("sharpe")),
        "std_deviation_pct": _num(f.get("stdDev")),
        "upside_capture_pct": _num(f.get("upCap")),
        "downside_capture_pct": _num(f.get("downCap")),
        "aum_cr": _num(f.get("aum")),
        "ter_pct": _num(f.get("ter")),
    }


def _find(name, ranked):
    m = engine.best_directory_match(name, [{"name": f.get("name") or "", "_f": f} for f in ranked])
    return m["_f"] if m else None


def _cat_norm(s):
    s = (s or "").lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]", " ", s).replace(" fund", " ")
    return " ".join(s.split())


def _category_stats(cat, ranked):
    import statistics
    q = _cat_norm(cat)
    # exact match first, then prefix, then substring — so "mid cap" does NOT
    # swallow "large & mid cap".
    exact = [f for f in ranked if _cat_norm(f.get("category")) == q]
    pref = [f for f in ranked if _cat_norm(f.get("category")).startswith(q)]
    contains = [f for f in ranked if q and q in _cat_norm(f.get("category"))]
    funds = exact or pref or contains
    if not funds:
        return {"error": f'No funds found in category "{cat}".'}
    def agg(k):
        xs = [f[k] for f in funds if isinstance(f.get(k), (int, float))]
        return {"avg": round(statistics.mean(xs), 2), "median": round(statistics.median(xs), 2),
                "count": len(xs)} if xs else None
    top = min(funds, key=lambda f: f.get("catRank", 1e9))
    return {
        "category": funds[0]["category"], "fund_count": len(funds),
        "top_ranked": top.get("name"),
        "rolling_3y_return_pct": agg("rr"), "sharpe": agg("sharpe"),
        "std_deviation_pct": agg("stdDev"), "upside_capture_pct": agg("upCap"),
        "downside_capture_pct": agg("downCap"), "aum_cr": agg("aum"), "ter_pct": agg("ter"),
    }


def handle(message, ranked, history=None):
    """Run one agent turn. Returns {reply, action?}. `ranked` is engine.rank(...) output.

    `history` is the prior conversation ([{role, content}, ...]) so follow-up questions
    keep context — e.g. the agent asks "which category?" and the user's next reply is
    understood as the answer, not a new query."""
    client = anthropic.Anthropic()
    action = None
    grounding = []   # every tool result the model saw — the basis for faithfulness checks
    messages = list(history or []) + [{"role": "user", "content": message}]

    for _ in range(MAX_STEPS):
        resp = client.messages.create(
            model=MODEL, max_tokens=1400,
            system=SYSTEM, tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return {"reply": text or "I'm not sure how to help with that yet.",
                    "action": action, "grounding": grounding}

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            out = _run_tool(b.name, b.input, ranked)
            grounding.append({"tool": b.name, "result": out})
            if b.name == "set_weights":
                action = {"type": "set_weights", "weights": {k: b.input.get(k) for k in
                          ("rr", "sharpe", "stdDev", "upCap", "downCap", "aum", "ter")}}
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})

    return {"reply": "That took too many steps — try rephrasing.",
            "action": action, "grounding": grounding}


def _run_tool(name, args, ranked):
    if name == "set_weights":
        return {"status": "applied", "weights": {k: args.get(k) for k in
                ("rr", "sharpe", "stdDev", "upCap", "downCap", "aum", "ter")}}
    if name == "get_fund":
        f = _find(args.get("name", ""), ranked)
        return _fund_view(f) if f else {"error": f'No fund found matching "{args.get("name")}".'}
    if name == "get_category_stats":
        return _category_stats(args.get("category", ""), ranked)
    if name == "compare_funds":
        fa, fb = _find(args.get("a", ""), ranked), _find(args.get("b", ""), ranked)
        return {"a": _fund_view(fa) if fa else {"error": f'not found: {args.get("a")}'},
                "b": _fund_view(fb) if fb else {"error": f'not found: {args.get("b")}'}}
    return {"error": "unknown tool"}
