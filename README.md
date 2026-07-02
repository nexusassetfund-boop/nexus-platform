# 깡토 스테이지 브리핑

깡토 추세추종 스테이지 감지 — GitHub Actions가 평일 10:00 / 12:00 / 14:00 (KST)에
코스피200 + 코스닥150 + 관심종목을 스캔하고, 결과를 GitHub Pages 웹페이지로 보여준다.

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
│       ├── scan.json      # 최신 스캔 결과
│       ├── tracking.json  # 보유/이탈 원장
│       └── state.json     # 실행 간 유지되는 상태 (스테이지 히스토리, RS 스냅샷, 원장)
├── config.json            # 파라미터 + 관심종목 (키는 없음!)
└── .github/workflows/scan.yml
```

## 트래킹 (편입/이탈 원장)

매 스캔마다 재분류하는 방식이 아니라, **한 번 편입되면 이탈 조건 전까지 유지**되는 원장 방식.

- **편입**: Stage 3 돌파 + 신뢰도 70↑ + KOSPI > MA200 + 클라이맥스 경고 없음
- **이탈**: 손절 −8% / 목표 +21% / 트레일링(+14% 도달 후 고점 대비 −8%) / MA60 하회 / KOSPI 청산 신호
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
