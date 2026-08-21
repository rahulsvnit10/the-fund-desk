"""
The Fund Desk - data engine.

Single source of truth for fund metrics + our own ranking.

Two data feeds, both free:
  1. Tickertape fund pages (server-rendered __NEXT_DATA__ JSON)
     -> Rolling (3Y) return, Sharpe, Std Dev, AUM, TER
  2. AMFI NAV via mfapi.in
     -> monthly returns, used to COMPUTE Upside/Downside capture
        against each category's benchmark index fund.

Beta is intentionally dropped in favour of the two capture ratios.
We never use Tickertape's ranking - we compute our own.
"""

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"

# ---------------------------------------------------------------------------
# Metric definitions - the seven we rank on. dir: +1 higher-better, -1 lower.
# ---------------------------------------------------------------------------
METRICS = [
    {"key": "rr",       "label": "Rolling Returns", "dir": 1,  "unit": "%",  "compute": False},
    {"key": "sharpe",   "label": "Sharpe Ratio",    "dir": 1,  "unit": "",   "compute": False},
    {"key": "stdDev",   "label": "Std Deviation",   "dir": -1, "unit": "%",  "compute": False},
    {"key": "upCap",    "label": "Upside Capture",  "dir": 1,  "unit": "%",  "compute": True},
    {"key": "downCap",  "label": "Downside Capture","dir": -1, "unit": "%",  "compute": True},
    {"key": "aum",      "label": "AUM",             "dir": 1,  "unit": "Cr", "compute": False},
    {"key": "ter",      "label": "TER",             "dir": -1, "unit": "%",  "compute": False},
]
METRIC_KEYS = [m["key"] for m in METRICS]

# ---------------------------------------------------------------------------
# Universe: Tickertape category listing pages. Each yields ~10 top funds.
# (label, url). Order roughly by popularity.
# ---------------------------------------------------------------------------
CATEGORY_PAGES = [
    ("Large Cap",       "https://www.tickertape.in/mutualfunds/equity/large-cap-fund"),
    ("Flexi Cap",       "https://www.tickertape.in/mutualfunds/equity/flexi-cap-fund"),
    ("Mid Cap",         "https://www.tickertape.in/mutualfunds/equity/mid-cap-fund"),
    ("Small Cap",       "https://www.tickertape.in/mutualfunds/equity/small-cap-fund"),
    ("Large & Mid Cap", "https://www.tickertape.in/mutualfunds/equity/large-and-mid-cap-fund"),
    ("ELSS",            "https://www.tickertape.in/mutualfunds/equity/elss-fund"),
    ("Multi Cap",       "https://www.tickertape.in/mutualfunds/equity/multi-cap-fund"),
    ("Value",           "https://www.tickertape.in/mutualfunds/equity/value-fund"),
    ("Focused",         "https://www.tickertape.in/mutualfunds/equity/focused-fund"),
    ("Balanced Advantage", "https://www.tickertape.in/mutualfunds/hybrid/balanced-advantage-fund"),
    ("Aggressive Hybrid", "https://www.tickertape.in/mutualfunds/hybrid/aggressive-hybrid-fund"),
    # directory-only (broaden lookup coverage; funds here rank if within the cap)
    ("Contra",          "https://www.tickertape.in/mutualfunds/equity/contra-fund"),
    ("Dividend Yield",  "https://www.tickertape.in/mutualfunds/equity/dividend-yield-fund"),
    ("Multi Asset",     "https://www.tickertape.in/mutualfunds/hybrid/multi-asset-allocation-fund"),
    ("Equity Savings",  "https://www.tickertape.in/mutualfunds/hybrid/equity-savings"),
    ("Conservative Hybrid", "https://www.tickertape.in/mutualfunds/hybrid/conservative-hybrid-fund"),
]

# Benchmark index fund (mfapi search query) per category, for capture ratios.
# Equity caps map to their natural index; hybrids to a broad market proxy.
BENCHMARK_QUERY = {
    "Large Cap":          "UTI Nifty 50 Index",
    "Large & Mid Cap":    "Motilal Oswal Nifty 500 Index",
    "Flexi Cap":          "Motilal Oswal Nifty 500 Index",
    "Multi Cap":          "Motilal Oswal Nifty 500 Index",
    "Value":              "Motilal Oswal Nifty 500 Index",
    "Focused":            "Motilal Oswal Nifty 500 Index",
    "ELSS":               "Motilal Oswal Nifty 500 Index",
    "Mid Cap":            "Motilal Oswal Nifty Midcap 150 Index",
    "Small Cap":          "Nippon India Nifty Smallcap 250 Index",
    "Aggressive Hybrid":  "UTI Nifty 50 Index",
    "Balanced Advantage": "UTI Nifty 50 Index",
    "Contra":             "Motilal Oswal Nifty 500 Index",
    "Dividend Yield":     "Motilal Oswal Nifty 500 Index",
    "Multi Asset":        "UTI Nifty 50 Index",
    "Equity Savings":     "UTI Nifty 50 Index",
    "Conservative Hybrid":"UTI Nifty 50 Index",
}

