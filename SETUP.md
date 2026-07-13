# Nexus Platform — 새 컴퓨터 세팅 가이드 (AI용)

> 이 문서만 읽으면 Claude Code(또는 다른 AI 에이전트)가 어떤 컴퓨터에서든 Nexus Platform 개발 환경을 세팅할 수 있도록 작성됨. 사람이 해야 하는 단계는 **[사용자]** 로 표시.

## 1. 시스템 개요

**Nexus Platform** — 넥서스자산운용의 완전 클라우드 자립형 한국 주식 플랫폼.
라이브: https://nexus-platform.nexusassetfund.workers.dev

| 저장소 | 내용 | 위치 |
|---|---|---|
| **nexus-cloud** (이 repo) | Cloudflare Worker 백엔드(`src/worker.js`, `src/portfolio.js`) + 웹 프론트(`public/index.html` 단일 SPA) + 보조 엔진(`engine/`) | github.com/nexusassetfund-boop/nexus-cloud (프라이빗) |
| **nexus-web** | 감지기 스캐너·시황 브리핑·주간 복기 파이프라인(GitHub Actions) + 산출 데이터(`docs/data/*.json`) | github.com/nexusassetfund-boop/nexus-platform (로컬 폴더명만 nexus-web) |

**아키텍처 한 장 요약**: GitHub Actions(nexus-platform repo)가 스캔/브리핑/복기 데이터를 생산해 `docs/data/`에 커밋 → Cloudflare Worker가 GitHub raw에서 동기화(`syncFromGitHub`, KV 저장) + 5분 시세 갱신 + 원장(Durable Object BookStore) 관리 → `public/index.html`이 `./data/*.json`을 읽어 렌더. 트리거는 **cron-job.org(주) + Worker 크론(보조) + GitHub schedule(백업)** 3중화.

주요 탭: 시황(브리핑|섹터 맵) · 스테이지 감지기(감지기|전략 포트폴리오[보유|이탈|주간 복기]) · 모델 포트폴리오 · 가치투자 · Post IPO · 설정

## 2. 세팅 절차

### 2-1. 클론
```
git clone https://github.com/nexusassetfund-boop/nexus-cloud.git
git clone https://github.com/nexusassetfund-boop/nexus-platform.git nexus-web
```
**[사용자]** 프라이빗 repo이므로 `gh auth login` (GitHub 계정: nexusassetfund-boop, workflow 스코프 포함) 필요.

### 2-2. 도구
- Node.js 18+ (wrangler는 `npx wrangler`로 실행 — 전역 설치 불필요)
- Python 3.11+ (`pip install requests openpyxl` 정도면 엔진 스크립트 충분; 스캐너 전체는 nexus-web/requirements.txt)
- **[사용자]** `npx wrangler login` — Cloudflare 계정(nexusassetfund@gmail.com) 브라우저 인증 1회

### 2-3. 비밀 파일 (repo에 없음 — 직접 생성)
`nexus-cloud/engine/cloud.json`:
```json
{
  "base_url": "https://nexus-platform.nexusassetfund.workers.dev",
  "token": "<관리 토큰>"
}
```
**[사용자]** `<관리 토큰>` = Worker ADMIN_TOKEN. 웹사이트 설정 탭에 저장해 둔 값 또는 기존 컴퓨터의 cloud.json에서 복사. 이 파일은 .gitignore에 걸려 있음 — **절대 커밋 금지**.

### 2-4. 배포 검증
```
cd nexus-cloud
npx wrangler deploy        # 성공하면 즉시 라이브 반영
```
프론트(`public/index.html`)·백엔드(`src/worker.js`) 수정 → `npx wrangler deploy` 가 배포의 전부다.

## 3. 개발 시 반드시 알아야 할 함정

