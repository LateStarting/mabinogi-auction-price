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
            r["listings_n"] = lk["n"]
            r["checked_at"] = lk["checked_at"]
            r["tradable"] = lk["n"] > 0 or (m is not None)
            r["category"] = lk.get("category")
            if len(lk["lowest"]) >= 2:
                # 최저가와 두 번째 가격 차이 (최저 매물 하나만 튀는지 확인용)
                r["gap2"] = round(lk["lowest"][1] / lk["lowest"][0] - 1, 3) if lk["lowest"][0] else None
        if m:
            r["trades"] = m.get("trades")
            r["per_day"] = m.get("per_day")
        if unit:
            value = unit * it["count"]
            net = value * (1 - config.FEE_RATE)
            r.update(unit_price=unit, basis=basis, value=value)
            if it["currency"] == "골드":
                r["profit"] = int(net - it["price"])
                r["ratio"] = round(net / it["price"], 3) if it["price"] else None
            else:
                r["gold_per"] = round(net / it["price"], 3) if it["price"] else None
    out = list(rows.values())
    rates = assess(out)
    apply_checks(out, load_confirmed())
    out.sort(key=lambda r: -(r.get("gold_per") or r.get("ratio") or 0))
    return {
        "collected_at": snap["collected_at"],
        "server": config.NPC_SERVER, "channel": config.NPC_CHANNEL,
        "excluded_tabs": sorted(config.NPC_EXCLUDE_TABS),
        "checked": len(lookup), "total": len({r["name"] for r in out}),
        "rates": rates,
        "rules": {
            "highlight_mult": HIGHLIGHT_MULT, "min_listings": MIN_LISTINGS,
            "gold_min_ratio": GOLD_MIN_RATIO, "fee_rate": config.FEE_RATE, "rate_min_n": RATE_MIN_N,
            "candidate_min_per_day": config.CANDIDATE_MIN_PER_DAY,
        },
        "items": out,
    }


# --- 하이라이트 기준 ---
HIGHLIGHT_MULT = 1.5     # 화폐별 기준 환율의 1.5배 이상이면 "주목"
MIN_LISTINGS = 3         # 경매장 매물이 이보다 적으면 가격을 믿지 않음
GOLD_MIN_RATIO = 1.2     # 골드 판매품: 수수료 뺀 경매장 가치가 NPC 가격의 1.2배 이상
HIGH_GAP2 = 0.3          # 최저가가 두 번째 가격보다 30% 넘게 싸면 "최저가 하나만 튐"
RATE_MIN_N = 5           # 기준 환율을 믿으려면 믿을 만한 품목이 최소 이만큼


