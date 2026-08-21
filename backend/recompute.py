"""Recompute only the capture ratios from the existing cache.

Reuses cached Tickertape metrics + scheme codes (the expensive part) and just
re-derives Upside/Downside Capture from NAV. Use this after a benchmark change
instead of a full rebuild — it never re-fetches Tickertape.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import engine

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache.json"


def main():
    d = json.loads(CACHE.read_text())
    funds = d["funds"]

    # benchmark series, computed once
    bench = {}
    for c in {engine._NIFTY50, engine._NIFTY500, engine._MIDCAP150, engine._SMALLCAP250,
              engine._GSEC, engine._SHORTDEBT, engine._GOLD}:
        try:
            bench[c] = engine.monthly_returns(c)
        except Exception:  # noqa: BLE001
            bench[c] = None

    def recompute(f):
        scheme = f.get("scheme") or engine.scheme_for(f)
        b = bench.get(engine.benchmark_for_category(f.get("category")))
        up = dn = None
        if scheme and b:
            try:
                up, dn = engine.capture_from_series(engine.monthly_returns(scheme), b)
            except Exception:  # noqa: BLE001
                pass
        f["upCap"], f["downCap"], f["scheme"] = up, dn, scheme
        return f

    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(recompute, funds))

    CACHE.write_text(json.dumps(d, indent=1))
    print("recomputed captures:", sum(1 for f in funds if f.get("upCap") is not None), "/", len(funds))
    gold = [f for f in funds if "gold" in (f.get("category") or "").lower()]
    for g in gold[:5]:
        print(f"  {g['name'][:40]:40} up={g.get('upCap')} dn={g.get('downCap')}")


if __name__ == "__main__":
    main()
