# 스테이지 브리핑

추세추종 스테이지 감지 — GitHub Actions가 평일(장중 여러 차례 + 장마감 후)에
코스피200 + 코스닥150 + 관심종목을 스캔하고, 결과를 GitHub Pages 웹페이지(Nexus Platform)로 보여준다.

**서버가 필요 없다.** 스캔은 GitHub Actions에서 돌고, 웹페이지는 정적 JSON을 읽는다.

## 구조

```
├── scanner/
│   ├── run_scan.py        # 1회 실행 스캐너 (Actions가 실행)
│   ├── data_provider.py   # KIS Open API + pykrx + FDR 데이터 소스
│   └── stage_detector.py  # 스테이지 감지 엔진 (VCP, 돌파, MTT)
├── docs/                  # GitHub Pages 루트
│   ├── index.html         # 브리핑 웹페이지
│   └── data/
│       ├── scan.json      # 최신 스캔 결과 (감지기)
│       ├── tracking.json  # 전략 포트폴리오 원장 (보유/이탈)
│       └── state.json     # 실행 간 유지되는 상태 (스테이지 히스토리, RS 스냅샷, 원장)
├── config.json            # 파라미터 + 관심종목 (키는 없음!)
└── .github/workflows/scan.yml
```

## 두 개의 뷰

- **스테이지 감지기** (`scan.json`) — 지금 스테이지 조건을 만족하는 **모든 후보**의 스냅샷. 장중 여러 번 갱신.
- **전략 포트폴리오** (`tracking.json`) — 그중 실제로 편입해 **이탈 전까지 보유 중**인 원장. 장마감(15:30 KST) 이후 하루 1회만 갱신.

## 전략 포트폴리오 (편입/이탈 원장)

매 스캔마다 재분류하는 방식이 아니라, **한 번 편입되면 이탈 조건 전까지 유지**되는 원장 방식.
스탁이지 전략실처럼 **스테이지별 독립 트랙**으로 트래킹한다.

- **편입 — 2트랙** (공통 게이트: MTT 필터 통과 · 클라이맥스 경고 없음 · KOSPI 진입 허용 · 스테이지 진입 2일 이내 신선도 · 재편입 쿨다운 5일 · 트랙당 보유 10 · 하루 편입 3건)
  - **S3 돌파 트랙**: Stage 3 (전고점 20~60일 고점 돌파 + 거래량 20일 평균의 2배↑) + 신뢰도 70↑
  - **S1 초기추세 트랙**: Stage 1 (MA 정배열 5>20>60>120 + 60일 저점 대비 +50%↑) + 신뢰도 75↑
- **이탈 (익절 없음)**: 고점 대비 **−10% 트레일링** / 종가 < **MA60**
  - KOSPI 청산 신호는 전량 매도가 아니라 **신규 편입 차단** 전용
- MTT 필터: 현재가>MA150·MA200 · MA150>MA200 · MA200 상승 · RS≥70
- **Stage 0 — 바닥 국면 (관찰 전용, 편입 없음)**: MTT는 급락 직후 수개월간 회복이 불가능해
  급등→급락→바닥 재상승 구간이 통째로 미감지되던 공백을 메운다. 120일 고점 대비 −25%↑ 낙폭 +
  저점 대비 +5%↑ 반등(하락 정지)이면 **바닥 다지기**, 여기에 MA20 상향 전환 · MA5>MA20 ·
  저점 상승까지 확인되면 **바닥 반등**. 신뢰도 상한 50으로 묶어 편입 컷(70/75)에 닿지 않는다.
  원장 편입 여부는 별도 백테스트 검증 후 결정한다.
- 임계값은 `config.json`의 `params`에서 수정

## 설정 방법

1. 이 저장소를 fork 하거나 clone 후 자신의 저장소로 push
2. **Secrets 등록**: Settings → Secrets and variables → Actions → New repository secret
   - `KIS_APP_KEY` — 한국투자증권 Open API 앱키
   - `KIS_APP_SECRET` — 앱시크릿
   - (발급: 한국투자증권 홈페이지 → 트레이딩 → Open API 신청)
3. **Pages 활성화**: Settings → Pages → Source: `Deploy from a branch`, Branch: `main` / `/docs`
4. **첫 스캔 실행**: Actions 탭 → "스테이지 스캔" → Run workflow
5. 몇 분 뒤 `https://<아이디>.github.io/<저장소명>/` 접속

> 참고: GitHub Actions의 예약(cron)은 best-effort라 지연·누락될 수 있어, `scan.yml`은 오프피크 분으로 슬롯을 중복 배치해 신뢰도를 높인다. 정확한 시각 보장이 필요하면 외부 크론(예: cron-job.org)으로 `workflow_dispatch` API를 호출한다.

## 로컬 실행

```bash
pip install -r requirements.txt
set KIS_APP_KEY=발급받은키
set KIS_APP_SECRET=발급받은시크릿
python scanner/run_scan.py
# docs/index.html을 브라우저로 열기 (로컬 파일은 fetch 제한이 있으므로 python -m http.server 권장)
```

## 주의

- KIS 앱키는 절대 config.json이나 코드에 넣지 말 것 — 환경변수/Secrets로만
- 시뮬레이션이며 투자 권유가 아님