def assess(rows):
    """화폐별 기준 환율(믿을 만한 품목의 효율 중앙값)을 구하고, 품목마다 등급·근거·주의를 붙인다."""
    def reliable(r):
        return r.get("value") is not None and (r.get("basis") == "시세" or (r.get("listings_n") or 0) >= MIN_LISTINGS)

    rates = {}
    for cur in {r["currency"] for r in rows if r["currency"] != "골드"}:
        effs = sorted(r["gold_per"] for r in rows if r["currency"] == cur and reliable(r) and r.get("gold_per"))
        if effs:
            # 믿을 만한 품목이 RATE_MIN_N 개 미만이면 기준 환율도 믿지 않음 (하이라이트 안 함)
            rates[cur] = {"median": round(statistics.median(effs), 3), "n": len(effs), "ok": len(effs) >= RATE_MIN_N}

    for r in rows:
        if r.get("value") is None:
            continue
        why, warn = [], []
        lst, basis = r.get("listings_n"), r.get("basis")
        # 가격 근거
        if basis == "시세":
            why.append(f"가격 근거: 실제 체결 중앙값(체결 {r.get('trades')}건)")
        else:
            warn.append("가격 근거가 '내놓은 최저가'라 실제로 그 값에 팔린다는 보장은 없음")
            if r.get("trades") is None:
                warn.append("체결 기록 미수집 → 팔리는 속도 모름")
        if lst is not None:
            if lst >= 10:
                why.append(f"경매장 매물 {r['listings']}개 → 거래되는 품목(가격 비교 가능)")
            elif lst >= MIN_LISTINGS:
                why.append(f"경매장 매물 {lst}개")
            else:
                warn.append(f"경매장 매물 {lst}개뿐 → 가격 신뢰 낮음")
        if r.get("gap2") is not None and r["gap2"] > HIGH_GAP2:
            warn.append(f"최저가가 두 번째 매물보다 {round(r['gap2'] * 100)}% 쌈 → 실제 시장가는 더 높을 수 있음")
        if len(r["npcs"]) > 3:
            why.append(f"NPC {len(r['npcs'])}명이 판매 → 서버·채널 상관없이 구매 가능")
        if r.get("limit"):
            warn.append(f"구매 제한 {r['limit']} → 반복 이익은 제한됨")
        else:
            why.append("구매 제한 없음")
        warn.append("NPC에서 산 물건의 재거래(귀속) 여부·경매장 매물의 옵션 차이는 게임에서 확인 필요")

        # 효율 판단
        score = None
        if r["currency"] == "골드":
            ratio = r.get("ratio")
            if ratio:
                score = ratio
                why.insert(0, f"수수료 뺀 경매장 가치가 NPC 가격의 {ratio:.1f}배 (개당 {r['profit']:,}골드 남음)")
                hot = ratio >= GOLD_MIN_RATIO
        else:
            rate = rates.get(r["currency"], {})
            base = rate.get("median")
            if base and r.get("gold_per"):
                score = r["gold_per"] / base
                r["vs_rate"] = round(score, 2)
                why.insert(0, f"{r['currency']} 1개 = {r['gold_per']:,}골드 — 기준 환율({base:,})의 {score:.1f}배")
                hot = score >= HIGHLIGHT_MULT and rate.get("ok")
                if not rate.get("ok"):
                    warn.insert(0, f"{r['currency']} 기준 환율이 품목 {rate['n']}개로만 계산돼 비교가 불안정함")
            else:
                hot = False
        trust = reliable(r) and not (r.get("gap2") is not None and r["gap2"] > HIGH_GAP2)
        if score is None:
            continue
        # 등급: A = 체결 시세 기반, B = 매물 충분, C = 매물 부족·최저가 튐
        grade = "A" if basis == "시세" else ("B" if trust else "C")
        r["grade"] = grade
        r["hot"] = bool(hot and grade in ("A", "B"))
        r["why"], r["warn"] = why, warn
    return rates


def load_confirmed():
    p = Path(__file__).parent / "confirmed.json"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f).get("items", {})


def apply_checks(rows, confirmed):
    """사용자 확인 결과를 붙이고, 골드 판매품은 '무위험에 가까운 후보' 4개 조건을 검사한다."""
    for r in rows:
        c = confirmed.get(r["name"])
        if c:
            r["confirmed"] = {"tradable": c.get("tradable"), "checked": c.get("checked"), "note": c.get("note")}
            if c.get("tradable") is False:
                r["hot"] = False
                if r.get("warn") is not None:
                    r["warn"].insert(0, f"게임에서 재거래 불가로 확인됨({c.get('checked') or ''})")
        if r["currency"] != "골드" or r.get("ratio") is None:
            continue
        per_day = r.get("per_day")
        checks = [
            ["골드로 구매 제한 없이 삼", not r.get("limit")],
            [f"체결 중앙값 기준 수수료 뺀 가치 ≥ NPC 가격 × {GOLD_MIN_RATIO}",
             r.get("basis") == "시세" and r["ratio"] >= GOLD_MIN_RATIO],
            [f"하루 체결 {config.CANDIDATE_MIN_PER_DAY}건 이상"
             + (f" (현재 {per_day}건)" if per_day is not None else f" (체결 {r.get('trades') or 0}건, 하루치 미만)"),
             per_day is not None and per_day >= config.CANDIDATE_MIN_PER_DAY],
            ["게임에서 재거래 가능 확인", bool(c and c.get("tradable"))],
        ]
        r["checks"] = checks
        r["candidate"] = all(ok for _, ok in checks)


