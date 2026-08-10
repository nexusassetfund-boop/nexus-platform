"""CANSLIM 스크리너 — 윌리엄 오닐 7대 요건(C/A/N/S1/S2/L)+M(시장방향) 한국시장 판정.

근거: STRATEGY_CODEBASE.md §4-3 (StockEasy 이식). 임계치는 한국시장 조정본.

재사용 (새로 짜지 않은 것):
  · 거래일 캘린더·전종목 스냅샷·깡토 RS·시장방향(M) → pullback_screener
  · 52주 고가/근접도·vol_2x_bo(S2) → pullback_screener._detail
  · 연간 재무(A: 순이익 YoY·ROE) → fetch_value._financials (DART, corp_code 캐시 공용)
새로 짠 것: 분기 순이익 YoY(C) — DART 전체재무제표의 분기 단독 순이익을 전년 동기와 직접 비교.

파이프라인:
  1) 전종목 스냅샷 → 깡토 RS + 시총 + 상장주식수
  2) 1차 필터(시총·RS·주가·우선주/스팩) → RS 상위 MAX_DETAIL 종목만 개별 일봉
  3) 일봉 → 52주 근접도(N)·거래량 2배 돌파(S2)
  4) 기관+외인 60거래일 순매수(I)
  5) DART → 분기 순이익 YoY(C, 2023 3분기부터 제공), 연간 순이익 YoY·ROE(A)
  6) 7점 채점 + M(시장방향)
출력: docs/data/canslim.json (프론트 '전략실 > CANSLIM' 탭이 읽음)
      + canslim_state.json(멤버십 스냅샷·리포 전용) / canslim_history.json(편입·편출, canslim.json에 동봉)

주의: EPS 대신 순이익(NI) 증가율을 쓴다 — 분기 주식수 시계열을 DART에서 별도로
      받아야 해서, 주식수 변동이 없는 대부분의 종목에서 동일한 근사다. 유상증자·
      대규모 자사주 소각 종목은 C/A가 실제 EPS와 벌어질 수 있다.
실행: 매일 장마감 후 canslim.yml. 테스트: CANSLIM_LIMIT=5.
실패 정책: 일봉 확보가 후보의 절반 미만이면 기존 출력 보존 후 exit 1 (눌림목과 동일).
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pullback_screener as pb

logger = logging.getLogger("canslim")
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "canslim.json"
SCAN_PATH = ROOT / "docs" / "data" / "scan.json"
STATE_PATH = ROOT / "docs" / "data" / "canslim_state.json"      # {code: {first,rank,score,name}}
HISTORY_PATH = ROOT / "docs" / "data" / "canslim_history.json"  # append-only 편입/편출
MEMBER_SCORE = 5                 # 편입/편출 판정선 — 프론트 기본 필터(score≥5)와 동일.
                                 # 전체 후보엔 2~3점도 섞여 있어 그대로 세면 이력이 무의미하다.

# ── 7대 요건 임계치 (한국시장 조정 — 완화하려면 여기만 수정) ──
C_NI_YOY = 20.0                  # C: 최근 분기 순이익 YoY ≥ 20%
A_NI_YOY = 25.0                  # A: 연간 순이익 YoY ≥ 25%
A_ROE = 17.0                     # A: 연간 ROE ≥ 17%
N_PROXIMITY = 0.85               # N: 종가 / 52주 고가 ≥ 0.85
S1_SHARES = 50_000_000           # S1: 상장주식수 ≤ 5,000만 주 (공급)
                                 # S2 = vol_2x_bo (거래량 2배 돌파, 수요)
L_RS_SCORE = 80                  # L: 깡토 RS ≥ 80 (주도주)
MIN_MARKET_CAP = 100_000_000_000  # 시총 1,000억 이상 (페니 필터)
MIN_PRICE = 1_000
I_FLOW_TD = 60                   # I: 기관+외인 순매수 집계 거래일

# 1차 필터 (개별 조회 전 — 배지가 의미를 갖도록 임계치보다 느슨하게)
PRE_RS_MIN = 60
PRE_PROX_MIN = 0.75
MAX_DETAIL = 120                 # 개별 일봉/DART 조회 상한 (API 보호)

_Q_REPRT = {"11013": "1Q", "11012": "2Q", "11014": "3Q"}   # 사업보고서(11011)는 연간이라 C에서 제외
# 순이익 계정명은 같은 회사도 연도마다 다르게 쓴다 (실측: 지엔씨에너지 2026 1Q '당기순이익',
# 2025 1Q '당기순이익(손실)'). 정확히 일치로 잡으면 전년 값을 놓쳐 종목 전체가 결측된다.
# 다만 '비지배지분에 귀속되는 당기순이익(손실)'·'당기순이익조정을 위한 가감'은 순이익이 아니므로
# 접두 + 허용 접미만 통과시킨다 (부분일치는 이것들까지 삼킨다).
_NI_PREFIX = ("당기순이익", "분기순이익", "반기순이익")
_NI_SUFFIX = ("", "(손실)", "(순손실)", "(당기순손실)")


def _is_ni_account(nm: str) -> bool:
    nm = (nm or "").replace(" ", "")
    return any(nm.startswith(p) and nm[len(p):] in _NI_SUFFIX for p in _NI_PREFIX)


def _shares_map(date: str, market: str) -> dict[str, float]:
    """기준일 전종목 상장주식수 (S1용). 실패해도 진행 — S1 배지만 결측."""
    pykrx = pb._pykrx_stock()
    try:
        df = pykrx.get_market_cap_by_ticker(date, market=market)
    except Exception as e:
        logger.warning("상장주식수 스냅샷 %s 실패: %s", market, e)
        return {}
    return {str(c).zfill(6): float(r.get("상장주식수") or 0) for c, r in df.iterrows()}


def _flow_map(start: str, end: str, market: str) -> dict[str, float]:
    """기간 기관+외국인 순매수대금(원) — I(기관 관심도). 실패 시 {}."""
    pykrx = pb._pykrx_stock()
    out: dict[str, float] = {}
    for investor in ("기관합계", "외국인"):
        try:
            df = pykrx.get_market_net_purchases_of_equities(start, end, market, investor)
        except Exception as e:
            logger.warning("수급 %s/%s 실패: %s", market, investor, e)
            return {}
        for code, row in df.iterrows():
            v = row.get("순매수거래대금")
            if v is not None:
                out[str(code).zfill(6)] = out.get(str(code).zfill(6), 0.0) + float(v)
        time.sleep(pb.PYKRX_SLEEP)
    return out


def _acnt_all_ni(corp: str, year: int, reprt: str) -> float | None:
    """DART 전체재무제표(fnlttSinglAcntAll) → 해당 '분기 단독' 순이익.

    이 엔드포인트의 분기보고서 손익계산서 당기금액은 누적이 아니라 분기 단독이다
    (실측: 삼성전자 2025 3분기보고서 매출액 86.1조 = 3Q 단독, 3Q 누적 ~240조 아님).
    그래서 차분이 필요 없고, 전년 동기는 같은 reprt_code를 연도만 바꿔 부르면 된다.
    CFS 우선, 없으면 OFS. 미제출·조회 실패는 None.
    """
    import urllib.request
    import fetch_value as fv
    if not fv.DART_KEY:
        return None
    for fs in ("CFS", "OFS"):
        url = (f"{fv._DART}/fnlttSinglAcntAll.json?crtfc_key={fv.DART_KEY}&corp_code={corp}"
               f"&bsns_year={year}&reprt_code={reprt}&fs_div={fs}")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            logger.warning("분기 재무 %s/%s/%s/%s 실패: %s", corp, year, reprt, fs, e)
            continue
        if d.get("status") != "000" or not d.get("list"):
            continue
        for sj in ("IS", "CIS"):         # 손익계산서 우선, 없으면 포괄손익계산서
            for x in d["list"]:
                if x.get("sj_div") == sj and _is_ni_account(x.get("account_nm")):
                    v = fv._num(x.get("thstrm_amount"))
                    if v is not None:
                        return v
        logger.debug("%s/%s/%s/%s 순이익 계정 없음", corp, year, reprt, fs)
    return None


def quarter_ni_yoy(corp: str, today: dt.date | None = None) -> dict:
    """C 요건 — 최근 제출 분기의 '해당 분기' 순이익 YoY(%).

    최신 제출 분기를 찾은 뒤, 같은 분기(reprt_code)의 전년도 값과 직접 비교한다.
    누적/분기 필드 해석에 기대지 않으므로 계정 표기가 바뀌어도 흔들리지 않는다.
    전년 동기가 적자/0이면 YoY는 무의미 → None + q_turnaround 플래그로만 표기.
    """
    today = today or dt.datetime.now(tz=KST).date()
    seq = [(today.year, r) for r in ("11014", "11012", "11013")] + \
          [(today.year - 1, r) for r in ("11014", "11012", "11013")]
    for year, reprt in seq:
        cur = _acnt_all_ni(corp, year, reprt)
        if cur is None:
            continue
        prev = _acnt_all_ni(corp, year - 1, reprt)
        if prev is None:
            continue
        period = f"{year} {_Q_REPRT[reprt]}"
        if prev <= 0:
            return {"q_period": period, "q_ni": cur, "q_ni_prev": prev,
                    "q_ni_yoy": None, "q_turnaround": int(cur > 0)}
        return {"q_period": period, "q_ni": cur, "q_ni_prev": prev,
                "q_ni_yoy": round((cur - prev) / prev * 100, 1), "q_turnaround": 0}
    return {}


# 임계치 묶음 — 백테스트가 완화본을 주입하는 진입점. 값은 위 상수 그대로(라이브 불변).
# value_backtest._base_params()의 "roe_min"(실서비스 미사용, 검증축) 선례와 같은 패턴.
THRESHOLDS = {"C": C_NI_YOY, "A_growth": A_NI_YOY, "A_roe": A_ROE,
              "N": N_PROXIMITY, "S1": S1_SHARES, "L": L_RS_SCORE}


def score(rec: dict, th: dict | None = None) -> tuple[int, dict]:
    """7대 요건 비트 — C/A1(성장)/A2(ROE)/N/S1/S2/L. 결측은 미충족(0)으로 본다.

    th: 임계치 오버라이드 — 백테스트가 완화본을 주입할 때만 쓴다(실서비스는 None).
    """
    t = THRESHOLDS if not th else {**THRESHOLDS, **th}
    b = {
        "C": int((rec.get("q_ni_yoy") or -1e9) >= t["C"]),
        "A_growth": int((rec.get("ni_growth") or -1e9) >= t["A_growth"]),
        "A_roe": int((rec.get("roe") or -1e9) >= t["A_roe"]),
        "N": int((rec.get("proximity_52w") or 0) >= t["N"]),
        "S1": int(0 < (rec.get("shares") or 0) <= t["S1"]),
        "S2": int(rec.get("vol_2x_bo") == 1),
        "L": int((rec.get("rs_kkangto") or 0) >= t["L"]),
    }
    return sum(b.values()), b


def i_gate(net_억: float | None) -> int | None:
    """I 관문 — 기관+외인 순매수가 (+)인가. 0(중립)은 미통과, 결측은 판정 보류(None).

    점수와 분리해 둔 이유는 canslim_i_backtest 결과다: 8번째 비트로 넣으면 I≤0 종목이
    다른 요건으로 통과해 효과가 반감됐다.
    """
    return None if net_억 is None else int(net_억 > 0)


def _days(a: str, b: str) -> int | None:
    try:
        return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days
    except Exception:
        return None


def _track(out: list[dict], today: str, now: dt.datetime | None = None) -> tuple[list, dict, bool]:
    """편입/편출 이력 + 순위·score 변화 (눌림목 _update_history와 같은 원칙, 성과추적 없음).

    순위 Δ는 참고용이다 — 정렬 키가 (score, RS, 12M)이라 score가 그대로면 순위 변동은
    같은 점수대 안의 RS 자리바꿈일 뿐이다. 상태 변화(편입/편출·score Δ)가 본 신호.
    확정(confirm)은 마감 이후 + 스냅샷이 당일일 때만 — 장중/휴장 실행이 당일 왕복
    편입·편출을 남기지 않게 한다.
    """
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        history = []

    members = {r["ticker"]: r for r in out if r["canslim_score"] >= MEMBER_SCORE}
    for r in out:
        p = state.get(r["ticker"]) or {}
        r["rank_prev"] = p.get("rank")
        r["rank_delta"] = (p["rank"] - r["rank"]) if p.get("rank") else None   # +면 순위 상승
        r["score_prev"] = p.get("score")
        r["score_delta"] = (r["canslim_score"] - p["score"]) if p.get("score") is not None else None
        if r["ticker"] in members:
            first = p.get("first") or today
            r["first_seen"] = first
            r["is_new"] = int(bool(state) and first == today)
            r["days_in_list"] = _days(today, first) or 0

    now = now or dt.datetime.now(tz=KST)
    confirm = (now.hour, now.minute) >= (15, 30) and now.strftime("%Y-%m-%d") == today
    # 후보 급감 가드 — 데이터 부분 장애로 쪼그라든 실행이 대량 편출을 확정하는 것 방지
    # (눌림목 7/28 사례). 진짜 시장 변화라면 다음 마감 실행이 하루 늦게 확정한다.
    if confirm and len(state) >= 8 and len(members) < len(state) * 0.4:
        logger.warning("멤버 급감 (%d→%d) — 일시 장애 의심, 이번 실행은 확정 보류",
                       len(state), len(members))
        confirm = False
    if not confirm:
        logger.info("비확정 실행 (now=%s, snap=%s) — 이력/상태 동결", now.strftime("%H:%M"), today)
        return history[-30:], state, False

    new_state = {c: {"first": r["first_seen"], "rank": r["rank"],
                     "score": r["canslim_score"], "name": r["name"]}
                 for c, r in members.items()}
    if not state:                                  # 최초 실행 — diff 없이 시드만
        return history[-30:], new_state, True

    added = [{"code": c, "name": r["name"], "score": r["canslim_score"]}
             for c, r in members.items() if c not in state]
    removed = [{"code": c, "name": (state[c].get("name") or c),
                "days": _days(today, state[c].get("first") or today),
                "last_score": state[c].get("score"), "last_rank": state[c].get("rank")}
               for c in sorted(set(state) - set(members))]
    if added or removed:
        if history and history[-1].get("date") == today:   # 같은 날 재확정 → 병합 (멱등)
            last = history[-1]
            last["added"] = list({a["code"]: a for a in last.get("added", []) + added}.values())
            last["removed"] = list({r["code"]: r for r in last.get("removed", []) + removed}.values())
        else:
            history.append({"date": today, "added": added, "removed": removed})
        HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return history[-30:], new_state, True


def build() -> dict | None:
    pykrx = pb._pykrx_stock()
    dates = pb._trading_dates()
    if len(dates) < 253:
        logger.error("거래일 캘린더 부족 (%d)", len(dates))
        return None
    i0 = len(dates) - 1
    d0 = dates[i0]
    idxs = (i0, i0 - 60, i0 - 120, i0 - 252)
    logger.info("기준일 %s", d0)

    market_of, pcts, caps, shares = {}, {}, {}, {}
    flows: dict[str, float] = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        snaps = []
        for j, ix in enumerate(idxs):
            closes, cp = pb._snapshot(dates, ix, mkt)
            snaps.append(closes)
            if j == 0:
                caps.update(cp)
            time.sleep(pb.PYKRX_SLEEP)
        cur = snaps[0]
        wr = {}
        for code, c in cur.items():
            p60, p120, p252 = (s.get(code) for s in snaps[1:])
            if not (p60 and p120 and p252):
                continue
            p3, p6, p12 = c / p60 - 1, c / p120 - 1, c / p252 - 1
            wr[code] = p3 * 0.5 + p6 * 0.3 + p12 * 0.2
            pcts[code] = [round(p3 * 100, 1), round(p6 * 100, 1), round(p12 * 100, 1), 50]
            market_of[code] = mkt
        for code, rs in pb._pct_rank_map(wr).items():
            pcts[code][3] = rs
        shares.update(_shares_map(d0, mkt))
        flows.update(_flow_map(dates[max(0, i0 - I_FLOW_TD)], d0, mkt))
        logger.info("%s 스냅샷 %d종목", mkt, len(wr))

    # 이름·섹터 (scan.json 재활용)
    names, sectors = {}, {}
    try:
        for r in json.loads(SCAN_PATH.read_text(encoding="utf-8")).get("results", []):
            c = str(r.get("ticker", "")).zfill(6)
            if r.get("name"):
                names[c] = r["name"]
            if r.get("sector"):
                sectors[c] = r["sector"]
    except Exception as e:
        logger.warning("scan.json 이름/섹터 재활용 실패: %s", e)

    pool = []
    for code, (p3, p6, p12, rs) in pcts.items():
        cap = caps.get(code)
        if not cap or cap < MIN_MARKET_CAP or rs < PRE_RS_MIN:
            continue
        if not code.endswith("0"):        # 우선주·신형코드 파생 배제
            continue
        pool.append((code, p3, p6, p12, rs))
    pool.sort(key=lambda x: -x[4])
    logger.info("1차 필터 통과 %d종목", len(pool))
    pool = pool[:(int(os.environ.get("CANSLIM_LIMIT", "0")) or MAX_DETAIL)]

    import fetch_value as fv
    corp_map = fv._corp_map()
    start = (dt.datetime.now(tz=KST).date() - dt.timedelta(days=400)).strftime("%Y%m%d")
    out, fails = [], 0
    for code, p3, p6, p12, rs in pool:
        name = names.get(code)
        if not name:
            try:
                name = pykrx.get_market_ticker_name(code) or code
            except Exception:
                name = code
        if "스팩" in name:
            continue
        try:
            det = pb._detail(code, start, d0)
        except Exception as e:
            logger.warning("%s 일봉 실패: %s", code, e)
            det = None
        time.sleep(pb.PYKRX_SLEEP)
        if not det:
            fails += 1
            continue
        if det["current_price"] < MIN_PRICE or det["proximity_52w"] < PRE_PROX_MIN:
            continue
        rec = {
            "ticker": code, "name": name, "sector": sectors.get(code),
            "market": market_of.get(code),
            "pct_3m": p3, "pct_6m": p6, "pct_12m": p12,
            "rs_kkangto": rs,
            "market_cap_억": round(caps[code] / 1e8),
            "shares": shares.get(code),
            "inst_frgn_net_억": round(flows[code] / 1e8) if code in flows else None,
            "current_price": det["current_price"], "w52_high": det["w52_high"],
            "proximity_52w": det["proximity_52w"], "retrace_pct": det["retrace_pct"],
            "vol_2x_bo": det["vol_2x_bo"],
        }
        corp = corp_map.get(code)
        if corp:
            m = fv._financials(corp).get("metrics", {})
            rec["fin_year"] = m.get("year")
            rec["ni_growth"] = m.get("ni_growth")
            rec["roe"] = m.get("roe")
            rec.update(quarter_ni_yoy(corp))
        rec["canslim_score"], rec["bits"] = score(rec)
        rec["strict"] = int(rec["canslim_score"] == 7)
        # I 관문 — 점수(7점)에는 넣지 않는다. 백테스트(2026-08, canslim_i_backtest)에서
        # 8번째 비트로 넣으면 효과가 반감(+8.8% vs 관문 +14.7%)했고, 관문은 약세·강세 두
        # 구간 모두 base를 이겼다. 결측(수급 조회 실패)은 None — 게이트 판정 보류.
        rec["i_gate"] = i_gate(rec["inst_frgn_net_억"])
        out.append(rec)

    if pool and fails > len(pool) / 2:
        logger.error("일봉 확보 실패 %d/%d — 일시 장애 의심, 기존 파일 보존", fails, len(pool))
        return None

    # C 요건 확보율 — 조용히 결측되면 전 종목 C가 0점이 되어 점수가 통째로 눌린다.
    # 첫 실행(2026-08-06)이 계정명 정확일치 탓에 8/42까지 떨어진 걸 놓쳤던 지점.
    q_ok = sum(1 for r in out if r.get("q_period"))
    logger.info("분기 실적(C) 확보 %d/%d", q_ok, len(out))
    if out and q_ok < len(out) * 0.5:
        logger.warning("분기 실적 확보율 %d%% — DART 계정명/제출 시점 확인 필요",
                       round(q_ok / len(out) * 100))

    out.sort(key=lambda r: (-r["canslim_score"], -r["rs_kkangto"], -r["pct_12m"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i

    today = f"{d0[:4]}-{d0[4:6]}-{d0[6:]}"
    history, new_state, confirm = _track(out, today)

    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "snap_date": today,
        "history": history,
        "member_score": MEMBER_SCORE,
        "_state": new_state,
        "_confirm": confirm,
        "market": pb.market_direction(),
        "thresholds": {
            "c_ni_yoy": C_NI_YOY, "a_ni_yoy": A_NI_YOY, "a_roe": A_ROE,
            "n_proximity": N_PROXIMITY, "s1_shares": S1_SHARES,
            "l_rs_score": L_RS_SCORE,
            "min_market_cap_억": MIN_MARKET_CAP // 100_000_000,
            "i_flow_td": I_FLOW_TD, "score_max": 7,
            "note": "C/A는 EPS가 아닌 순이익(NI) 기준 근사 — 주식수 변동 종목은 실제 EPS와 다를 수 있음. "
                    "M(시장방향)이 correction이면 신규 진입 보류가 오닐 원전. "
                    "I(기관+외인 순매수)는 점수 미반영 관문 — 백테스트에서 8번째 비트보다 관문이 유효했음.",
        },
        "scanned": len(pool),
        "count": len(out),
        "strict_count": sum(r["strict"] for r in out),
        # 수급 조회가 통째로 실패한 날은 프론트가 게이트를 끄고 score 필터로 돌아가야 한다
        # (게이트를 그대로 적용하면 표가 빈다 — KRX 간헐 차단이 실제로 있었다).
        "i_flow_ok": int(sum(1 for r in out if r["i_gate"] is not None) >= max(1, len(out) // 2)),
        "gate_count": sum(1 for r in out if r["i_gate"] == 1 and r["canslim_score"] >= 5),
        "candidates": out,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = build()
    if data is None:
        logger.error("CANSLIM 스캔 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    new_state = data.pop("_state", {})
    confirm = data.pop("_confirm", True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if confirm:   # 멤버십 기준은 마감 확정 실행에서만 갱신
        STATE_PATH.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("저장: %s (후보 %d, 7요건 충족 %d, 시장 %s)",
                OUT_PATH, data["count"], data["strict_count"], data["market"]["status"])


if __name__ == "__main__":
    main()