1. **데이터 흐름**: 프론트는 Worker KV(`data:*.json`)를 읽는다. `syncFromGitHub`는 **scan.json의 scan_time이 바뀔 때만** 전체 동기화(gh_sync_marker) — value*.json 등만 바뀐 커밋은 KV에 자동 반영 안 됨. 즉시 반영하려면 `POST /api/push` (admin 인증, body `{files:{"파일명": <json>}}`, PUSH_FILES 화이트리스트 확인).
2. **무료 플랜 예산**: KV 쓰기 1,000/일(시세 5분 주기 ~550/일 사용 중), 크론 1회당 서브리퀘스트 50. 신규 기능은 정적 자산·기존 스캔 편승·클라이언트 계산 우선(예산 0 원칙).
3. **refresh 순서**: syncFromGitHub → refreshScanQuotes → refreshQuotes → refreshIpoQuotes. sync가 뒤로 가면 실시간 시세를 덮어씀.
4. **UI**: 원본 Tailwind 디자인 유지(재디자인 금지 — 사용자 확정). 외부 텍스트는 `_esc()`로 이스케이프. 라우트 `/data/briefing/:id`는 `/data/` 프리픽스 핸들러보다 앞에 있어야 함.
5. **트리거**: cron-job.org(계정 nexusassetfund)가 주 트리거 — 시세 5분/장마감 15:45/스캔 매시 07분/마감 스캔 15:35/장전 브리핑 07:20/장마감 브리핑 15:40 (Asia/Seoul 평일). GitHub schedule은 15~40분 지연 상습, workflow_dispatch는 즉시.
6. **장전(am) 브리핑 디스패치에 isTradingDayToday 가드 금지** — 07:20엔 당일 KOSPI 캔들이 없어 항상 휴장 오판. am은 주말 체크만.
7. **데이터 소스 성질**: KRX(pykrx)는 클라우드/일부 IP 간헐 차단(유니버스 캐시로 대응 완료), 야후 지수 캔들은 1~2일 지연 가능(네이버 fchart 폴백 패턴 사용), 네이버 폴링 다중종목은 `SERVICE_ITEM:코드1,코드2` 형식(EUC-KR 응답).
8. **KIS 토큰**: 24시간 유효, Worker KV 공유(`/api/kis/token`) — 러너가 재사용 후 발급 시 업로드. 재발급 남발 금지.
9. **nexus-web 커밋 경합**: 스캔 봇이 수시로 커밋 — push 거절 시 `git pull --rebase origin main` 후 push.
10. **Windows 참고**: PowerShell 5.1(`&&` 미지원), 콘솔 cp949 → `python -X utf8` 필수.

## 4. 비밀·시크릿 위치 맵 (값은 여기 없음)

| 시크릿 | 위치 | 용도 |
|---|---|---|
| ADMIN_TOKEN | Worker Secret + engine/cloud.json + cron-job.org 잡 헤더 | 관리 API 인증 |
| GH_TOKEN | Worker Secret | 스캔 workflow_dispatch |
| CLAUDE_CODE_OAUTH_TOKEN | nexus-platform repo Secrets | 브리핑·주간 복기 Claude 헤드리스 |
| NEXUS_ADMIN_TOKEN | nexus-platform repo Secrets | Actions → Worker 게시 |
| KIS_APP_KEY/SECRET, DART_API_KEY | nexus-platform repo Secrets | 시세·재무 |
| cron-job.org API 키 | 사용자 보유 | 트리거 잡 관리 |

GitHub Secrets·Worker Secrets는 이미 설정돼 있어 새 컴퓨터에서 만질 필요 없음. 필요한 것은 **cloud.json 한 파일**뿐.

## 5. 검증 체크리스트 (세팅 후)

- [ ] `npx wrangler deploy` 성공 + 사이트 접속 확인
- [ ] `Invoke-RestMethod <base_url>/data/scan.json` — scan_time이 최근 거래일
- [ ] cloud.json 인증 확인: `POST /api/quotes/refresh` (Bearer 토큰) → `{"ok":true,...}`
- [ ] nexus-web에서 `gh run list --limit 5` — 스캔/브리핑 워크플로 정상 이력
