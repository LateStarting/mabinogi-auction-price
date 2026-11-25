"""마비노기 경매장 수집기.

사용: NEXON_API_KEY=... python collector/collect.py --data <data 디렉터리>

data 디렉터리 구조
  state.json                      호출 예산·마지막 수집 시각
  history/YYYY-MM-DD.jsonl.gz     체결 내역 (KST 날짜별, auction_buy_id 중복 제거)
  listings_latest.json.gz         가장 최근 매물 스냅샷
"""
import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402

BASE = "https://open.api.nexon.com/mabinogi/v1/auction/"
KST = dt.timezone(dt.timedelta(hours=9))


class BudgetExceeded(Exception):
    pass


class Client:
    def __init__(self, key, state):
        self.key = key
        self.state = state
        self.last_call = 0.0
        self.calls = 0

    def _budget(self):
        today = dt.datetime.now(KST).date().isoformat()
        b = self.state.setdefault("budget", {"date": today, "used": 0})
        if b["date"] != today:
            b.update(date=today, used=0)
        return b

    def get(self, endpoint, **params):
        b = self._budget()
        if b["used"] >= config.DAILY_BUDGET:
            raise BudgetExceeded(f"오늘 호출 예산 소진 ({b['used']}/{config.DAILY_BUDGET})")
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        req = urllib.request.Request(BASE + endpoint + "?" + q, headers={"x-nxopen-api-key": self.key})
        for attempt in range(4):
            wait = config.MIN_CALL_GAP_SEC - (time.time() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()
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
            d = self.get(endpoint, auction_item_category=category, cursor=cursor)
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


def collect_history(client, data):
    new_rows = {}
    for cat in config.CATEGORIES:
        for h in client.sweep("history", cat, "auction_history"):
            ts = dt.datetime.fromisoformat(h["date_auction_buy"].replace("Z", "+00:00"))
            day = ts.astimezone(KST).date().isoformat()
            new_rows.setdefault(day, []).append({
                "id": h["auction_buy_id"],
                "key": item_key(h),
                "cat": h["auction_item_category"],
                "price": h["auction_price_per_unit"],
                "count": h["item_count"],
                "ts": ts.isoformat(),
            })
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


def collect_listings(client, data):
    rows = []
    for cat in config.CATEGORIES:
        for it in client.sweep("list", cat, "auction_item"):
            rows.append({
                "key": item_key(it),
                "cat": it["auction_item_category"],
                "price": it["auction_price_per_unit"],
                "count": it["item_count"],
                "expire": it["date_auction_expire"],
            })
    snap = {"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(), "items": rows}
    with gzip.open(data / "listings_latest.json.gz", "wt", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    return len(rows)


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
    ap.add_argument("--force", choices=["history", "listings", "both"], help="주기와 상관없이 실행")
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

    do_hist = args.force in ("history", "both") or minutes_since(state.get("last_history")) >= config.HISTORY_INTERVAL_MIN - 2
    do_list = args.force in ("listings", "both") or minutes_since(state.get("last_listings")) >= config.LISTINGS_INTERVAL_MIN - 2
    report = {}
    try:
        if do_hist:
            report["history_added"] = collect_history(client, data)
            state["last_history"] = now
        if do_list:
            report["listings"] = collect_listings(client, data)
            state["last_listings"] = now
    except BudgetExceeded as e:
        report["stopped"] = str(e)
    finally:
        report["pruned_days"] = prune(data)
        report["calls"] = client.calls
        report["budget"] = state.get("budget")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
