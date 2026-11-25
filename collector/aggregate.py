"""수집 데이터를 시세표용 prices.json 으로 집계.

사용: python collector/aggregate.py --data <data 디렉터리> --out site/prices.json
"""
import argparse
import datetime as dt
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402

KST = dt.timezone(dt.timedelta(hours=9))


def load_history(data, days):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    rows = []
    for p in sorted((data / "history").glob("*.jsonl.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if dt.datetime.fromisoformat(r["ts"]) >= since:
                    rows.append(r)
    return rows


def weighted_median(pairs):
    """(가격, 수량) 목록의 수량 가중 중앙값."""
    pairs = sorted(pairs)
    total = sum(c for _, c in pairs)
    acc = 0
    for price, c in pairs:
        acc += c
        if acc * 2 >= total:
            return price
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data = Path(args.data)

    hist = load_history(data, config.BASELINE_DAYS)
    first_ts = min((r["ts"] for r in hist), default=None)
    span_days = 0
    if first_ts:
        span = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(first_ts)
        span_days = max(span.total_seconds() / 86400, 1 / 24)

    by_key = {}
    for r in hist:
        e = by_key.setdefault(r["key"], {"cat": r["cat"], "trades": [], "last": None})
        e["trades"].append((r["price"], r["count"]))
        if e["last"] is None or r["ts"] > e["last"][0]:
            e["last"] = (r["ts"], r["price"])

    snap = {"collected_at": None, "items": []}
    lp = data / "listings_latest.json.gz"
    if lp.exists():
        with gzip.open(lp, "rt", encoding="utf-8") as f:
            snap = json.load(f)
    now = dt.datetime.now(dt.timezone.utc)
    listings = {}
    for it in snap["items"]:
        exp = dt.datetime.fromisoformat(it["expire"].replace("Z", "+00:00"))
        if exp <= now:
            continue
        listings.setdefault(it["key"], {"cat": it["cat"], "prices": []})["prices"].append((it["price"], it["count"]))

    items = []
    for key in set(by_key) | set(listings):
        h = by_key.get(key)
        l = listings.get(key)
        cat = (h or l)["cat"]
        row = {"key": key, "cat": cat}
        if h:
            n = len(h["trades"])
            row["trades"] = n
            row["volume"] = sum(c for _, c in h["trades"])
            # 수집 기간이 하루 미만이면 일평균은 왜곡되므로 내지 않음
            row["per_day"] = round(n / span_days, 1) if span_days >= 1 else None
            row["last_price"] = h["last"][1]
            row["last_ts"] = h["last"][0]
            if n >= config.MIN_TRADES:
                row["median"] = weighted_median(h["trades"])
                prices = [p for p, _ in h["trades"]]
                row["p25"] = statistics.quantiles(prices, n=4)[0] if n >= 4 else None
        if l:
            prices = sorted(l["prices"])
            row["min_listing"] = prices[0][0]
            row["listings"] = len(prices)
            row["listed_qty"] = sum(c for _, c in prices)
        med = row.get("median")
        if med and row.get("min_listing"):
            row["discount"] = round(1 - row["min_listing"] / med, 4)
            threshold = med * (1 - config.FEE_RATE) * (1 - config.MIN_MARGIN)
            cheap = [(p, c) for p, c in l["prices"] if p <= threshold]
            if cheap:
                row["deal_count"] = len(cheap)
                row["deal_profit"] = int(sum((med * (1 - config.FEE_RATE) - p) * c for p, c in cheap))
        items.append(row)

    items.sort(key=lambda r: (-(r.get("deal_profit") or 0), -(r.get("trades") or 0)))
    out = {
        "generated_at": now.isoformat(),
        "listings_at": snap.get("collected_at"),
        "history_days": round(span_days, 2),
        "trade_rows": len(hist),
        "categories": config.CATEGORIES,
        "params": {
            "baseline_days": config.BASELINE_DAYS,
            "min_trades": config.MIN_TRADES,
            "fee_rate": config.FEE_RATE,
            "min_margin": config.MIN_MARGIN,
        },
        "items": items,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"items": len(items), "trade_rows": len(hist),
                      "deals": sum(1 for r in items if r.get("deal_count"))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
