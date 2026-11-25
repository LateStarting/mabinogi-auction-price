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
RECENT_PER_ITEM = 10   # 행 상세에 보여 줄 품목별 최근 체결 수
RECENT_FEED = 300      # 최근 체결 탭에 보여 줄 전체 최신 체결 수
CHART_POINTS = 60      # 행 상세 차트에 찍을 품목별 최근 체결 수


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


def build_npc(data, market):
    """NPC 상점 품목별 경매장 가치와 화폐 1개당 골드(효율)."""
    snap_path, lookup_path = data / "npc_latest.json", data / "lookup.json"
    if not snap_path.exists():
        return None
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)
    lookup = {}
    if lookup_path.exists():
        with open(lookup_path, encoding="utf-8") as f:
            lookup = json.load(f)

    rows = {}
    for it in snap["items"]:
        k = (it["name"], it["currency"], it["price"], it["count"])
        r = rows.get(k)
        if r:
            if it["npc"] not in r["npcs"]:
                r["npcs"].append(it["npc"])
            continue
        r = rows[k] = {
            "name": it["name"], "tab": it["tab"], "npcs": [it["npc"]], "count": it["count"],
            "currency": it["currency"], "price": it["price"],
            "limit": f'{it["limit_type"]} {it["limit_value"]}개' if it.get("limit_type") else None,
        }
        m = market.get(it["name"])
        lk = lookup.get(it["name"])
        unit, basis = None, None
        if m and m.get("median"):
            unit, basis = m["median"], "시세"
        elif lk and lk["n"]:
            unit, basis = lk["lowest"][0], "최저 매물"
        if lk:
            r["listings"] = lk["n"] if not lk.get("more") else f'{lk["n"]}+'
            r["checked_at"] = lk["checked_at"]
            r["tradable"] = lk["n"] > 0 or (m is not None)
        if unit:
            value = unit * it["count"]
            net = value * (1 - config.FEE_RATE)
            r.update(unit_price=unit, basis=basis, value=value)
            if it["currency"] == "골드":
                r["profit"] = int(net - it["price"])
                r["ratio"] = round(net / it["price"], 3) if it["price"] else None
            else:
                r["gold_per"] = round(net / it["price"], 3) if it["price"] else None
    out = sorted(rows.values(), key=lambda r: -(r.get("gold_per") or r.get("ratio") or 0))
    return {
        "collected_at": snap["collected_at"],
        "server": config.NPC_SERVER, "channel": config.NPC_CHANNEL,
        "excluded_tabs": sorted(config.NPC_EXCLUDE_TABS),
        "checked": len(lookup), "total": len({r["name"] for r in out}),
        "items": out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data = Path(args.data)

    hist = load_history(data, config.BASELINE_DAYS)
    first_ts = min((r["ts"] for r in hist), default=None)
    last_ts = max((r["ts"] for r in hist), default=None)
    span_days = 0
    if first_ts:
        span = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(first_ts)
        span_days = max(span.total_seconds() / 86400, 1 / 24)

    by_key = {}
    for r in hist:
        e = by_key.setdefault(r["key"], {"cat": r["cat"], "trades": [], "last": None, "days": {}, "recent": []})
        e["trades"].append((r["price"], r["count"]))
        e["recent"].append((r["ts"], r["price"], r["count"]))
        day = dt.datetime.fromisoformat(r["ts"]).astimezone(KST).strftime("%m/%d")
        e["days"].setdefault(day, []).append((r["price"], r["count"]))
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
        listings.setdefault(it["key"], {"cat": it["cat"], "prices": [], "rows": []})
        listings[it["key"]]["prices"].append((it["price"], it["count"]))
        listings[it["key"]]["rows"].append((it["price"], it["count"], it["expire"]))

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
                # 상세 보기용: 일별(KST) 중앙값·체결 건수
                row["daily"] = [[d, weighted_median(t), len(t)] for d, t in sorted(h["days"].items())]
                # 상세 보기용: 최근 체결 10건 (시각, 가격, 수량)
                latest = sorted(h["recent"], reverse=True)
                row["recent"] = latest[:RECENT_PER_ITEM]
                # 상세 차트용: 최근 체결 최대 60건, [유닉스 분, 가격, 수량] (용량 줄이려고 숫자만)
                row["chart"] = [[int(dt.datetime.fromisoformat(t).timestamp() // 60), p, c]
                                for t, p, c in reversed(latest[:CHART_POINTS])]
        if l:
            prices = sorted(l["prices"])
            row["min_listing"] = prices[0][0]
            row["listings"] = len(prices)
            row["listed_qty"] = sum(c for _, c in prices)
            if h and row.get("median"):
                # 상세 보기용: 가장 싼 매물 5개 (가격, 수량, 만료)
                row["cheapest"] = sorted(l["rows"])[:5]
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
    npc = build_npc(data, {r["key"]: r for r in items})

    # 최근 체결 탭: 전체 품목의 최신 체결 (시각, 품목, 카테고리, 가격, 수량, 시세)
    med_of = {r["key"]: r.get("median") for r in items}
    feed = sorted(hist, key=lambda r: r["ts"], reverse=True)[:RECENT_FEED]
    recent_feed = [[r["ts"], r["key"], r["cat"], r["price"], r["count"], med_of.get(r["key"])] for r in feed]
    out = {
        "generated_at": now.isoformat(),
        "listings_at": snap.get("collected_at"),
        "history_days": round(span_days, 2),
        "history_from": first_ts,   # 집계에 들어간 가장 이른 체결 시각
        "history_to": last_ts,      # 가장 늦은 체결 시각
        "trade_rows": len(hist),
        "categories": config.CATEGORIES,
        "params": {
            "baseline_days": config.BASELINE_DAYS,
            "min_trades": config.MIN_TRADES,
            "fee_rate": config.FEE_RATE,
            "min_margin": config.MIN_MARGIN,
        },
        "items": items,
        "npc": npc,
        "recent": recent_feed,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"items": len(items), "trade_rows": len(hist),
                      "deals": sum(1 for r in items if r.get("deal_count"))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
