# Evaluating an AI feature — a PM's guide

Any feature powered by an AI model has a quality problem you don't get with normal
software: the model can word the same answer differently every time, and occasionally get
something wrong. "It looked fine when I tried it" isn't something you can ship on.

**Evaluation** is how you turn that gut feel into a number you can track — you grade the
model's answers against clear criteria, so you can tell whether a change made the feature
better or worse.

This guide explains the concepts generically first, then shows how our own feature — the
Fund Desk's "Ask Fund Agent" — puts each one into practice.

---

## The 4 steps of evaluation

Evals aren't a single test you run once. They're a loop with four stages. Skipping the
early ones is why teams end up "testing by vibes."

### 1. Observability (tracing) — *see what the feature actually does*
Before you can grade anything, you have to **capture** it: every call the feature makes —
the user's input, what the model did step by step (including any tools it called), and the
final answer. This recording is called a **trace**, and the practice is **observability**.

Think of it as **CCTV for your AI feature.** No footage, nothing to review. This is what
tools like Langfuse and LangSmith give you first — a dashboard of every call.

> Without this step, the other three are impossible. You can't grade, monitor, or improve
> what you never recorded.

### 2. Offline evaluation — *test before you ship*
Run the feature against a **golden set** — a fixed list of test cases where you already
know what a good answer looks like — and score each one. You do this **on demand**, usually
right before shipping a change.

Think of it as an **exam with an answer key that you wrote.** It's your regression test:
"did my new prompt still pass all the cases it used to?"

### 3. Online evaluation — *watch real traffic after you ship*
Grade **real user interactions as they happen** in production. You've never seen these
questions before, so there's no answer key — you can only run the checks that don't need one.

Think of it as **monitoring live calls** in a support centre. Offline is the training exam;
online is quality-listening on real customers.

### 4. Iteration (the flywheel) — *close the loop*
Read the failures, fix the root cause (usually the prompt, sometimes the model or the data),
and **re-measure**. Crucially, when a real user hits a bug online, you **promote that case
into the golden set** — so from then on every offline run guards against it, and the bug
can never quietly come back.

> This is the whole point. Measure → fix → re-measure, with the answer key growing from real
> usage. Steps 1–3 exist to feed step 4.

```
   ┌─────────────────────────────────────────────────────────┐
   │                                                         │
   ▼                                                         │
1. OBSERVE  ──▶  2. OFFLINE EVAL  ──▶  3. ONLINE EVAL  ──▶  4. ITERATE
 (trace it)     (golden set / QA)     (live traffic)      (fix + re-measure)
                      ▲                                         │
                      └───────── promote real failures ────────┘
```

---

## The 4 building blocks

The 4 steps above are the **process** (what you do, and when). This is the **parts list** —
the machinery you assemble to make that process run. Every eval system, in any tool, is these
four nouns:

1. **Traces (observability)** — the recording of what your app did on one run: input, prompt,
   tool calls, output, tokens, latency, cost. The foundation — everything else scores traces.
2. **Dataset** — your test cases: a list of example inputs, optionally each with a reference
   (expected) output. This is the "golden set."
3. **Task** — the thing under test: the prompt / chain / agent that maps input → output.
4. **Evaluators (scorers)** — functions that grade an output. Three flavours:
   - **Code / heuristic** — exact match, regex, "is valid JSON?", contains keyword, latency < X,
     cost < Y.
   - **LLM-as-a-judge** — a second LLM scores correctness / relevance / tone against criteria
     or the reference.
   - **Human** — you or reviewers label outputs in a queue.

### Parts vs process — how they connect
The parts get *used* across the process. Same machinery, cross-sectioned two ways:

| Building block (the part) | Where it shows up in the lifecycle |
|---|---|
| **Traces** | *Produced* in Step 1 (Observe). Also what Step 3 (Online) scores live. |
| **Dataset** | *Used* in Step 2 (Offline). *Grown* in Step 4 (Iterate). |
| **Task** | *Exercised* in both Step 2 and Step 3 — the thing being graded either way. |
| **Evaluators** | *Applied* in Step 2 (all 3 flavours, answer-key allowed) and Step 3 (only the flavours that need no answer key). |

One sentence that ties all four together:

> In each lifecycle stage you run your **Task** over some inputs (a **Dataset** offline, or live
> traffic online), record a **Trace** of what happened, and score that trace with **Evaluators**.

The parts list describes a *single run*. The lifecycle's Step 4 (promote real failures into the
dataset) is what the parts list leaves implicit — it's the loop that makes evals compound over
time.

---

## Offline vs Online

The two evaluation steps differ on one thing: **do you know the right answer in advance?**

