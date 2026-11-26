# 마비노기 경매장 시세표

https://latestarting.github.io/mabinogi-auction-price/

NEXON Open API의 마비노기 경매장·NPC 상점 데이터로 품목별 시세(최근 7일 체결가 중앙값), 최저 매물가, 저평가 매물, NPC 상점 품목의 두카트 효율을 보여 주는 정적 사이트입니다. 비영리 개인 프로젝트이며 넥슨과 관련이 없습니다.

Data based on NEXON Open API.

## 구조

| 파일 | 역할 |
|---|---|
| `collector/config.py` | 대상 카테고리, 수집 주기, 호출 예산, NPC 설정, 계산 기준 |
| `collector/collect.py` | 수집. 우선순위: 거래 내역 → 현재 매물 → NPC 상점 → NPC 품목 경매장 이름 조회 |
| `collector/aggregate.py` | 수집 데이터를 `site/prices.json`으로 집계 (`items` + `recent` + `npc` + `enchant`) |
| `site/index.html` | 정적 페이지. 탭: 경매장 시세 / 최근 체결 (`#recent`) / NPC 상점 효율 (`#npc`) / 인챈트 본전 (`#enchant`) |
| `.github/workflows/collect.yml` | 15분 cron + `main` 푸시 + 수동 실행. build job → deploy job |

마비노기 경매장은 전 서버 통합이라 서버 구분 없이 집계합니다.

## 수집 주기와 호출 예산 (개발 단계 키: 하루 1,000건)

| 작업 | 주기 | 하루 |
|---|---|---|
| 거래 내역 (최근 1시간만 제공, 12개 + NPC 카테고리 최대 4개) | 45분 | 약 560건 |
| 현재 매물 | 12시간 | 약 130건 |
| NPC 상점 21명 (류트 1채널, 고정 품목) | 24시간 | 21건 |
| NPC 품목 경매장 이름 조회 | 품목당 24시간, 매물 없으면 7일 | 약 50~150건 |

- 예산 카운터는 `state.json`에 KST 하루(`budget`)와 UTC 하루(`budget_utc`)를 모두 기록하고, 둘 중 하나라도 `DAILY_BUDGET`(950)에 닿으면 멈춥니다. 넥슨의 리셋 기준 시각을 몰라서 두 기준을 다 봅니다.
- 특정 날짜만 상한을 바꾸려면 `BUDGET_OVERRIDES`에 `"kst:YYYY-MM-DD"` / `"utc:YYYY-MM-DD"`를 넣습니다.

## 계산 규칙

- 품목 키: `item_display_name` + 값에 영향을 주는 옵션(색상·품질·의장 등급 등). 내구도·남은 거래 횟수는 무시.
- 시세: 최근 7일 체결가의 수량 가중 중앙값. 체결 5건 미만이면 표시 안 함.
- 저평가 매물: 매물가 ≤ 시세 × (1 − `FEE_RATE`) × (1 − `MIN_MARGIN`). `FEE_RATE` 5%는 가정값.
- NPC 효율: 경매장 가격(시세 있으면 시세, 없으면 최저 매물가) × 묶음 수량 × (1 − 수수료) ÷ NPC 가격 = 화폐 1개당 골드. 골드 판매품은 차익.
- 채널마다 바뀌는 NPC 탭(주머니, 한정상품)은 제외 — 서비스 단계 키가 있어야 채널 전체를 돌 수 있음.

## 화면

- **경매장 시세**: 품목별 시세·최저 매물가·시세 대비·체결 건수/수량·매물 수량·최근 체결(가격 · N분 전). 검색, 카테고리, 체결 있는 품목만, 저평가만 필터.
  - 행을 누르면 상세: 최근 체결 가격 차트(점 = 체결 1건, 점 크기 = 수량, 시세·최저 매물 기준선), 일별 시세, 최근 체결 10건, 가장 싼 매물 5개.
- **최근 체결**: 전체 품목의 최신 체결 300건과 시세 대비.
- **NPC 상점 효율**: 화폐(두카트·골드·인장·솔방울)별 효율 순위. "이 표 읽는 법" 안내, 화폐별 기준 환율, 주목 배지, 등급(A/B/C), 행을 누르면 근거(✔)·주의(⚠).
- **인챈트 본전**: 인챈트 스크롤(랭크별)마다 성공 1회 기대 재료비, 장비 소모 개수, 본전 판매가. 바탕 장비 가격을 넣으면 줄마다 계산. 평일/목요일 확률 전환.

