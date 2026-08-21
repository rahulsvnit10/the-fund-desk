# The Fund Desk

Local mutual-fund screener. Ranks a universe of Indian mutual funds on seven
metrics using **your own weights**, and looks up any fund by name.

**Metrics:** Rolling (3Y) Returns · Sharpe · Std Deviation · Upside Capture ·
Downside Capture · AUM · TER

**Data (one consistent source per metric, free):**
- Tickertape fund pages → Returns, Sharpe, Std Dev, AUM, TER
- AMFI NAV (mfapi.in) → Upside/Downside Capture, computed vs each category's index fund

Refreshes every 24h or on demand. **Educational only — not investment advice.**

## Run
```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m uvicorn app:app --app-dir backend --port 8077
```
Open http://localhost:8077

Universe defaults to the full sitemap (~1,600 funds); set `FUND_DESK_MAX=40`
for a fast dev build. First full build takes a few minutes; after that it's
cached and auto-refreshes every 24h (or `backend/recompute.py` re-derives just
the capture ratios from cache in ~30s after a benchmark change).

## Ask Fund Agent (optional)

The chat widget (bottom-right) uses Claude to set weights from plain language,
explain a fund's rank, summarise a category, or compare two funds — every answer
grounded in the live data. It needs an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

then start the server. Without a key the dashboard works fully; only the agent
is disabled (it shows a "set your key" message).
