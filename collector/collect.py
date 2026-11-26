"""마비노기 경매장 수집기.

사용: NEXON_API_KEY=... python collector/collect.py --data <data 디렉터리>

data 디렉터리 구조
  state.json                      호출 예산·마지막 수집 시각
  history/YYYY-MM-DD.jsonl.gz     체결 내역 (KST 날짜별, auction_buy_id 중복 제거)
  listings_latest.json.gz         가장 최근 매물 스냅샷
  npc_latest.json                 NPC 상점 고정 품목 스냅샷 (하루 1회)
  lookup.json                     NPC 품목별 경매장 최저 매물가 (이름 조회)
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402

BASE = "https://open.api.nexon.com/mabinogi/v1/"
KST = dt.timezone(dt.timedelta(hours=9))


class BudgetExceeded(Exception):
    pass


class Client:
    def __init__(self, key, state):
        self.key = key
        self.state = state
        self.last_call = 0.0
        self.calls = 0

    def _budgets(self):
        """KST 하루와 UTC 하루 두 기준의 사용량. 둘 중 하나라도 예산을 넘으면 멈춘다."""
        now = dt.datetime.now(dt.timezone.utc)
        out = []
        for name, tz in (("budget", KST), ("budget_utc", dt.timezone.utc)):
            today = now.astimezone(tz).date().isoformat()
            b = self.state.setdefault(name, {"date": today, "used": 0})
            if b["date"] != today:
                b.update(date=today, used=0)
            prefix = "kst" if tz is KST else "utc"
            limit = config.BUDGET_OVERRIDES.get(f"{prefix}:{today}", config.DAILY_BUDGET)
            out.append((b, limit, prefix))
        return out

    def get(self, endpoint, **params):
        budgets = self._budgets()
        for b, limit, prefix in budgets:
            if b["used"] >= limit:
                raise BudgetExceeded(f"호출 예산 소진 ({prefix} {b['date']}: {b['used']}/{limit})")
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        req = urllib.request.Request(BASE + endpoint + "?" + q, headers={"x-nxopen-api-key": self.key})
        for attempt in range(4):
            wait = config.MIN_CALL_GAP_SEC - (time.time() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()
            for b, _, _ in budgets:
                b["used"] += 1
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:200]
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 ** attempt * 5)
                    continue
                raise RuntimeError(f"{endpoint} {params} → HTTP {e.code} {body}")
            except urllib.error.URLError:
                time.sleep(2 ** attempt * 5)
        raise RuntimeError(f"{endpoint} {params} 재시도 실패")

    def sweep(self, endpoint, category, list_key):
        cursor, out = None, []
        while True:
            d = self.get("auction/" + endpoint, auction_item_category=category, cursor=cursor)
            out.extend(d.get(list_key) or [])
            cursor = d.get("next_cursor")
            if not cursor:
                return out


# 값에 영향이 없거나 이미 display_name 에 반영된 옵션
IGNORED_OPTIONS = {"내구도", "남은 거래 횟수", "인챈트 종류"}


def item_key(it):
    """시세 비교 단위. 인챈트 스크롤은 display_name 에 종류가 들어 있고,
    색상·품질·의장 등급처럼 값에 영향을 주는 옵션은 키에 붙인다."""
    name = it.get("item_display_name") or it.get("item_name")
    extra = []
    for o in it.get("item_option") or []:
        t = o.get("option_type")
        if t in IGNORED_OPTIONS:
            continue
        v = "/".join(str(x) for x in (o.get("option_sub_type"), o.get("option_value"), o.get("option_value2")) if x)
        extra.append(f"{t}:{v}")
    return name + (" [" + ", ".join(sorted(extra)) + "]" if extra else "")


_RANK_RE = re.compile(r"랭크\s*([0-9A-F])")


def enchant_rank(it):
    """인챈트 스크롤의 랭크 (옵션 '인챈트 종류' 값 예: '곤란의 (랭크 A)'). 없으면 None."""
    for o in it.get("item_option") or []:
        if o.get("option_type") == "인챈트 종류":
            m = _RANK_RE.search(str(o.get("option_value") or ""))
            if m:
                return m.group(1)
    return None


def with_rank(row, it):
    r = enchant_rank(it)
    if r:
        row["rank"] = r
    return row


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def minutes_since(iso):
    if not iso:
        return 1e9
    return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds() / 60


def npc_extra_categories(data):
    """거래 가능한 NPC 품목이 많이 속한 경매장 카테고리 (기본 카테고리 제외, 최대 N개).
    NPC_HISTORY_FROM 이전이면 빈 목록."""
    if dt.datetime.now(KST).date().isoformat() < config.NPC_HISTORY_FROM:
        return []
    lookup = load_json(data / "lookup.json", {})
    counts = {}
    for e in lookup.values():
        cat = e.get("category")
        if cat and cat not in config.CATEGORIES and e.get("n", 0) >= config.NPC_HISTORY_MIN_LISTINGS:
            counts[cat] = counts.get(cat, 0) + 1
    return [c for c, _ in sorted(counts.items(), key=lambda x: -x[1])[: config.NPC_HISTORY_MAX_CATS]]


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def collect_history(client, data, extra=(), fetched=None):
    """fetched: 카테고리 → 가져온 시각(UTC ISO)을 채워 줄 dict (전부 저장된 뒤 state 에 반영)."""
    new_rows = {}
    for cat in list(config.CATEGORIES) + [c for c in extra if c not in config.CATEGORIES]:
        if fetched is not None:
            fetched[cat] = now_iso()
        for h in client.sweep("history", cat, "auction_history"):
            ts = dt.datetime.fromisoformat(h["date_auction_buy"].replace("Z", "+00:00"))
            day = ts.astimezone(KST).date().isoformat()
            new_rows.setdefault(day, []).append(with_rank({
                "id": h["auction_buy_id"],
                "key": item_key(h),
                "cat": h["auction_item_category"],
                "price": h["auction_price_per_unit"],
                "count": h["item_count"],
                "ts": ts.isoformat(),
            }, h))
    added = 0
    hdir = data / "history"
    hdir.mkdir(parents=True, exist_ok=True)
    for day, rows in new_rows.items():
        path = hdir / f"{day}.jsonl.gz"
        existing, seen = [], set()
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as f:
                existing = [json.loads(line) for line in f]
            seen = {r["id"] for r in existing}
        fresh = [r for r in rows if r["id"] not in seen]
        if not fresh:
            continue
        seen.update(r["id"] for r in fresh)
        added += len(fresh)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            for r in existing + fresh:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return added


def collect_listings(client, data, fetched=None):
    rows = []
    for cat in config.CATEGORIES:
        if fetched is not None:
            fetched[cat] = now_iso()
        for it in client.sweep("list", cat, "auction_item"):
            rows.append(with_rank({
                "key": item_key(it),
                "cat": it["auction_item_category"],
                "price": it["auction_price_per_unit"],
                "count": it["item_count"],
                "expire": it["date_auction_expire"],
            }, it))
    snap = {"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(), "items": rows}
    with gzip.open(data / "listings_latest.json.gz", "wt", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    return len(rows)


def collect_npc(client, data):
    """NPC 상점 고정 품목 스냅샷 (서버·채널 1곳, 채널별로 바뀌는 탭 제외)."""
    rows = []
    for npc in config.NPC_NAMES:
        d = client.get("npcshop/list", npc_name=npc, server_name=config.NPC_SERVER, channel=config.NPC_CHANNEL)
        for tab in d.get("shop") or []:
            if tab["tab_name"] in config.NPC_EXCLUDE_TABS:
                continue
            for it in tab["item"]:
                for p in it.get("price") or []:
                    rows.append({
                        "npc": npc,
                        "tab": tab["tab_name"],
                        "name": it["item_display_name"],
                        "count": it.get("item_count") or 1,
                        "currency": p["price_type"],
                        "price": p["price_value"],
                        "limit_type": it.get("limit_type"),
                        "limit_value": it.get("limit_value"),
                    })
    snap = {"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(), "items": rows}
    with open(data / "npc_latest.json", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    return len(rows)


def collect_lookups(client, data):
    """NPC 품목을 경매장에서 이름으로 조회해 최저 매물가를 기록. 주기가 된 품목만, 한 번에 일부씩."""
    snap = load_json(data / "npc_latest.json", None)
    if not snap:
        return 0
    path = data / "lookup.json"
    lookup = load_json(path, {})
    names = sorted({r["name"] for r in snap["items"]})
    lookup = {k: v for k, v in lookup.items() if k in names}

    def due(name):
        e = lookup.get(name)
        if not e:
            return True
        wait = config.LOOKUP_INTERVAL_MIN if e["n"] else config.LOOKUP_EMPTY_RETRY_MIN
        return minutes_since(e["checked_at"]) >= wait - 2

    todo = sorted((n for n in names if due(n)), key=lambda n: lookup.get(n, {}).get("checked_at", ""))
    done = 0
    try:
        for name in todo[: config.LOOKUP_PER_RUN]:
            d = client.get("auction/list", item_name=name)
            items = d.get("auction_item") or []
            prices = sorted(it["auction_price_per_unit"] for it in items)
            lookup[name] = {
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "n": len(items),
                "more": bool(d.get("next_cursor")),
                "lowest": prices[:5],
                "category": items[0]["auction_item_category"] if items else None,
            }
            done += 1
    finally:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lookup, f, ensure_ascii=False)
    return done


def prune(data):
    cutoff = (dt.datetime.now(KST) - dt.timedelta(days=config.RETENTION_DAYS)).date().isoformat()
    removed = 0
    for p in (data / "history").glob("*.jsonl.gz"):
        if p.name[:10] < cutoff:
            p.unlink()
            removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--force", choices=["history", "listings", "both", "npc", "all"], help="주기와 상관없이 실행")
    args = ap.parse_args()

    key = os.environ.get("NEXON_API_KEY")
    if not key:
        sys.exit("NEXON_API_KEY 환경변수가 없습니다")
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    state_path = data / "state.json"
    state = load_json(state_path, {})
    client = Client(key, state)
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    f = args.force
    do_hist = f in ("history", "both", "all") or minutes_since(state.get("last_history")) >= config.HISTORY_INTERVAL_MIN - 2
    do_list = f in ("listings", "both", "all") or minutes_since(state.get("last_listings")) >= config.LISTINGS_INTERVAL_MIN - 2
    do_npc = f in ("npc", "all") or minutes_since(state.get("last_npc")) >= config.NPC_INTERVAL_MIN - 2
    report = {}
    # 우선순위: 거래 내역(놓치면 복구 불가) → 매물 → NPC 상점 → 이름 조회
    try:
        if do_hist:
            extra = npc_extra_categories(data)
            report["history_extra_categories"] = extra
            got = {}
            report["history_added"] = collect_history(client, data, extra, got)
            state["last_history"] = now
            state.setdefault("cat_history_at", {}).update(got)
            state["history_extra_categories"] = extra
        if do_list:
            got = {}
            report["listings"] = collect_listings(client, data, got)
            state["last_listings"] = now
            state.setdefault("cat_listings_at", {}).update(got)
        if do_npc:
            report["npc_items"] = collect_npc(client, data)
            state["last_npc"] = now
        report["lookups"] = collect_lookups(client, data)
    except BudgetExceeded as e:
        report["stopped"] = str(e)
    finally:
        report["pruned_days"] = prune(data)
        report["calls"] = client.calls
        report["budget"] = {"kst": state.get("budget"), "utc": state.get("budget_utc")}
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