### 인챈트 본전 계산 (`aggregate.py`의 `build_enchant`)

| 항목 | 규칙 |
|---|---|
| 랭크 | 경매장 아이템 옵션 "인챈트 종류"의 `랭크 X`에서 추출(`collect.py`의 `enchant_rank`). 체결·매물 행에 `rank`로 저장 |
| 가격 | 스크롤: 체결 시세, 없으면 최저 매물. 마법가루: `ENCHANT_POWDER`(고대의 마법가루)의 시세 또는 최저 매물 |
| 성공률 | `ENCHANT_RATES` 평일/목요일, 지력 200 이상 기준. 팬 정리 자료(나무위키, biketago)라 게임 안 확률과 대조 필요 |
| 기대 재료비 | (스크롤 + 마법가루) ÷ 성공률. 시도 1회 = 스크롤 1장 + 마법가루 1개로 가정 |
| 장비 소모 | 실패 시 인챈트 불가 랭크(`ENCHANT_RUIN_RANKS` 3·2·1)와 미확인 랭크(`ENCHANT_PENALTY_UNKNOWN` 6·5·4)는 1 ÷ 성공률개, 나머지는 1개 |
| 본전 판매가 | (기대 재료비 + 장비 가격 × 장비 소모) ÷ (1 − 수수료) |

옵션 수치가 랜덤이라 성공해도 판매가는 보장되지 않습니다. 인챈트된 장비의 시세는 이 표에 없습니다(같은 장비라도 옵션 조합이 너무 많음).

### NPC 효율 판정 규칙 (`aggregate.py`의 `assess`)

| 항목 | 규칙 |
|---|---|
| 기준 환율 | 화폐별로 "믿을 만한" 품목(체결 시세 기반이거나 경매장 매물 `MIN_LISTINGS`(3)개 이상)의 효율 중앙값. 이런 품목이 `RATE_MIN_N`(5)개 미만이면 기준이 불안정하다고 보고 주목 표시 안 함 |
| 주목 | 두카트 등: 효율 ≥ 기준 환율 × `HIGHLIGHT_MULT`(1.5). 골드 판매품: 수수료 뺀 경매장 가치 ≥ NPC 가격 × `GOLD_MIN_RATIO`(1.2). 등급 A·B만 |
| 등급 A | 가격 근거가 실제 체결 중앙값 |
| 등급 B | 최저 매물가 기반, 매물 3개 이상, 최저가가 튀지 않음 |
| 등급 C | 매물 부족 또는 최저가가 두 번째 매물보다 `HIGH_GAP2`(30%) 넘게 쌈 |
| 근거(✔) | 기준 대비 배수, 매물 수, 판매 NPC 수, 구매 제한 없음 등 |
| 주의(⚠) | 최저가 기반, 체결 기록 없음, 매물 적음, 최저가 튐, 구매 제한, 재거래·옵션 확인 필요 |

### 무위험에 가까운 후보 (`apply_checks`)

NPC 골드 판매품 중 네 조건을 모두 넘는 품목에 "후보" 배지를 붙입니다. ① 골드로 구매 제한 없이 삼 ② 체결 중앙값 기준 수수료 뺀 가치 ≥ NPC 가격 × 1.2 ③ 하루 체결 `CANDIDATE_MIN_PER_DAY`(3)건 이상 ④ 게임에서 재거래 가능 확인. ④는 `collector/confirmed.json`에 직접 적습니다:

```json
{"items": {"블라고 누아": {"tradable": true, "checked": "2026-11-26", "note": "델에서 사서 경매장 등록 확인"}}}
```

`tradable: false`로 적으면 주목 표시가 꺼지고 "재거래 불가" 표시가 붙습니다.

### NPC 품목 체결 기록 (2026-11-26부터 자동)

거래 가능한 NPC 품목(경매장 매물 3개 이상)이 많이 속한 경매장 카테고리를 최대 4개까지 거래 내역 수집에 추가합니다(`NPC_HISTORY_FROM`, `NPC_HISTORY_MAX_CATS`). 그러면 그 품목들의 가격 근거가 체결 중앙값(A등급)으로 바뀌고 체결 건수가 보입니다. 예산을 맞추려고 현재 매물 스냅샷은 12시간 주기입니다.

## prices.json 구조

