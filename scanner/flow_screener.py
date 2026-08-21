"""수급 감지기 — 외국인·기관 연속 순매수 종목 탐지. 출력: docs/data/flow.json
      + flow_state.json(멤버십 스냅샷) / flow_members.json(편입·편출, flow.json에 동봉)

프론트 '스테이지 감지기 > 수급' 하위탭과 홈 '연동 전략 편입·편출' 카드가 읽는다.
홈에 나가는 편입·편출 멤버십은 S·A(외국인 기준)만 — 아래 백테스트 결과 참조.

설계 요지 (flow_history 참조):
  · '연속 N일'을 1차 기준으로 쓰지 않는다. 하루 노이즈 매도로 리셋되는 게 옛 방식의 결함.
    흠집 허용 스트릭 + 창(10거래일) 강도·지속성을 함께 본다.
  · 순매수는 주수를 거래량으로 정규화한다(intensity). 10만주는 종목마다 의미가 다르다.
  · 1일 집중도(concentr)가 높으면 지수 리밸런싱·블록딜·ETF LP 물량일 공산이 크다 — 후보에서 뺀다.
  · 추세형(종가≥MA20) / 역발상형(종가<MA20)을 나눠서 낸다. 같은 표에 섞으면 해석이 안 된다.

## 백테스트 결과가 설계를 바꿨다 (flow_backtest.py, 96신호일 / 28,178관측)
  · 외국인은 (+), 기관은 (−)다. intensity_frgn IC +0.028(t=3.27) / intensity_inst
    IC −0.029(t=−2.85), persist_inst −0.031(t=−3.14). 기관합계는 '노이즈'가 아니라
    반대 방향 신호였다 — 금융투자·ETF LP가 섞인 탓으로 추정하나 분리 불가(위 참조).
  · 그래서 **쌍끌이를 최상위 등급으로 두지 않는다**. tol_both_ge4 IC −0.011로
    오히려 음수 — 기관 조건을 AND로 걸면 외국인 신호를 망친다.
  · 가장 강한 신호는 외국인 우위 강도(frgn − inst) IC +0.033(t=3.96),
    실전 조합은 추세형 한정 외국인 강도 IC +0.031(t=3.58, 초과수익 +0.60%/20거래일).
  · '연속 N일'은 강도·지속성에 진다. hard/tol 스트릭 4일·5일 전부 |t|<2.
    흠집 허용이 무관용보다 낫지도 않았다(tol 0.008 vs hard 0.011) — 스트릭은
    사용자가 요구한 탐지 조건으로 남기되 **랭킹은 강도로** 한다.
  · 초과수익 절대값이 +0.6%/20거래일이라 거래비용 감안 시 알파라 부르기 어렵다.
    → '매수 신호'가 아니라 **관찰 리스트**. 프론트 문구도 그렇게 되어 있다.

## 2차 백테스트 — KRX 상세 병합으로 미검증 3종 검증 (같은 96신호일, 2026-08)
  · 연기금 P 조건(스트릭≥4·강도≥2%): 초과수익 −0.85%/20거래일(초과t=−3.01, 2,624건).
    연기금 4일+ 매집 자체가 −0.90%(t=−3.97) — '스마트머니 매집'이 아니라 역신호였다.
    연기금은 하락 시 사는 성향이라 순매집이 약세 종목 마커가 되는 것으로 추정.
  · 기관합계−금융투자(inst_xf)도 여전히 (−): 강도 IC −0.019(t=−2.33), 지속성
    IC −0.027(t=−2.96). 기관 (−)가 금융투자·ETF LP 노이즈 탓이라는 가설은 기각 —
    구조적이다. frgn−instxf IC +0.026(t=3.29)로 기관합계판(+0.028)과 동등, 개선 아님.
  · 전환(rev): 외국인 −0.64%(초과t=−2.07)·기관(−금투) −0.84%(t=−2.87) — 연속
    순매도 후 1~3일 순매수 전환은 반등 신호가 아니라 유의한 역신호. 매도 스트릭
    문턱 3/4/5 전 구간 (−)로 파라미터에 강건하다. 연기금 rev는 무신호(t=−0.83).

## 3차 검증 — "스트릭 창 vs 강도 창 불일치"는 결함이 아니다 (2026-08-19)
자격이 스트릭(가변 구간)과 강도(고정 10일)를 함께 요구하므로, 최근 매집을 시작한
종목은 강도 분모에 매집 이전 며칠이 섞여 희석된다 → "최근 매집주가 누락된다"는
문제 제기가 있었다. 300종목·520거래일·96신호일로 검증한 결과 **고치면 안 된다**:
  · 강도 창을 스트릭 구간에 맞추면 IC가 오히려 **떨어진다**: 10일 0.0388(t=3.55)
    → 스트릭구간 0.0267(t=2.57). 10일 창은 희석이 아니라 더 안정된 추정치다.
  · 그 '누락' 종목군(스트릭≥4 · 10일강도<0.05 · 스트릭구간강도≥0.05, 1,316건)의
    fwd20 초과수익은 −0.34%(t=−0.65) — 놓쳐서 아까운 종목이 아니었다.
  · 스트릭 길이 자체는 여전히 무신호(IC 0.0091, t=0.79). 스트릭 게이트를 아예
    빼도 개선 없음(+0.18% → +0.10%, 둘 다 t<0.4).

## 3차 검증 — 알파는 '매도 쪽'에만 있다 (같은 표본, t는 전부 날짜 단위)
10일 외국인 강도를 날짜별 분위로 자르면 IC(+0.039, t=3.55)의 출처가 드러난다:
  · 하위 20% −0.89%(t=−2.83) / 하위 10% −0.90%(t=−2.03) — 유의하게 나쁘다.
    보유기간을 늘려도 강건: fwd40 −2.09%(t=−3.04), fwd60 −3.13%(t=−3.89).
  · **상위 20% +0.21%(t=0.62) / 상위 10% +0.09%(t=0.21) — 알파가 없다.**
    fwd60에선 상위 분위조차 −2.14%(t=−2.07)로 뒤집힌다.
  · 롱숏 스프레드는 어느 보유기간에서도 유의한 적이 없다(t 최대 1.63).
=> '외국인이 사는 종목을 찾는다'는 롱 전용 전제가 데이터로 지지되지 않는다.
   쓸모 있는 방향은 **제외 필터**(하위 분위를 다른 전략 후보에서 뺀다)이지 편입
   신호가 아니다. 등급·편입을 매수 근거로 승격하지 말 것.

### 주의: 위쪽 1·2차 결과의 '초과t'는 관측치 단위라 높게 읽힌다
flow_backtest 의 초과t는 신호일을 가로질러 관측치를 다 모아 계산한다. 같은 날 300종목은
독립이 아니므로(공통 시장 변동) 표본수가 부풀고 t가 과대평가된다 — 실측 대비: 외국인
강도 상위 20% 초과t가 관측치 단위 +0.83인데 날짜 단위로는 +0.62, 하위 20%는 −3.84 대
−2.83이다. IC의 t값은 원래 날짜 단위라 영향이 없다. **초과t는 IC의 t보다 후하게 나온다는
전제로 읽을 것.** (이 계산을 바꾸면 위 1·2차 수치도 전부 재산출해야 하므로 보류 — 별도 작업.)

등급 (위 결과 반영):
  S  외국인 자격 + 추세형(종가≥MA20) + 외국인 우위(edge>0) — 백테스트 최선 조합
  A  외국인 자격
  P  연기금 자격 — KRX 상세가 닿을 때만. 백테스트상 (−) 신호 — C처럼 경고 표시용
  C  기관 단독 자격 — 백테스트상 (−) 신호라 경고 표시용으로만 남긴다
  자격 = 흠집 허용 스트릭 ≥ MIN_STREAK 이고 해당 주체 강도 ≥ MIN_INTENSITY
별도 플래그 rev: 연속 순매도(흠집 허용 ≥ MIN_STREAK)를 지속하다 최근 1~3일 순매수로
  전환한 종목(외국인·기관). 등급과 직교 — 프론트에서 '전환' 필터로 따로 본다.
  백테스트상 (−) — '수급 개선' 신호가 아니라 이탈 관찰용이다.

기관은 KRX 상세가 닿으면 기관합계−금융투자(ETF LP·차익거래 제거) 기준, 아니면
기관합계 폴백(inst_basis 필드로 구분). KRX는 로그인(KRX_ID/KRX_PW) 없인 차단 상습.

실행: 매일 장마감 후. 테스트: FLOW_LIMIT=20 python scanner/flow_screener.py
실패 정책: 수급 확보가 유니버스의 절반 미만이면 기존 출력 보존 후 exit 1.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import flow_history as fh

logger = logging.getLogger("flow")
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "flow.json"
UNIV_CACHE = ROOT / "docs" / "data" / "flow_universe.json"
STATE_PATH = ROOT / "docs" / "data" / "flow_state.json"      # 직전 멤버십 스냅샷
HIST_PATH = ROOT / "docs" / "data" / "flow_members.json"     # append-only 편입/편출
# 파일명이 flow_history.json이 아닌 건 scanner/flow_history.py(일별 수급 시계열)와
# 헷갈리지 않기 위해서다 — 이쪽은 '명단에 들고 난' 이력이다.

UNIVERSE_N = int(os.environ.get("FLOW_LIMIT", "300"))   # 시총 상위 N
MIN_STREAK = 4              # 흠집 허용 연속 순매수일
# 창 누적 순매수 / 창 누적 거래량. 백테스트가 측정한 건 '강도 상위 20%' 구간이라
# 유니버스 상위 20% 언저리(실측 p80 ≈ 13%, 완화해서 5%)에 맞춘다. 2%로 두면 300종목 중
# 115종목이 후보로 잡혀 관찰 리스트 구실을 못 했다.
MIN_INTENSITY = 0.05
# 연기금은 매매 규모가 기관합계·외국인보다 한참 작다 — 같은 5%를 걸면 후보가 안 나온다.
# 백테스트 실측: 이 조건(스트릭≥4·강도≥2%)은 초과수익 −0.85%(t=−3.01) — 역신호.
# 문턱을 조정해도 방향이 바뀌지 않아(4일+ 매집 자체가 −0.90%) 경고 표시용으로만 쓴다.
MIN_INTENSITY_PENSION = 0.02
MAX_CONCENTR = 0.6          # 1일 집중도 상한 (리밸런싱·블록딜 배제)
WORKERS = 8


def _row(code: str, name: str) -> dict | None:
    rows = fh._safe_fetch(code, pages=2)            # 40거래일 — MA20 + 창 여유
    if len(rows) < 20:
        return None
    detail_ok = False
    if fh.detail_available():
        detail = fh.fetch_detail(code, rows[0]["date"], rows[-1]["date"])
        detail_ok = bool(detail) and fh.join_detail(rows, detail)
    m = fh.metrics(rows)
    if m is None:
        return None
    f = m["frgn"]
    # 기관: 상세가 닿으면 기관합계−금융투자, 아니면 기관합계 폴백
    i = fh.side_metrics(rows, "inst_xf") if detail_ok else m["inst"]
    p = fh.side_metrics(rows, "pension") if detail_ok else None
    ma20 = statistics.fmean(r["close"] for r in rows[-20:])
    close = m["close"]
    concentr = max(f["concentr"], i["concentr"])
    trend = close >= ma20
    edge = f["intensity"] - i["intensity"]          # 백테스트 최강 신호 (IC 0.033, t=3.96)
    f_ok = f["streak"] >= MIN_STREAK and f["intensity"] >= MIN_INTENSITY
    i_ok = i["streak"] >= MIN_STREAK and i["intensity"] >= MIN_INTENSITY
    p_ok = p is not None and p["streak"] >= MIN_STREAK and p["intensity"] >= MIN_INTENSITY_PENSION
    grade = ("S" if (f_ok and trend and edge > 0) else "A" if f_ok
             else "P" if p_ok else "C" if i_ok else "-")
    # 매도→매수 전환 (등급과 직교): 직전 매도 스트릭이 자격 기준 이상일 때만
    f_rev = f["rev_flip"] > 0 and f["rev_sell"] >= MIN_STREAK
    i_rev = i["rev_flip"] > 0 and i["rev_sell"] >= MIN_STREAK
    rev = "both" if (f_rev and i_rev) else "frgn" if f_rev else "inst" if i_rev else None
    return {
        "ticker": code,
        "name": name,
        "close": close,
        "base_date": m["date"],
        "frgn_streak": f["streak"], "frgn_blemish": f["blemish"],
        "inst_streak": i["streak"], "inst_blemish": i["blemish"],
        "frgn_hard_streak": f["hard_streak"], "inst_hard_streak": i["hard_streak"],
        "frgn_net": int(f["net"]), "inst_net": int(i["net"]),
        "frgn_intensity_pct": round(f["intensity"] * 100, 2),
        "inst_intensity_pct": round(i["intensity"] * 100, 2),
        "intensity_pct": round(m["both_intensity"] * 100, 2),
        "frgn_persist": round(f["persist"], 2),
        "inst_persist": round(i["persist"], 2),
        "concentr": round(concentr, 2),
        "net_value_억": round((f["net"] + i["net"]) * close / 1e8),
        "avg_volume": int(m["avg_volume"]),
        "ma20": round(ma20),
        "regime": "trend" if trend else "contra",
        "edge_pct": round(edge * 100, 2),           # 외국인 우위 강도 — 랭킹 기준
        "grade": grade,
        "inst_basis": "ex_fin" if detail_ok else "total",
        "pension_net": int(p["net"]) if p else None,
        "pension_streak": p["streak"] if p else None,
        "pension_blemish": p["blemish"] if p else None,
        "pension_intensity_pct": round(p["intensity"] * 100, 2) if p else None,
        "rev": rev,
        "frgn_rev_flip": f["rev_flip"], "frgn_rev_sell": f["rev_sell"],
        "inst_rev_flip": i["rev_flip"], "inst_rev_sell": i["rev_sell"],
        "is_candidate": int((grade != "-" or rev is not None) and concentr <= MAX_CONCENTR),
    }


# ── 편입/편출 이력 ──────────────────────────────────────
# 홈 '연동 전략 편입·편출' 카드가 이 history를 그대로 읽는다(canslim·눌림목과 동일 규약).
# 멤버십은 **S·A(외국인 기준)만** — C(기관 단독)·P(연기금)·rev(전환)는 백테스트에서
# 전부 (−) 신호라 이걸 '편입'으로 내보내면 카드가 나쁜 종목을 추천하는 꼴이 된다.
MEMBER_GRADES = ("S", "A")


def _days(a: str, b: str) -> int | None:
    try:
        return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days
    except Exception:                               # noqa: BLE001
        return None


def _track(cands: list[dict], snap: str) -> list[dict]:
    """편입/편출 이력을 갱신하고 최근 30건을 돌려준다 (canslim _track과 같은 원칙).

    확정 조건은 **데이터 기준일이 오늘**일 때뿐이다. 네이버 투자자별 확정치가 아직
    안 붙은 시각에 돌면 base_date가 T-1이라, 그 실행이 어제 명단으로 오늘의 편입·편출을
    확정해 버리는 걸 막는다(flow.yml 주석의 16:50 사례).
    """
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:                               # noqa: BLE001
        state = {}
    try:
        history = json.loads(HIST_PATH.read_text(encoding="utf-8"))
    except Exception:                               # noqa: BLE001
        history = []

    members = {r["ticker"]: r for r in cands if r["grade"] in MEMBER_GRADES}
    for r in cands:
        p = state.get(r["ticker"]) or {}
        if r["ticker"] in members:
            first = p.get("first") or snap
            r["first_seen"] = first
            r["is_new"] = int(bool(state) and first == snap)
            r["days_in_list"] = _days(snap, first) or 0

    today = dt.datetime.now(tz=KST).strftime("%Y-%m-%d")
    if snap != today:
        logger.info("비확정 실행 (기준일 %s ≠ 오늘 %s) — 이력/상태 동결", snap, today)
        return history[-30:]
    # 후보 급감 가드 — 네이버 부분 장애로 쪼그라든 실행이 대량 편출을 확정하는 것 방지
    # (눌림목 7/28 사례). 진짜 시장 변화면 다음 실행이 하루 늦게 확정한다.
    if len(state) >= 8 and len(members) < len(state) * 0.4:
        logger.warning("멤버 급감 (%d→%d) — 일시 장애 의심, 이번 실행은 확정 보류",
                       len(state), len(members))
        return history[-30:]

    new_state = {c: {"first": r["first_seen"], "name": r["name"],
                     "grade": r["grade"], "rank": r["rank"]}
                 for c, r in members.items()}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
    if not state:                                   # 최초 실행 — diff 없이 시드만
        return history[-30:]

    added = [{"code": c, "name": r["name"], "grade": r["grade"]}
             for c, r in members.items() if c not in state]
    removed = [{"code": c, "name": (state[c].get("name") or c),
                "days": _days(snap, state[c].get("first") or snap),
                "last_grade": state[c].get("grade"), "last_rank": state[c].get("rank")}
               for c in sorted(set(state) - set(members))]
    if added or removed:
        if history and history[-1].get("date") == snap:   # 같은 날 재실행 → 병합 (멱등)
            last = history[-1]
            last["added"] = list({a["code"]: a for a in last.get("added", []) + added}.values())
            last["removed"] = list({r["code"]: r for r in last.get("removed", []) + removed}.values())
        else:
            history.append({"date": snap, "added": added, "removed": removed})
        HIST_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    return history[-30:]


def build() -> dict | None:
    univ = fh.universe(UNIVERSE_N, UNIV_CACHE)
    fh.detail_available()                           # 스레드 시작 전 1회 probe (레이스 방지)
    logger.info("유니버스 %d종목 수급 조회", len(univ))
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(lambda kv: _row(*kv), univ.items()):
            if r:
                rows.append(r)
    logger.info("수급 확보 %d/%d", len(rows), len(univ))
    if len(rows) < len(univ) * 0.5:
        return None

    cands = [r for r in rows if r["is_candidate"]]
    # 순서 = 백테스트 초과수익 순: S·A(+) → C(−0.43%) → 전환 전용(−0.64~−0.84%) → P(−0.85%)
    _ORDER = {"S": 0, "A": 1, "C": 2, "P": 4}       # 3 = 전환 전용(무등급)
    cands.sort(key=lambda r: (_ORDER.get(r["grade"], 3 if r["rev"] else 9), -r["edge_pct"]))
    for n, r in enumerate(cands, 1):
        r["rank"] = n
    base = max((r["base_date"] for r in rows), default="")
    snap = f"{base[:4]}-{base[4:6]}-{base[6:]}" if base else ""
    history = _track(cands, snap) if snap else []
    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "snap_date": snap,
        "history": history,
        "thresholds": {
            "window": fh.WINDOW, "min_streak": MIN_STREAK,
            "tol_ratio": fh.TOL_RATIO, "max_blemish": fh.MAX_BLEMISH,
            "min_intensity_pct": MIN_INTENSITY * 100,
            "min_intensity_pension_pct": MIN_INTENSITY_PENSION * 100,
            "max_concentr": MAX_CONCENTR,
            "rev_max_flip": fh.REV_MAX_FLIP,
            "universe_n": UNIVERSE_N,
            "detail": "krx" if fh.detail_available() else "none",
            "note": "스트릭은 '흠집 허용' — 직전 평균 순매수의 30% 미만인 매도일은 연속을 끊지 않는다. "
                    "강도는 순매수주수/거래량(주수 정규화). 기관은 KRX 상세가 닿으면 "
                    "기관합계−금융투자(ETF LP·차익거래 제거), 차단 시 기관합계 폴백(inst_basis 참조). "
                    "전환(rev)은 연속 순매도 지속 후 최근 1~3일 순매수 전환 — 백테스트상 역신호라 "
                    "'수급 개선'이 아니라 이탈 관찰용이다. P(연기금)·C(기관)도 백테스트 (−) — 경고 표시. "
                    "초과수익이 20거래일 +0.6% 수준이라 매수 신호가 아니라 관찰 리스트다.",
        },
        # 프론트가 그대로 노출하는 검증 요약 — 근거 없는 등급으로 보이지 않게 한다
        "evidence": {
            "period": "96 신호일 / 28,178 관측 (주간 리밸런싱, 20거래일 성과)",
            "best": "외국인 우위 강도(외인−기관) IC +0.033 (t=3.96)",
            "trend_only": "추세형 한정 외국인 강도 IC +0.031 (t=3.58), 초과수익 +0.60%",
            "inst_negative": "기관 강도 IC −0.029 (t=−2.85) · 기관 지속성 −0.031 (t=−3.14)",
            "streak_weak": "연속일수(무관용·흠집허용, 4·5일) 전부 |t| < 2 — 랭킹은 강도로 한다",
            "both_bad": "쌍끌이(양쪽 스트릭) IC −0.011 — 기관을 AND로 걸면 외국인 신호가 죽는다",
            "pension_negative": "연기금 P 조건(스트릭≥4·강도≥2%) 초과수익 −0.85% (초과t=−3.01, "
                                "2,624건) — 매집 신호가 아니라 역신호. 경고 표시용",
            "instxf_negative": "기관합계−금융투자도 (−): 강도 IC −0.019 (t=−2.33) — "
                               "기관 역신호는 금융투자·ETF LP 탓이 아니라 구조적",
            "rev_negative": "전환(rev) 초과수익: 외국인 −0.64% (t=−2.07) · 기관−금투 −0.84% "
                            "(t=−2.87) — 매도 스트릭 문턱 3/4/5 전 구간 (−). 반등 신호 아님",
            "caveat": "생존편향(현 상장사 스냅샷) · 거래비용 미반영 · 검증창 약 2년(96신호일)",
        },
        "scanned": len(rows),
        "count": len(cands),
        "s_count": sum(1 for r in cands if r["grade"] == "S"),
        "a_count": sum(1 for r in cands if r["grade"] == "A"),
        "p_count": sum(1 for r in cands if r["grade"] == "P"),
        "c_count": sum(1 for r in cands if r["grade"] == "C"),
        "rev_count": sum(1 for r in cands if r["rev"]),
        "trend_count": sum(1 for r in cands if r["regime"] == "trend"),
        "candidates": cands,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = build()
    if data is None:
        logger.error("수급 스캔 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("저장: %s (후보 %d, 쌍끌이 %d, 추세형 %d)",
                OUT_PATH, data["count"], data["s_count"], data["trend_count"])


if __name__ == "__main__":
    main()
