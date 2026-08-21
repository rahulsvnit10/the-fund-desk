"""Evaluators for the Fund Desk agent (Langfuse Evaluation objects).

Start with the objective one: does set_weights raise the metrics the user asked
to prioritise and lower the ones they don't? Faithfulness evaluators for the
generative capabilities come next.
"""
import json
import re

import anthropic
from langfuse import Evaluation

MKEYS = ["rr", "sharpe", "stdDev", "upCap", "downCap", "aum", "ter"]
JUDGE_MODEL = "claude-sonnet-5"   # a stronger model than the Haiku that generates
NEUTRAL = 1.5   # midpoint of the 0–3 weight scale: above = prioritised, below = de-prioritised


def weight_intent(*, input, output, expected_output, **kwargs):
    """Directional check: 'high' metrics raised past neutral, 'low' metrics below it.

    expected_output = {"high": [...metric keys...], "low": [...metric keys...]}.
    Scores the *intent*, not an exact vector — there's no single right weighting.

    Threshold is the fixed neutral midpoint (1.5 on the 0–3 scale), NOT the sample
    mean. A request that legitimately raises many metrics (e.g. "safety AND growth")
    would inflate a mean-based bar and fail dials that were genuinely raised; the
    midpoint asks the fair question — "did the agent turn this dial past neutral?"
    """
    w = (output or {}).get("weights")
    if not w:
        return Evaluation(name="weight_intent", value=0.0,
                          comment="Agent did not set weights (no set_weights action).")
    high, low = expected_output.get("high", []), expected_output.get("low", [])
    checks, passed, misses = 0, 0, []
    for m in high:
        checks += 1
        if w.get(m, 0) > NEUTRAL:
            passed += 1
        else:
            misses.append(f"{m} should be high (got {w.get(m)}, neutral {NEUTRAL})")
    for m in low:
        checks += 1
        if w.get(m, 0) < NEUTRAL:
            passed += 1
        else:
            misses.append(f"{m} should be low (got {w.get(m)}, neutral {NEUTRAL})")
    score = passed / checks if checks else 0.0
    return Evaluation(name="weight_intent", value=score,
                      comment="All intent checks passed." if not misses else "; ".join(misses))


# --- Faithfulness (for explain / compare / summarize) --------------------------
# expected_output carries the fund's REAL metric snapshot (the ground truth).

def _numbers(text):
    out = []
    for r in re.findall(r"[\d][\d,]*\.?\d*", text or ""):
        try:
            out.append(float(r.replace(",", "")))
        except ValueError:
            pass
    return out


def _allowed(metrics):
    """Every real figure the answer may legitimately state, with rounding + unit forms."""
    vals = []
    for k in MKEYS:
        v = metrics.get(k)
        if isinstance(v, (int, float)):
            vals += [round(v, 2), round(v, 1), float(round(v))]
            if k == "aum":
                vals += [round(v / 1000, 1), float(round(v / 1000))]  # "₹105.1k cr"
    for k in ("rank", "catRank", "catSize", "score"):
        v = metrics.get(k)
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return vals


def _all_numbers(obj):
    """Recursively pull every numeric value out of a nested tool-result structure,
    with rounding variants — so category averages/medians the agent cited count as real."""
    vals = []
    if isinstance(obj, dict):
        for v in obj.values():
            vals += _all_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            vals += _all_numbers(v)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        for x in (obj, abs(obj)):   # abs form: a stated "0.90" should match a −0.90 Sharpe
            vals += [round(x, 2), round(x, 1), float(round(x)), float(x)]
            vals += [round(x / 1000, 1), float(round(x / 1000))]  # AUM in "k crore" form
    return vals


def _grounding_numbers(output, expected_output):
    """Allowed figures = everything in the agent's actual tool results (its grounding),
    falling back to the single-fund reference snapshot when no grounding was captured."""
    grounding = (output or {}).get("grounding")
    if grounding:
        return _all_numbers(grounding)
    return _allowed(expected_output or {})


def numbers_faithful(*, input, output, expected_output, **kwargs):
    """Deterministic gate: flag any DECIMAL figure the agent stated that appears in
    NEITHER its tool results nor the reference metrics.

    Checks against the agent's real grounding (all tool results — including category
    stats it pulled for comparison), not just the one fund's snapshot, so legitimately
    sourced comparison figures aren't mistaken for fabrications. Integers (ranks,
    counts, years) are left to the LLM judge.
    """
    allowed = _grounding_numbers(output, expected_output)
    stated = _numbers((output or {}).get("reply", ""))

    def ok(n):
        return any(abs(n - a) <= max(0.05, abs(a) * 0.02) for a in allowed)

    invented = [n for n in stated if n != int(n) and not ok(n)]
    return Evaluation(name="numbers_faithful", value=1.0 if not invented else 0.0,
                      comment="No invented figures." if not invented
                              else f"Figures not in tool results: {invented}")


_JUDGE_RUBRIC = """You grade a mutual-fund answer for FAITHFULNESS to the provided metrics.
Every factual claim — numbers AND qualitative statements ("low cost", "beats the market",
"ranks #1") — must be supported by the metrics given. Score:
2 = fully supported, nothing invented or contradicted
1 = mostly supported, one vague or slightly-off claim
0 = a clearly unsupported or wrong claim
Reason through the claims first, then score."""

_JUDGE_FORMAT = {"type": "json_schema", "schema": {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "integer", "enum": [0, 1, 2]},
    },
    "required": ["reasoning", "unsupported_claims", "score"],
    "additionalProperties": False,
}}


def llm_faithful(*, input, output, expected_output, **kwargs):
    """LLM-as-judge: catches unsupported *qualitative* claims the number check can't.

    The judge sees the agent's full grounding (all tool results, including any category
    stats it pulled), not just the single-fund snapshot — so category-comparison claims
    the agent legitimately sourced aren't marked unsupported.
    """
    grounding = (output or {}).get("grounding")
    context = json.dumps(grounding) if grounding else json.dumps(expected_output)
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=JUDGE_MODEL, max_tokens=1200,
        thinking={"type": "disabled"},     # schema already forces reasoning-first; avoid double-spend + truncation
        system=_JUDGE_RUBRIC, output_config={"format": _JUDGE_FORMAT},
        messages=[{"role": "user", "content":
                   f"DATA THE AGENT WAS GIVEN (tool results):\n{context}\n\nANSWER:\n{(output or {}).get('reply','')}"}],
    )
    try:
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
    except (StopIteration, json.JSONDecodeError) as e:
        return Evaluation(name="llm_faithful", value=None, comment=f"judge output not parseable ({e})")
    return Evaluation(name="llm_faithful", value=data["score"] / 2,
                      comment=data["reasoning"][:300],
                      metadata={"unsupported_claims": data.get("unsupported_claims")})
