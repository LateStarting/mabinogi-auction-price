# 마비노기 경매장 시세표

NEXON Open API의 마비노기 경매장 데이터로 품목별 시세(최근 7일 체결가 중앙값)와 최저 매물가를 보여 주는 정적 사이트입니다. 비영리 개인 프로젝트이며 넥슨과 관련이 없습니다.

Data based on NEXON Open API.

## 구조

- `collector/collect.py` — 거래 내역(최근 1시간만 제공)과 현재 매물을 수집. 하루 호출 예산을 `state.json`에 기록하고 넘으면 멈춤.
- `collector/aggregate.py` — 수집 데이터를 `site/prices.json`으로 집계.
- `collector/config.py` — 대상 카테고리, 수집 주기, 예산, 기준선·저평가 기준.
- `site/index.html` — 시세표 페이지 (`prices.json`을 읽음).
- `.github/workflows/collect.yml` — 15분마다 실행. 주기가 된 수집만 하고, 원본 데이터는 Actions 캐시에 보관(공개 저장소에 올리지 않음), 집계본만 Pages에 배포.

## 설정

1. 저장소 Settings → Secrets and variables → Actions에 `NEXON_API_KEY` 등록.
2. Settings → Pages → Source를 **GitHub Actions**로 설정.
3. Actions 탭에서 `collect-and-deploy`를 `force=both`로 한 번 수동 실행.

## 호출 예산 (개발 단계 키: 하루 1,000건)

- 거래 내역: 45분마다 약 15건 → 하루 약 480건
- 매물: 4시간마다 약 65건 → 하루 약 390건
- 서비스 단계 키를 받으면 `config.py`에서 카테고리·주기·`DAILY_BUDGET`을 늘린다.

## 로컬 실행

```bash
export NEXON_API_KEY=...
python collector/collect.py --data data --force both
python collector/aggregate.py --data data --out site/prices.json
cd site && python -m http.server
```

## 데이터 정책

- 원본은 30일만 보관 (NEXON Open API 30일 갱신 고지).
- 원본 데이터 덤프나 API를 제3자에게 재제공하지 않음. 원본은 Actions 캐시에만 있고, 캐시가 지워지면 기준선이 처음부터 다시 쌓인다.