# Hardcoded mfapi scheme codes for each benchmark index fund. Direct name
# matching is fragile for these (AMFI writes "Midcap" as one word), and the
# codes are stable, so we resolve benchmarks by code.
_NIFTY50, _NIFTY500 = "120716", "147625"
_MIDCAP150, _SMALLCAP250 = "147622", "148519"
# debt benchmarks (representative bond indices, by duration bucket)
_GSEC = "151597"        # Invesco India Nifty G-sec Sep 2032 Index (medium-long bond)
_SHORTDEBT = "150754"   # Nippon India Nifty AAA PSU Bond + SDL Sep 2026 (short/mid)
_GOLD = "140088"        # Nippon India ETF Gold BeES (gold-price ETF, out of the ranked universe → no self-reference)
BENCHMARK_CODE = {
    "Large Cap": _NIFTY50, "Aggressive Hybrid": _NIFTY50, "Balanced Advantage": _NIFTY50,
    "Multi Asset": _NIFTY50, "Equity Savings": _NIFTY50, "Conservative Hybrid": _NIFTY50,
    "Flexi Cap": _NIFTY500, "Multi Cap": _NIFTY500, "Value": _NIFTY500, "Focused": _NIFTY500,
    "ELSS": _NIFTY500, "Large & Mid Cap": _NIFTY500, "Contra": _NIFTY500, "Dividend Yield": _NIFTY500,
    "Mid Cap": _MIDCAP150, "Small Cap": _SMALLCAP250,
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _get(url, tries=3, as_json=False):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            raw = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
            return json.loads(raw) if as_json else raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def _next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else {}


# ---------------------------------------------------------------------------
# Tickertape: harvest slugs + parse a fund page
# ---------------------------------------------------------------------------
def harvest_slugs(url):
    """Return ordered unique fund slugs (…-M_XXXX) from a category page."""
    html = _get(url)
    seen, out = set(), []
    for s in re.findall(r'/mutualfunds/[a-z0-9-]+-M_[A-Z0-9]+', html):
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


SITEMAP_URL = "https://www.tickertape.in/sitemaps/mutualfunds/sitemap.xml"
_VARIANT = ("idcw", "reinv", "-div", "dividend", "bonus", "payout", "-reg-", "regular", "fof-", "-fof")

def sitemap_slugs():
    """Every mutual-fund slug from Tickertape's sitemap, growth plans only.

    Excludes IDCW / reinvest / dividend / regular / fund-of-fund variants so we
    rank one Direct-Growth entry per fund (final Direct dedupe is by TER later).
    """
    body = _get(SITEMAP_URL)
    slugs, seen = [], set()
    for loc in re.findall(r"<loc>([^<]+)</loc>", body):
        s = loc.split("tickertape.in")[-1]
        low = s.lower()
        if "-m_" not in low or any(t in low for t in _VARIANT):
            continue
        if s not in seen:
            seen.add(s)
            slugs.append(s)
    return slugs


# category (Tickertape subsector) -> benchmark index-fund scheme code
def benchmark_for_category(cat):
    c = (cat or "").lower()
    if "gold" in c:
        return _GOLD
    if "small cap" in c:
        return _SMALLCAP250
    if "mid cap" in c and "large" not in c:
        return _MIDCAP150
    if "large" in c and "mid" in c:
        return _NIFTY500
    if "large cap" in c:
        return _NIFTY50
    if any(k in c for k in ("flexi", "multi cap", "value", "focused", "contra",
                            "dividend yield", "elss", "tax", "thematic", "sector")):
        return _NIFTY500
    if any(k in c for k in ("aggressive", "balanced", "multi asset", "equity savings",
                            "conservative hybrid", "asset allocation", "hybrid")):
        return _NIFTY50
    # debt & money-market: capture vs a duration-appropriate bond index
    if any(k in c for k in ("liquid", "overnight", "money market", "ultra short",
                            "low duration", "short duration", "floating", "arbitrage")):
        return _SHORTDEBT
    if any(k in c for k in ("gilt", "long duration", "medium", "corporate bond", "credit",
                            "banking", "dynamic bond", "bond", "duration", "debt",
                            "fixed maturity", "interval")):
        return _GSEC
    return None  # anything genuinely unclassifiable


def _find_returns_node(obj, ret1y_hint):
    """Find the fund's own returns dict (ret1y/ret3y) disambiguated by ret1y."""
    best = None
    def walk(o):
        nonlocal best
        if isinstance(o, dict):
            if all(k in o for k in ("ret1y", "ret3y")) and isinstance(o.get("ret3y"), (int, float)):
                if ret1y_hint is None or abs((o.get("ret1y") or 0) - ret1y_hint) < 0.01:
                    best = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return best


def name_from_slug(slug):
    """'/mutualfunds/hdfc-mid-cap-fund-M_HDCMS' -> 'hdfc mid cap fund'."""
    core = slug.rsplit("/", 1)[-1]
    core = re.sub(r"-M_[A-Z0-9]+$", "", core)
    return core.replace("-", " ").strip()


def best_directory_match(query, directory):
    """Fuzzy-match a typed name against the harvested slug directory.

    Uses _expand (not _norm) so 'midcap' and 'mid cap' unify, and requires the
    query's leading brand token (the AMC, e.g. 'kotak') to appear in the match when
    that token is a real brand in the directory. Without this, 'kotak mid cap fund'
    fell through to 'Tata Mid Cap Fund' — same category, wrong house.
    """
    return match_with_index(query, match_index(directory))


def _collapse(s):                        # 'Mid Cap' / 'Midcap' -> 'midcap' (space-insensitive)
    return re.sub(r"[^a-z0-9]", "", _expand(s))


def match_index(directory):
    """Precompute (entry, token_set, collapsed_name) once, to reuse across a batch of
    lookups (e.g. a CSV upload) instead of re-expanding every fund on every query."""
    return [(e, set(t for t in _expand(e["name"]).split() if t not in _STOP), _collapse(e["name"]))
            for e in directory]


def _tok_hit(t, ct, col):
    # a token matches by exact token OR (for real words) as a substring of the
    # collapsed name — so 'bluechip' finds 'Blue Chip' and 'mid'+'cap' find 'Midcap'
    return t in ct or (len(t) >= 4 and t in col)


def match_with_index(query, index):
    """Match one query against a prebuilt match_index. Same logic as best_directory_match."""
    qt_list = [t for t in _expand(query).split() if t not in _STOP]
    qt = set(qt_list)
    if not qt:
        return None
    brand = qt_list[0]
    # the leading token counts as a brand only if it actually names some fund
    brand_required = any(_tok_hit(brand, ct, col) for _, ct, col in index)
    best, best_score = None, (-1, 0)
    for e, ct, col in index:
        if brand_required and not _tok_hit(brand, ct, col):
            continue                     # never cross AMCs (kotak query -> only kotak funds)
        overlap = sum(1 for t in qt if _tok_hit(t, ct, col))
        score = (overlap, -(len(ct - qt) + len(qt - ct)))
        if overlap and score > best_score:
            best, best_score = e, score
    # a multi-word query must match more than just the AMC — else 'sbi bluechip'
    # (no such fund here) would return any SBI fund. Blank beats a wrong match.
    min_needed = 2 if len(qt) >= 2 else 1
    if best and best_score[0] >= min_needed:
        return best
    return None


def search_fund(text):
    """Resolve a typed/pasted fund name to matching Tickertape funds.

    Returns a list of {name, slug} for mutual-fund results, best match first.
    """
    url = "https://api.tickertape.in/search?types=mutualfund&text=" + urllib.parse.quote(text)
    try:
        data = _get(url, as_json=True)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in (data.get("data", {}) or {}).get("stocks", []) or []:
        slug = it.get("slug", "")
        if it.get("type") == "mutualfund" and "/mutualfunds/" in slug and "-M_" in slug:
            out.append({"name": it.get("name"), "slug": slug})
    return out


def parse_fund(slug):
    """Fetch a Tickertape fund page and return a raw metric record (no captures)."""
    html = _get("https://www.tickertape.in" + slug if slug.startswith("/") else slug)
    data = _next_data(html)
    pp = data.get("props", {}).get("pageProps", {})
    si = pp.get("securityInfo", {})
    ss = pp.get("securitySummary", {})

    vals = {}
    for it in ss.get("keyRatios", []) or []:
        if isinstance(it, dict) and isinstance(it.get("value"), (int, float)):
            vals[it["backL"]] = it["value"]
    ratios = (pp.get("mfPageFaq", {}) or {}).get("ratios") or {}
    for k, v in ratios.items():
        if isinstance(v, (int, float)):
            vals[k] = v

    rnode = _find_returns_node(pp, vals.get("ret1y"))
    if rnode:
        vals.setdefault("ret3y", rnode.get("ret3y"))

    isin = None
    def _isin(o):
        nonlocal isin
        if isin:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower() == "isin" and isinstance(v, str) and v.startswith("INF"):
                    isin = v; return
                _isin(v)
        elif isinstance(o, list):
            for v in o:
                _isin(v)
    _isin(pp)

    mf_id = si.get("mfId") or (slug.rsplit("_", 1)[-1] and "M_" + slug.rsplit("M_", 1)[-1])
    return {
        "mfId": mf_id,
        "slug": slug,
        "isin": isin,
        "name": si.get("name"),
        "amc": si.get("amc"),
        "category": si.get("subsector") or si.get("sector"),
        "rr": vals.get("ret3y"),
        "sharpe": vals.get("sharpe"),
        "stdDev": vals.get("stdDev"),
        "aum": vals.get("aum"),
        "ter": vals.get("expRatio"),
    }


# ---------------------------------------------------------------------------
# AMFI ISIN -> scheme code map (exact matching, name-independent)
# ---------------------------------------------------------------------------
_AMFI_MAP = None

def amfi_map():
    """ISIN -> AMFI scheme code (== mfapi code), from AMFI's public NAVAll file."""
    global _AMFI_MAP
    if _AMFI_MAP is None:
        _AMFI_MAP = {}
        try:
            raw = _get("https://www.amfiindia.com/spages/NAVAll.txt")
            for line in raw.splitlines():
                p = line.split(";")
                if len(p) >= 4 and p[0].strip().isdigit():
                    for isin in (p[1].strip(), p[2].strip()):
                        if isin.startswith("INF"):
                            _AMFI_MAP[isin] = p[0].strip()
        except Exception:  # noqa: BLE001
            pass
    return _AMFI_MAP


def scheme_for(rec):
    """Resolve a fund to its mfapi scheme code: ISIN first, then name match."""
    isin = rec.get("isin")
    if isin:
        code = amfi_map().get(isin)
        if code:
            return code
    return match_scheme(rec.get("name") or "")


# ---------------------------------------------------------------------------
# mfapi.in NAV: match a scheme, monthly returns, capture ratios
# ---------------------------------------------------------------------------
_norm = lambda s: re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
_STOP = {"fund", "direct", "growth", "plan", "option", "regular", "the", "scheme", "an"}

# Tickertape vs AMFI naming differences: abbreviations and compound cap-words.
_COMPOUND = [("flexicap", "flexi cap"), ("midcap", "mid cap"), ("smallcap", "small cap"),
             ("largecap", "large cap"), ("multicap", "multi cap"), ("largemidcap", "large mid cap")]
_ALIAS = {"pru": "prudential", "sl": "sun life", "mf": "", "opp": "opportunities",
          "opps": "opportunities", "intl": "international", "svc": "services",
          "mgmt": "management", "corp": "corporate", "infra": "infrastructure"}

def _expand(s):
    """Normalize a fund name for matching across Tickertape/AMFI conventions."""
    s = _norm(s)
    for a, b in _COMPOUND:
        s = re.sub(r"\b" + a + r"\b", b, s)
    toks = []
    for t in s.split():
        toks.extend(_ALIAS.get(t, t).split())  # alias may expand to two words
    return " ".join(t for t in toks if t)

def match_scheme(name):
    """Find the mfapi scheme code for a fund's Direct plan (Growth preferred).

    Index funds are often named '... - Direct Plan' with no 'Growth' word, so
    'Growth' is a preference, not a requirement. IDCW/Dividend plans excluded.
    """
    toks = [t for t in _expand(name).split() if t not in _STOP]
    q = " ".join(toks)
    try:
        hits = _get("https://api.mfapi.in/mf/search?q=" + urllib.parse.quote(q), as_json=True)
    except Exception:  # noqa: BLE001
        return None
    if not hits:
        return None

    def usable(h):
        n = h["schemeName"].lower()
        return "direct" in n and "idcw" not in n and "dividend" not in n
    cands = [h for h in hits if usable(h)] or hits
    want = set(toks)

    def score(h):
        cand_toks = set(t for t in _expand(h["schemeName"]).split() if t not in _STOP)
        overlap = len(want & cand_toks)
        extra = len(cand_toks - want)      # tokens in candidate not asked for
        missing = len(want - cand_toks)    # asked-for tokens not present
        growth = "growth" in h["schemeName"].lower()
        # most overlap, then fewest extra/missing (exact beats superset), then Growth
        return (overlap, -extra - missing, growth)
    cands.sort(key=score, reverse=True)
    top = cands[0]

    # Accept only a confident match. A wrong scheme means wrong capture numbers,
    # so when names genuinely diverge we return None and show "—" instead.
    ctoks = set(t for t in _expand(top["schemeName"]).split() if t not in _STOP)
    if len(want & ctoks) < max(2, round(len(want) * 0.6)):
        return None
    CAPWORDS = {"large", "mid", "small", "multi", "flexi", "micro"}
    qcap, ccap = want & CAPWORDS, ctoks & CAPWORDS
    if qcap and qcap != ccap:            # e.g. 'large cap' must not match 'large mid cap'
        return None
    return top["schemeCode"]


def monthly_returns(code, years=3):
    d = _get(f"https://api.mfapi.in/mf/{code}", as_json=True)
    rows = []
    for x in d.get("data", []):
        if x["nav"] not in ("", "0", "0.0"):
            rows.append((datetime.strptime(x["date"], "%d-%m-%Y"), float(x["nav"])))
    rows.sort()
    by_month = {}
    for dt, nav in rows:
        by_month[(dt.year, dt.month)] = nav  # last nav of each month wins
    seq = [by_month[k] for k in sorted(by_month)]
    seq = seq[-(years * 12 + 1):]
    return [(seq[i] / seq[i - 1] - 1) * 100 for i in range(1, len(seq)) if seq[i - 1]]


def capture_from_series(fr, br):
    """Upside/Downside capture (%) from two aligned monthly-return series."""
    n = min(len(fr), len(br))
    if n < 12:
        return None, None
    fr, br = fr[-n:], br[-n:]
    up = [(f, b) for f, b in zip(fr, br) if b > 0]
    dn = [(f, b) for f, b in zip(fr, br) if b < 0]
    up_c = (sum(f for f, _ in up) / sum(b for _, b in up) * 100) if up else None
    dn_c = (sum(f for f, _ in dn) / sum(b for _, b in dn) * 100) if dn else None
    return (round(up_c, 1) if up_c is not None else None,
            round(dn_c, 1) if dn_c is not None else None)


def capture_ratios(fund_code, bench_code):
    """Return (upside%, downside%) fetching both NAV series."""
    try:
        return capture_from_series(monthly_returns(fund_code), monthly_returns(bench_code))
    except Exception:  # noqa: BLE001
        return None, None


# ---------------------------------------------------------------------------
# Ranking - our own model. Equal weights by default; caller can override.
# ---------------------------------------------------------------------------
def rank(funds, weights=None):
    """Return funds with .score plus overall + in-category rank, sorted by score."""
    weights = weights or {k: 1.0 for k in METRIC_KEYS}
    wsum = sum(weights.get(k, 0) for k in METRIC_KEYS) or 1.0

    bounds = {}
    for k in METRIC_KEYS:
        xs = [f[k] for f in funds if isinstance(f.get(k), (int, float))]
        bounds[k] = (min(xs), max(xs)) if xs else (0, 1)

    for f in funds:
        s, used = 0.0, 0.0
        norm = {}
        for m in METRICS:
            k = m["key"]
            v = f.get(k)
            if not isinstance(v, (int, float)):
                continue
            lo, hi = bounds[k]
            n = 0.5 if hi == lo else (v - lo) / (hi - lo)
            if m["dir"] < 0:
                n = 1 - n
            norm[k] = round(n, 4)
            s += n * weights.get(k, 0)
            used += weights.get(k, 0)
        f["_norm"] = norm
        f["score"] = round((s / used * 100) if used else 0, 1)

    ordered = sorted(funds, key=lambda f: f["score"], reverse=True)
    for i, f in enumerate(ordered):
        f["rank"] = i + 1
    by_cat = {}
    for f in ordered:
        by_cat.setdefault(f.get("category"), []).append(f)
    for cat, lst in by_cat.items():
        for i, f in enumerate(lst):
            f["catRank"] = i + 1
            f["catSize"] = len(lst)
    return ordered