| 키 | 내용 |
|---|---|
| `items[]` | 품목별 `median`, `min_listing`, `discount`, `trades`, `volume`, `per_day`, `listed_qty`, `last_price`, `last_ts`, 저평가 `deal_count`/`deal_profit`. 시세가 있으면 상세용 `daily`(일별 중앙값), `recent`(최근 10건), `chart`(최근 최대 60건, `[유닉스 분, 가격, 수량]`), `cheapest`(가장 싼 매물 5개) |
| `recent[]` | 최신 체결 300건 `[시각, 품목, 카테고리, 가격, 수량, 시세]` |
| `npc` | `rates`(화폐별 기준 환율), `rules`(판정 기준값), `items[]`: `unit_price`, `value`, `basis`(시세/최저 매물), `gold_per` 또는 `profit`/`ratio`, `vs_rate`, `grade`, `hot`, `why[]`, `warn[]`, `listings`, `trades` |
| `enchant` | `powder`, `powder_price`, `powder_basis`, `fee_rate`, `rates`, `ruin_ranks`, `unknown_ranks`, `items[]`: `key`, `rank`, `scroll`, `scroll_basis`, `p_wd`/`p_th`, `cost_wd`/`cost_th`, `items_wd`/`items_th`, `ruin`, `penalty_unknown` |

상세용 데이터 개수는 `aggregate.py`의 `RECENT_PER_ITEM`, `RECENT_FEED`, `CHART_POINTS`로 조정합니다(현재 집계본 약 1.2MB).

## 처음 설정

1. Settings → Secrets and variables → Actions에 `NEXON_API_KEY` 등록.
2. Settings → Pages → Source를 **GitHub Actions**로.
3. Actions → `collect-and-deploy` → Run workflow, `force=all`.

## 운영

| 하고 싶은 것 | 방법 |
|---|---|
| 즉시 수집·배포 | Actions → `collect-and-deploy` → Run workflow. `force`: `history` / `listings` / `both` / `npc` / `all` |
| 코드 수정 반영 | `main`에 푸시 → 자동 배포 (수집은 주기가 된 것만 하므로 호출이 늘지 않음) |
| API 키 교체 | Secret `NEXON_API_KEY` 값 변경 |
| 카테고리·주기·예산 변경 | `collector/config.py` 수정 후 푸시 |
| 사이트가 안 바뀌어 보임 | Pages 캐시(최대 10분) → 강력 새로고침(Ctrl+Shift+R) |
| 실행 실패 | Actions 탭에서 실패한 step 확인. 배포 step의 일시 오류는 Re-run |

- 저장소에 60일간 활동이 없으면 GitHub가 예약 실행을 멈춥니다. 가끔 수동 실행이나 커밋이 필요합니다.

## 서비스 단계 키로 바꿀 때

1. 넥슨 오픈 API에서 마비노기 앱을 "서비스 출시 단계"로 등록(서비스 URL = 위 사이트).
2. 새 키로 Secret 교체.
3. `config.py`: `DAILY_BUDGET` 상향, `CATEGORIES` 전체(79개), `HISTORY_INTERVAL_MIN` 30, `LISTINGS_INTERVAL_MIN` 30~60, `BUDGET_OVERRIDES` 정리.
4. NPC 채널별 수집(서버 4 × 채널 × 상인 NPC, 36분 주기)을 추가하고 `NPC_EXCLUDE_TABS`에서 한정상품·주머니 제거.
5. 한 번 실행이 15분 안에 끝나는지 확인.

## 로컬 실행

```bash
export NEXON_API_KEY=...
python collector/collect.py --data data --force all
python collector/aggregate.py --data data --out site/prices.json
cd site && python -m http.server
```

## 데이터·약관 정책

- 원본 수집 데이터는 Actions 캐시에만 두고 공개 저장소에 올리지 않습니다(약관 5조 ⑤). 사이트에는 집계본만 공개합니다.
- 원본은 30일만 보관합니다(NEXON Open API 30일 갱신 고지). 캐시가 지워지면 기준선이 처음부터 다시 쌓입니다.
- API 키는 Secret에만 둡니다(약관 5조 ①②).
- 무료·무광고로 운영합니다. 광고나 제휴 링크는 넥슨 승낙 후에만 붙입니다(약관 6조 ⑥).
- 게임 이미지·로고는 쓰지 않습니다(약관 6조 ③).
