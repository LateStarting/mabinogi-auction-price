# 마비노기 경매장 시세표

https://latestarting.github.io/mabinogi-auction-price/

NEXON Open API의 마비노기 경매장·NPC 상점 데이터로 품목별 시세(최근 7일 체결가 중앙값), 최저 매물가, 저평가 매물, NPC 상점 품목의 두카트 효율을 보여 주는 정적 사이트입니다. 비영리 개인 프로젝트이며 넥슨과 관련이 없습니다.

Data based on NEXON Open API.

## 구조

| 파일 | 역할 |
|---|---|
| `collector/config.py` | 대상 카테고리, 수집 주기, 호출 예산, NPC 설정, 계산 기준 |
| `collector/collect.py` | 수집. 우선순위: 거래 내역 → 현재 매물 → NPC 상점 → NPC 품목 경매장 이름 조회 |
| `collector/aggregate.py` | 수집 데이터를 `site/prices.json`으로 집계 (`items` + `npc`) |
| `site/index.html` | 정적 페이지. 탭: 경매장 시세 / NPC 상점 효율 (`#npc`) |
| `.github/workflows/collect.yml` | 15분 cron + `main` 푸시 + 수동 실행. build job → deploy job |

마비노기 경매장은 전 서버 통합이라 서버 구분 없이 집계합니다.

## 수집 주기와 호출 예산 (개발 단계 키: 하루 1,000건)

| 작업 | 주기 | 하루 |
|---|---|---|
| 거래 내역 (최근 1시간만 제공) | 45분 | 약 480건 |
| 현재 매물 | 6시간 | 약 260건 |
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