| | **Offline** | **Online** |
|---|---|---|
| The cases | a golden set (fixed, pre-written) | whatever real users type |
| When | before shipping, on demand | continuously, in production |
| Right answer known? | **Yes** — you wrote it | **No** — brand-new question |
| Feels like | a QA / regression exam | monitoring live calls |

**Key consequence:** a **golden set only exists for offline.** You can't pre-write the answer
to a question a user hasn't asked yet. So online can only run checks that *don't* need an
answer key (see faithfulness below).

---

## Metrics vs methods

Two ideas people constantly blur:

- **A metric is *what* you measure** — accuracy, faithfulness, relevance, tone, safety.
- **A method is *how* you measure it** — plain code, an AI grader ("LLM-as-a-judge"), or a
  human reviewer.

The same metric can be measured by different methods. This separation matters because it's
why one quality (say, "did it make something up?") can be checked two completely different
ways.

### The common metrics in the industry
Accuracy/correctness, **faithfulness** (a.k.a. groundedness / "not hallucinating"), relevance,
helpfulness, tone, safety/toxicity. These are standard concepts across the field (RAGAS,
Langfuse, LangSmith, and others) — not names we invented.

### Accuracy vs Faithfulness — the two that get confused
They sound alike but compare against **different reference points**:

| | Compares… | …against | Needs an answer key? | The question |
|---|---|---|---|---|
| **Accuracy** | what the feature decided/answered | **the correct answer you wrote** | **Yes** | "Did it do the right thing?" |
| **Faithfulness** | the facts/figures in the answer | **the source data it was given** | **No** | "Did it make something up?" |

One-liner: **Accuracy = "matches the answer we expected." Faithfulness = "matches the data it
was given, nothing invented."**

Note faithfulness is *not* about whether the underlying data is correct — that's data quality.
It's about whether the *answer* stays true to whatever data the model was handed.

Because faithfulness needs no answer key, **it's the metric you can run online**; accuracy
(which needs one) is offline-only.

### LLM-as-a-judge is a *method*, not a metric
"Using a second, stronger AI to grade the first AI's answer" is a **technique**. You can point
it at faithfulness, tone, relevance — anything subjective that code can't easily check. So
"is it inventing something?" is *faithfulness* (the what); an LLM judge is one *way* to check
it (the how). Code is another way. Always sanity-check the judge itself — it's an AI too, so a
bad score can mean the answer was wrong **or** the judge was too harsh.

---

## How the Fund Desk puts this into practice

Everything above is generic. Here's how our "Ask Fund Agent" feature — the chat widget that
sets ranking weights and explains funds — implements each piece.

### The feature's four skills, and what each is graded on

| Skill | Metric | Plain check |
|---|---|---|
| **Set weights** (NL → the 7 ranking dials) | Accuracy | "Safety + low fees" → did it actually turn up the safety and cost dials? |
| **Explain a fund** | Faithfulness | Did it stick to the fund's real numbers, or make things up? |
| **Compare two funds** | Faithfulness + balance | Real numbers, and a fair pros/cons for each? |
| **Summarise a category** | Faithfulness | Do the summary's figures match the real averages? |

### The two metrics we chose (and why only two)
We deliberately track just **accuracy** (for weight-setting) and **faithfulness** (for the
three generative skills). They're what actually matters for a fund agent — a wrong number or
an over-claim is the real risk, not tone.

### Our three evaluators, mapped to metric + method

| Evaluator | Metric | Method | What it catches |
|---|---|---|---|
| `weight_intent` | Accuracy | code (compare dials to expected intent) | didn't raise the metrics the user asked for |
| `numbers_faithful` | Faithfulness | code (does a stated figure exist in the data?) | invented **numbers** — a fake Sharpe, return, fee |
| `llm_faithful` | Faithfulness | **LLM-as-a-judge** (Claude Sonnet grades) | invented **claims** — "lowest cost", "beats the market" |

Two things worth calling out:

- **Faithfulness is checked twice, two different ways.** `numbers_faithful` is faithfulness by
  code (catches fake *numbers*); `llm_faithful` is faithfulness by AI judge (catches unsupported
  *claims* the number check can't see). Both are faithfulness — neither is accuracy.
- **Don't confuse `numbers_faithful` with accuracy** just because both involve figures.
  Accuracy compares a *decision* to *our answer key*; `numbers_faithful` compares *stated
  figures* to *the source data* (no answer key). Different reference point = different metric.

### Where the Fund Desk stands on the 4 steps

| Step | Status for the Fund Desk |
|---|---|
| 1. Observability | ✅ Offline runs **and every live production chat** trace to our Langfuse dashboard |
| 2. Offline eval | ✅ Two golden sets — "set weights" (7 cases) and "explain a fund" (spanning equity + debt) |
| 3. Online eval | ✅ **Live** — every production chat is traced (`/api/agent`) and graded on faithfulness by `evals/score_live.py`, run automatically each day (GitHub Action) plus an on-demand button |
| 4. Iteration | 🟡 Flywheel ready — when a live chat scores low, promote it into the offline golden set so every future run guards against it |

### What our latest runs told us
- **Set weights (accuracy):** `weight_intent` averages **0.976** across 7 cases. The one
  imperfect case is the conflicting request *"safety and low fees, with a high growth"* —
  the agent raised rolling return for "growth" but left *upside capture* just below average
  (scored 0.83, 5 of 6 checks). A genuine, specific weakness the golden set surfaced.
- **Explain a fund (faithfulness):** numbers are almost always right, but explanations tend to
  add one claim the data can't prove — e.g. calling a fund "**lowest** cost" when it only knows
  *that* fund's fee. A one-line prompt fix, then re-run to confirm the score rises. That loop is
  the entire point of evals.

---

## How to improve a score (as a PM)

A low score is a *symptom*. The PM skill is diagnosing the cause before "fixing the AI,"
because half the time the AI isn't the problem — the test is. Every low score has three
possible causes, and they have completely different fixes:

| The real cause | What it looks like | The fix |
|---|---|---|
| **The grader is wrong** | The answer is actually fine; the evaluator flags it anyway | Fix the evaluator, not the agent |
| **The test is unfair** | The "correct answer" you wrote was too strict or ambiguous | Fix the golden set |
| **The agent is wrong** | The answer genuinely fabricates or over-claims | Fix the prompt (or model/data) |

**Always rule out the first two before touching the agent.** Chasing a score the grader is
mis-assigning just over-fits your agent to a broken test. Our own run was a live example of
all three:

### Improving accuracy — worked example
Our weight-setting score sat at 0.976, dragged down by one case ("safety **and** high growth").

- We *looked* before fixing, and found the grader marked a dial "not raised" if it was below
  the **average** of all dials. On a request that raises many dials, the average inflates and
  unfairly fails dials that were genuinely raised. → **The grader was wrong.** We changed the
  bar to a fixed neutral midpoint. Score went to **1.0**, no change to the agent.
- The lesson: the first move on a low accuracy score is *read the failing case and check the
  answer key*, not rewrite the prompt.

### Improving faithfulness — worked example
Our faithfulness scores looked bad (numbers 0.50, claims 0.44). Three different causes, found
by reading the flagged answers:

1. **Grader blind to context.** The agent cited real *category averages* (from a tool), but
   the evaluators only knew the single fund's numbers, so they flagged every category figure as
   "invented." → Fix: feed the grader the agent's **full grounding** (everything it actually
   saw). Biggest single jump.
2. **Grader bug.** A negative Sharpe of "−0.90" was read as "0.90" (the minus sign was dropped)
   and flagged. → Fix: match on the absolute value.
3. **Agent genuinely over-claiming.** *This* was the real one — the agent editorialised:
   calling a 1.02% fee "low cost" with nothing to compare against, reading large AUM as
   "investor confidence," guessing how the ranking works, saying a trait was "typical for the
   category" without category data. → Fix: a prompt rule — *state what the numbers say, don't
   interpret what they imply, and make no comparison you don't have data for.*

**The general playbook for faithfulness:** read the specific claims the judge flagged →
separate "real over-claim" from "grader couldn't see it" → fix graders first, then tighten the
prompt to forbid the exact over-claim pattern (superlatives without data, interpreting numbers,
inventing mechanisms) → re-run and confirm the score moved.

> The meta-lesson a PM should take away: **evals improve the tests as much as they improve the
> product.** A grader you can't trust is worse than no grader, so validating the grader is part
> of the job — never optimise a number you haven't verified is measuring the right thing.

---

## How to run it and see the scores

Run from the project folder (needs the API keys in `.env`):

```bash
./.venv/bin/python evals/run_evals.py            # OFFLINE: run all golden-set tests
./.venv/bin/python evals/run_evals.py weights    # offline: just "set weights" (accuracy)
./.venv/bin/python evals/run_evals.py explain    # offline: just "explain a fund" (faithfulness)
./.venv/bin/python evals/score_live.py           # ONLINE: grade new live production chats
```

Offline prints average scores + a dashboard link — analyse at **Langfuse → Datasets → pick the
set → Runs**. Online chats show at **Langfuse → Traces**, filtered by the **`live-chat`** tag,
each with its faithfulness scores; the online scorer also runs automatically every day (GitHub
Action → "Score live chats", which also has a manual **Run workflow** button).

---

*Technical companion for engineers: the evaluator code is in `evals/evaluators.py`, the
datasets and runner in `evals/run_evals.py`.*
