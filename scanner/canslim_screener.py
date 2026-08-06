"""CANSLIM 스크리너 — 윌리엄 오닐 7대 요건(C/A/N/S1/S2/L)+M(시장방향) 한국시장 판정.

근거: STRATEGY_CODEBASE.md §4-3 (StockEasy 이식). 임계치는 한국시장 조정본.

재사용 (새로 짜지 않은 것):
  · 거래일 캘린더·전종목 스냅샷·깡토 RS·시장방향(M) → pullback_screener
  · 52주 고가/근접도·vol_2x_bo(S2) → pullback_screener._detail
  · 연간 재무(A: 순이익 YoY·ROE) → fetch_value._financials (DART, corp_code 캐시 공용)
새로 짠 것: 분기 순이익 YoY(C) — DART 분기보고서 누적치 차분.

파이프라인:
  1) 전종목 스냅샷 → 깡토 RS + 시총 + 상장주식수
  2) 1차 필터(시총·RS·주가·우선주/스팩) → RS 상위 MAX_DETAIL 종목만 개별 일봉
  3) 일봉 → 52주 근접도(N)·거래량 2배 돌파(S2)
  4) 기관+외인 60거래일 순매수(I)
  5) DART → 분기 순이익 YoY(C), 연간 순이익 YoY·ROE(A)
  6) 7점 채점 + M(시장방향)
출력: docs/data/canslim.json (프론트 '스테이지 감지기 > CANSLIM' 하위탭이 읽음)

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

_REPRT_SEQ = ["11013", "11012", "11014", "11011"]   # 1Q · 반기 · 3Q · 사업보고서(누적)


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


def _acnt_q(corp: str, year: int, reprt: str) -> tuple[float | None, float | None]:
    """DART 분기/사업보고서 → (당기 누적 순이익, 전년 동기 누적 순이익).

    fnlttSinglAcnt의 분기 응답은 thstrm=당기 누적, frmtrm=전년 동기 누적이다
    (frmtrm_nm 예: '제 54 기 3분기'). 조회 실패/미제출은 (None, None).
    """
    import urllib.request
    import fetch_value as fv
    if not fv.DART_KEY:
        return None, None
    url = (f"{fv._DART}/fnlttSinglAcnt.json?crtfc_key={fv.DART_KEY}&corp_code={corp}"
           f"&bsns_year={year}&reprt_code={reprt}")
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        logger.warning("분기 재무 %s/%s/%s 실패: %s", corp, year, reprt, e)
        return None, None
    if d.get("status") != "000" or not d.get("list"):
        return None, None
    rows = [x for x in d["list"] if x.get("fs_div") == "CFS"] or d["list"]
    for x in rows:
        if x.get("account_nm", "").startswith("당기순이익"):
            return fv._num(x.get("thstrm_amount")), fv._num(x.get("frmtrm_amount"))
    return None, None


def quarter_ni_yoy(corp: str, today: dt.date | None = None) -> dict:
    """C 요건 — 최근 제출 분기의 '해당 분기' 순이익 YoY(%).

    누적치만 주는 DART 응답에서 분기 값을 차분으로 뽑는다:
      Q_n = 누적_n - 누적_{n-1}  (1분기는 누적이 곧 분기)
    당기·전년 동기 모두 같은 차분을 적용하므로 추가 호출은 직전 분기 1건뿐이다.
    """
    today = today or dt.datetime.now(tz=KST).date()
    # 최신 제출본 탐색 (당해 3Q → 반기 → 1Q → 전년 사업보고서 → …)
    seq = [(today.year, r) for r in reversed(_REPRT_SEQ[:3])] + \
          [(today.year - 1, r) for r in reversed(_REPRT_SEQ)]
    for year, reprt in seq:
        cur, prev = _acnt_q(corp, year, reprt)
        if cur is None or prev is None:
            continue
        i = _REPRT_SEQ.index(reprt)
        if i == 0:                       # 1분기 — 누적 = 분기
            q_cur, q_prev = cur, prev
        else:
            p_year, p_reprt = (year, _REPRT_SEQ[i - 1])
            b_cur, b_prev = _acnt_q(corp, p_year, p_reprt)
            if b_cur is None or b_prev is None:
                continue
            q_cur, q_prev = cur - b_cur, prev - b_prev
        if not q_prev or q_prev <= 0:    # 전년 적자/0 → YoY 무의미 (흑전은 별도 표기)
            return {"q_period": f"{year}-{reprt}", "q_ni": q_cur,
                    "q_ni_prev": q_prev, "q_ni_yoy": None,
                    "q_turnaround": int(q_prev is not None and q_prev <= 0 and (q_cur or 0) > 0)}
        return {"q_period": f"{year}-{reprt}", "q_ni": q_cur, "q_ni_prev": q_prev,
                "q_ni_yoy": round((q_cur - q_prev) / abs(q_prev) * 100, 1),
                "q_turnaround": 0}
    return {}


def score(rec: dict) -> tuple[int, dict]:
    """7대 요건 비트 — C/A1(성장)/A2(ROE)/N/S1/S2/L. 결측은 미충족(0)으로 본다."""
    b = {
        "C": int((rec.get("q_ni_yoy") or -1e9) >= C_NI_YOY),
        "A_growth": int((rec.get("ni_growth") or -1e9) >= A_NI_YOY),
        "A_roe": int((rec.get("roe") or -1e9) >= A_ROE),
        "N": int((rec.get("proximity_52w") or 0) >= N_PROXIMITY),
        "S1": int(0 < (rec.get("shares") or 0) <= S1_SHARES),
        "S2": int(rec.get("vol_2x_bo") == 1),
        "L": int((rec.get("rs_kkangto") or 0) >= L_RS_SCORE),
    }
    return sum(b.values()), b


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
        out.append(rec)

    if pool and fails > len(pool) / 2:
        logger.error("일봉 확보 실패 %d/%d — 일시 장애 의심, 기존 파일 보존", fails, len(pool))
        return None

    out.sort(key=lambda r: (-r["canslim_score"], -r["rs_kkangto"], -r["pct_12m"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i

    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "snap_date": f"{d0[:4]}-{d0[4:6]}-{d0[6:]}",
        "market": pb.market_direction(),
        "thresholds": {
            "c_ni_yoy": C_NI_YOY, "a_ni_yoy": A_NI_YOY, "a_roe": A_ROE,
            "n_proximity": N_PROXIMITY, "s1_shares": S1_SHARES,
            "l_rs_score": L_RS_SCORE,
            "min_market_cap_억": MIN_MARKET_CAP // 100_000_000,
            "i_flow_td": I_FLOW_TD, "score_max": 7,
            "note": "C/A는 EPS가 아닌 순이익(NI) 기준 근사 — 주식수 변동 종목은 실제 EPS와 다를 수 있음. "
                    "M(시장방향)이 correction이면 신규 진입 보류가 오닐 원전.",
        },
        "scanned": len(pool),
        "count": len(out),
        "strict_count": sum(r["strict"] for r in out),
        "candidates": out,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = build()
    if data is None:
        logger.error("CANSLIM 스캔 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("저장: %s (후보 %d, 7요건 충족 %d, 시장 %s)",
                OUT_PATH, data["count"], data["strict_count"], data["market"]["status"])


if __name__ == "__main__":
    main()