RANK_ORDER = "FEDCBA987654321"


def build_enchant(market, ranks):
    """인챈트 스크롤별 '성공 1번에 드는 기대 재료비'와 실패 시 장비 손실 배수.

    가정: 시도 1회 = 스크롤 1장 + 마법가루 1개. 성공할 때까지 반복(기하분포) →
      기대 시도 횟수 = 1/p, 기대 재료비 = (스크롤 + 마법가루) / p.
    실패하면 더 이상 인챈트 불가인 랭크는 실패마다 바탕 장비를 새로 구해야 하므로
      기대 장비 소모 = 1/p개 (성공한 1개 포함). 그 외 랭크는 장비 1개(실패 시 내구도만 감소).
    본전 판매가(인챈트된 장비) = (기대 재료비 + 장비가 × 장비 소모) / (1 − 수수료).
    """
    powder = market.get(config.ENCHANT_POWDER) or {}
    powder_price = powder.get("median") or powder.get("min_listing")
    powder_basis = "시세" if powder.get("median") else ("최저 매물" if powder.get("min_listing") else None)
    rows = []
    for key, rank in ranks.items():
        m = market.get(key)
        if not m:
            continue
        scroll = m.get("median") or m.get("min_listing")
        if not scroll:
            continue
        row = {
            "key": key, "rank": rank,
            "scroll": scroll, "scroll_basis": "시세" if m.get("median") else "최저 매물",
            "scroll_trades": m.get("trades"), "scroll_listings": m.get("listings"),
            "ruin": rank in config.ENCHANT_RUIN_RANKS,
            "penalty_unknown": rank in config.ENCHANT_PENALTY_UNKNOWN,
        }
        if powder_price:
            per_try = scroll + powder_price
            for day, table in config.ENCHANT_RATES.items():
                p = table.get(rank)
                if not p:
                    continue
                tag = "wd" if day == "평일" else "th"
                row[f"p_{tag}"] = p
                row[f"cost_{tag}"] = int(round(per_try / p))
                # 장비 소모 개수: 장비를 잃는 랭크(미확인 포함)는 1/p, 아니면 1
                row[f"items_{tag}"] = round(1 / p, 2) if (row["ruin"] or row["penalty_unknown"]) else 1
        rows.append(row)
    rows.sort(key=lambda r: (RANK_ORDER.index(r["rank"]) if r["rank"] in RANK_ORDER else 99, r["key"]))
    return {
        "powder": config.ENCHANT_POWDER, "powder_price": powder_price, "powder_basis": powder_basis,
        "fee_rate": config.FEE_RATE,
        "rates": config.ENCHANT_RATES,
        "ruin_ranks": sorted(config.ENCHANT_RUIN_RANKS), "unknown_ranks": sorted(config.ENCHANT_PENALTY_UNKNOWN),
        "items": rows,
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
    ranks = {}  # 인챈트 스크롤 키 → 랭크
    for r in hist:
        if r.get("rank"):
            ranks[r["key"]] = r["rank"]
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
        if it.get("rank"):
            ranks[it["key"]] = it["rank"]
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
        # 기본 카테고리 + NPC 품목 때문에 추가로 체결을 모은 카테고리
        "categories": list(config.CATEGORIES) + sorted(
            {r["cat"] for r in items if r.get("trades") and r["cat"] not in config.CATEGORIES}),
        "params": {
            "baseline_days": config.BASELINE_DAYS,
            "min_trades": config.MIN_TRADES,
            "fee_rate": config.FEE_RATE,
            "min_margin": config.MIN_MARGIN,
        },
        "items": items,
        "npc": npc,
        "enchant": build_enchant({r["key"]: r for r in items}, ranks),
        "recent": recent_feed,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"items": len(items), "trade_rows": len(hist),
                      "deals": sum(1 for r in items if r.get("deal_count"))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
