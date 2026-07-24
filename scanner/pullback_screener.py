"""
깡토 눌림목 스크리너 — 강한 상승 추세 후 건전한 조정(눌림)+응축(VCP)을 거친 종목 발굴.

근거: 강주현 『손실은 짧게 수익은 길게』 SOP (깡토 눌림목 매수 전략 사양서 v1.0).
백테스트(2021-01~2026-05 워크포워드): score≥4 진입 + 90일 보유 + 시장방향 필터가 핵심.

파이프라인:
  1) pykrx 전종목 종가 스냅샷 4개 시점(당일/-60/-120/-252 거래일) → pct_3m/6m/12m
  2) 깡토 RS = percentile(0.5*pct_3m + 0.3*pct_6m + 0.2*pct_12m, 시장별 KOSPI/KOSDAQ) × 98 + 1
  3) 하드필터: pct_6m≥30% AND pct_12m≥50% AND 시총≥5,000억 AND 0.65≤prox52≤0.92
  4) 통과 종목만 개별 1년 일봉 조회 → 응축 지표(consolidation_days, vol_dry, VCP, vol_2x_bo)
  5) 후보에 한해 WISEreport 다개년 컨센서스(consensus.fetch_consensus_multi) → PER 가속 bit
  6) 점수화(0~7, 원 사양 §4.2) + 시장방향(KOSPI 200MA+분배일)
출력: docs/data/pullback.json (프론트 '전략실 > 눌림목' 탭이 읽음)

주의: PER 가속(fwd1 PER > fwd2 PER > 0)은 하드필터가 아닌 가점(+1)으로만 사용 —
      컨센서스 미제공(중소형) 종목이 필터로 전멸하는 것을 방지. 사양 §4.1과 다른 점.
실행: 매일 장마감 후 pullback.yml. 테스트: PULLBACK_LIMIT=10 으로 개별 조회 수 제한.
실패 정책: 응축 지표 확보가 후보의 절반 미만이면 기존 출력 보존 후 exit 1.
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

logger = logging.getLogger("pullback")

_PYKRX = None


def _pykrx_stock():
    """pykrx 지연 import + 재시도. pykrx는 import 시점에 KRX 로그인(KRX_ID/PW)을 시도하는데,
    KRX가 비JSON 응답(간헐 차단)을 주면 import 자체가 죽는다 — 재시도 후 최후엔 무로그인 폴백."""
    global _PYKRX
    if _PYKRX is not None:
        return _PYKRX
    for attempt in range(3):
        try:
            from pykrx import stock
            _PYKRX = stock
            return stock
        except Exception as e:
            logger.warning("pykrx import 실패(시도 %d, KRX 로그인 이슈 추정): %s", attempt + 1, e)
            for m in [k for k in list(sys.modules) if k.startswith("pykrx")]:
                sys.modules.pop(m, None)
            time.sleep(5 * (attempt + 1))
    logger.warning("KRX 로그인 포기 — 무로그인으로 pykrx 재시도")
    os.environ.pop("KRX_ID", None)
    os.environ.pop("KRX_PW", None)
    from pykrx import stock
    _PYKRX = stock
    return stock
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "pullback.json"
STATE_PATH = ROOT / "docs" / "data" / "pullback_state.json"      # {code: first_seen}
HISTORY_PATH = ROOT / "docs" / "data" / "pullback_history.json"  # append-only 편입/편출
SCAN_PATH = ROOT / "docs" / "data" / "scan.json"

# ── 기준 (사양서 §3~4 — 완화하려면 여기만 수정) ──
PCT_6M_MIN = 30.0            # 6개월 수익률 하한 (%)
PCT_12M_MIN = 50.0           # 12개월 수익률 하한 (%)
PROX_MIN, PROX_MAX = 0.65, 0.92   # 52주 고가 근접도 밴드 (조정 8~35%)
DEEP_PROX_MAX = 0.85         # 깊은 눌림 가점 기준
MIN_CAP = 500_000_000_000    # 시총 5,000억 이상 (중·대형주)
CONSOL_DAYS = 5              # 단기 베이스 (직전 고점 후 경과일)
CONSOL_TIGHT = 15            # 정석 베이스 (VCP 3주+)
DRY_RATIO = 0.85             # 거래량 소멸: vol5/vol60 ≤ 0.85
RS_LEADER = 70               # 깡토 RS 주도주 기준
MIN_PRICE = 1_000            # 동전주 배제
MAX_DETAIL = 150             # 개별 일봉 조회 상한 (API 보호)
PYKRX_SLEEP = 0.25
# 시장방향 (M — KOSPI 분배일 + MA200)
DIST_WINDOW, DIST_THRESH, DIST_WARN = 20, 5, 3
MA_WINDOW, MA_BUFFER = 200, 0.03


def _trading_dates(days: int = 420) -> list[str]:
    """KOSPI 지수 일봉으로 최근 거래일 목록(YYYYMMDD 오름차순) 확보."""
    end = dt.datetime.now(tz=KST).date()
    start = end - dt.timedelta(days=int(days * 1.6))
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader("KS11", start.isoformat(), end.isoformat())
        if df is not None and len(df) >= 260:
            return [d.strftime("%Y%m%d") for d in df.index]
    except Exception as e:
        logger.warning("FDR KS11 실패: %s — pykrx 지수로 대체", e)
    pykrx = _pykrx_stock()
    df = pykrx.get_index_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "1001")
    return [d.strftime("%Y%m%d") for d in df.index]


def _kospi_series(days: int = 320):
    """KOSPI 종가·거래량 시계열 (시장방향 계산용)."""
    end = dt.datetime.now(tz=KST).date()
    start = end - dt.timedelta(days=int(days * 1.6))
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader("KS11", start.isoformat(), end.isoformat())
        closes = [float(v) for v in df["Close"]]
        vols = [float(v) for v in df.get("Volume", [])]
        if len(closes) >= MA_WINDOW:
            return closes, vols, df.index[-1].strftime("%Y-%m-%d")
    except Exception as e:
        logger.warning("FDR KS11 시계열 실패: %s", e)
    pykrx = _pykrx_stock()
    df = pykrx.get_index_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "1001")
    return ([float(v) for v in df["종가"]], [float(v) for v in df["거래량"]],
            df.index[-1].strftime("%Y-%m-%d"))


def market_direction() -> dict:
    """사양서 §3.6 — 분배일 + MA200 기반 시장 상태."""
    closes, vols, base = _kospi_series()
    ma200 = sum(closes[-MA_WINDOW:]) / MA_WINDOW
    kospi = closes[-1]
    ratio = (kospi - ma200) / ma200
    dist = 0
    for i in range(len(closes) - DIST_WINDOW, len(closes)):
        if i <= 0:
            continue
        chg = (closes[i] - closes[i - 1]) / closes[i - 1]
        if chg < 0.002 and vols and vols[i] > vols[i - 1]:
            dist += 1
    if ratio < -MA_BUFFER or dist >= DIST_THRESH:
        status, label, color = "correction", "조정 국면", "red"
    elif dist >= DIST_WARN:
        status, label, color = "under_pressure", "상승 압박", "yellow"
    else:
        status, label, color = "confirmed_uptrend", "상승 확정", "green"
    return {
        "status": status, "label": label, "color": color,
        "kospi_close": round(kospi, 2), "ma200": round(ma200, 2),
        "ma200_ratio_pct": round(ratio * 100, 2),
        "dist_days": dist, "dist_window": DIST_WINDOW, "base_date": base,
    }


def _snapshot(dates: list[str], idx: int, market: str) -> tuple[dict[str, float], dict[str, float]]:
    """거래일 dates[idx] 전종목 (종가맵, 시총맵). KRX 간헐 차단 대비 재시도 + 직전 거래일 백오프.
    (get_market_cap_by_ticker는 value_screen·run_scan이 CI에서 검증한 엔드포인트)"""
    pykrx = _pykrx_stock()
    for back in range(3):                     # 하루씩 백오프 (수익률 오차 미미)
        date = dates[max(0, idx - back)]
        for attempt in range(3):
            try:
                df = pykrx.get_market_cap_by_ticker(date, market=market)
                closes, caps = {}, {}
                for code, row in df.iterrows():
                    c = float(row.get("종가") or 0)
                    if c > 0:
                        code6 = str(code).zfill(6)
                        closes[code6] = c
                        caps[code6] = float(row.get("시가총액") or 0)
                if len(closes) >= 100:        # 미정산·빈 응답 방어
                    return closes, caps
            except Exception as e:
                logger.warning("스냅샷 %s %s 시도 %d 실패: %s", market, date, attempt + 1, e)
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"{market} {dates[idx]} 스냅샷 확보 실패")


def _pct_rank_map(vals: dict[str, float]) -> dict[str, int]:
    """깡토 RS: w_return 오름차순 백분위 × 98 + 1 (1~99)."""
    items = sorted(vals.items(), key=lambda x: x[1])
    n = len(items)
    if n < 2:
        return {k: 50 for k in vals}
    return {code: round(i / (n - 1) * 98 + 1) for i, (code, _) in enumerate(items)}


def _detail(code: str, start: str, end: str) -> dict | None:
    """개별 1년 일봉 → 응축 지표 (사양서 §3.2~3.3, 3.7)."""
    pykrx = _pykrx_stock()
    df = pykrx.get_market_ohlcv(start, end, code)
    if df is None or len(df) < 120:
        return None
    closes = [float(v) for v in df["종가"]]
    vols = [float(v) for v in df["거래량"]]
    w52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    close = closes[-1]
    prox = close / w52 if w52 else None
    # 응축: 최근 60거래일 창에서 직전 고점 갱신 후 경과일
    win = closes[-60:]
    consol = len(win) - 1 - max(range(len(win)), key=lambda i: win[i])
    vol5 = sum(vols[-5:]) / 5
    vol60 = sum(vols[-60:]) / min(60, len(vols))
    dry_ratio = round(vol5 / vol60, 2) if vol60 else None
    # VCP: 20일 수익률 표준편차가 직전 20일 대비 축소
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]

    def _std(xs):
        m = sum(xs) / len(xs)
        return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5
    contracting = int(len(rets) >= 40 and _std(rets[-20:]) < _std(rets[-40:-20]))
    # vol_2x_bo: 최근 20거래일 중 거래량 2배 + 상승 마감일 존재
    vol2x = 0
    for i in range(max(1, len(vols) - 20), len(vols)):
        v20 = sum(vols[max(0, i - 20):i]) / min(20, i)
        if v20 and vols[i] > 2 * v20 and closes[i] > closes[i - 1]:
            vol2x = 1
            break
    return {
        "current_price": close, "w52_high": w52,
        "proximity_52w": round(prox, 4), "retrace_pct": round((1 - prox) * 100, 1),
        "pivot": w52, "distance_to_pivot_pct": round((w52 / close - 1) * 100, 1),
        "consolidation_days": consol,
        "vol_contracting": contracting,
        "vol_dry_ratio": dry_ratio,
        "vol_drying": int(dry_ratio is not None and dry_ratio <= DRY_RATIO),
        "vol_2x_bo": vol2x,
    }


def _consensus_per(code: str, close: float) -> dict:
    """다개년 컨센서스 → 포워드 PER·PER 가속 bit. 미제공 시 {} (가점 없음)."""
    import consensus
    fwd = consensus.fetch_consensus_multi(code)
    time.sleep(0.4)   # WISEreport 요청 예의 지연 (value_screen과 동일)
    if len(fwd) < 2:
        return {}
    eps1, eps2 = fwd[0]["eps"], fwd[1]["eps"]
    out = {
        "fwd_year1": fwd[0]["year"], "fwd_year2": fwd[1]["year"],
        "per_fwd1": round(close / eps1, 1) if eps1 > 0 else None,
        "per_fwd2": round(close / eps2, 1) if eps2 > 0 else None,
        # 사양 §3.5: per_fwd1 > per_fwd2 > 0 ⇔ eps2 > eps1 > 0 (실적 가속)
        "per_accel": int(eps1 > 0 and eps2 > eps1),
    }
    if out["per_fwd1"] and out["per_fwd2"]:
        out["per_delta_pct"] = round((out["per_fwd1"] - out["per_fwd2"]) / out["per_fwd1"] * 100, 1)
    return out


def _score(rec: dict) -> tuple[int, dict]:
    """사양서 §4.2 — 7점 만점 (PER 가속은 컨센서스 제공 종목만 가점 가능)."""
    b = {
        "per_accel": int(rec.get("per_accel") == 1),
        "deep_pullback": int(rec["proximity_52w"] <= DEEP_PROX_MAX),
        "base_short": int(rec["consolidation_days"] >= CONSOL_DAYS),
        "base_tight": int(rec["consolidation_days"] >= CONSOL_TIGHT),
        "vcp": int(rec["vol_contracting"] == 1),
        "vol_dry": int(rec["vol_drying"] == 1),
        "rs_leader": int(rec["rs_kkangto"] >= RS_LEADER),
    }
    return sum(b.values()), b


def _update_history(cands: list[dict], prev_state: dict, today: str) -> list:
    """편입/편출 이력 누적 (append-only, quality_growth 패턴).
    편출 기록에는 당시 score·가격·체류일을 남겨 사후 성과 추적(90일 보유 룰 운영)에 쓴다."""
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        history = []
    prev_codes = set(prev_state)
    if not prev_codes:                      # 최초 실행 — diff 없음
        return history[-30:]
    try:
        prev_by_code = {c["ticker"]: c
                        for c in json.loads(OUT_PATH.read_text(encoding="utf-8"))["candidates"]}
    except Exception:
        prev_by_code = {}
    cur = {r["ticker"]: r for r in cands}
    added = [{"code": c, "name": r["name"], "score": r["pullback_score"]}
             for c, r in cur.items() if c not in prev_codes]
    removed = []
    for c in sorted(prev_codes - set(cur)):
        p = prev_by_code.get(c, {})
        first = prev_state.get(c)
        days = None
        if first:
            try:
                days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(first)).days
            except Exception:
                pass
        removed.append({"code": c, "name": p.get("name", c), "first_seen": first,
                        "days": days, "last_score": p.get("pullback_score"),
                        "last_price": p.get("current_price")})
    if added or removed:
        history.append({"date": today, "added": added, "removed": removed})
        HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return history[-30:]


def build() -> dict | None:
    pykrx = _pykrx_stock()
    dates = _trading_dates()
    if len(dates) < 253:
        logger.error("거래일 캘린더 부족 (%d)", len(dates))
        return None
    # 장중/미정산이면 당일 스냅샷이 비므로 _snapshot 이 자체 백오프하지만,
    # 15:35 이후 실행 전제로 마지막 거래일을 기준으로 삼는다.
    i0 = len(dates) - 1
    idxs = (i0, i0 - 60, i0 - 120, i0 - 252)
    d0 = dates[i0]
    logger.info("기준일 %s (-60:%s -120:%s -252:%s)",
                d0, dates[idxs[1]], dates[idxs[2]], dates[idxs[3]])

    market_of, w_return, pcts, caps = {}, {}, {}, {}
    for mkt in ("KOSPI", "KOSDAQ"):
        snaps = []
        for j, ix in enumerate(idxs):
            closes, cp = _snapshot(dates, ix, mkt)
            snaps.append(closes)
            if j == 0:
                caps.update(cp)
            time.sleep(PYKRX_SLEEP)
        cur = snaps[0]
        wr = {}
        for code, c in cur.items():
            p60, p120, p252 = (s.get(code) for s in snaps[1:])
            if not (p60 and p120 and p252):
                continue
            p3, p6, p12 = c / p60 - 1, c / p120 - 1, c / p252 - 1
            wr[code] = p3 * 0.5 + p6 * 0.3 + p12 * 0.2
            pcts[code] = (round(p3 * 100, 1), round(p6 * 100, 1), round(p12 * 100, 1))
            market_of[code] = mkt
        w_return.update(wr)
        rs_map = _pct_rank_map(wr)
        for code, rs in rs_map.items():
            pcts[code] = (*pcts[code], rs)
        logger.info("%s 스냅샷 %d종목", mkt, len(wr))

    # 이름·트레일링 PER (전종목 원샷 — 실패해도 진행)
    names, pers = {}, {}
    try:
        fund_df = pykrx.get_market_fundamental_by_ticker(d0, market="ALL")
        for code, row in fund_df.iterrows():
            per = float(row.get("PER") or 0)
            if per > 0:
                pers[str(code).zfill(6)] = per
    except Exception as e:
        logger.warning("펀더멘털 스냅샷 실패: %s", e)

    # 하드필터 (개별 조회 전 1차: 수익률·시총·동전주·스팩·우선주)
    pool = []
    for code, (p3, p6, p12, rs) in pcts.items():
        if p6 < PCT_6M_MIN or p12 < PCT_12M_MIN:
            continue
        cap = caps.get(code)
        if not cap or cap < MIN_CAP:
            continue
        if not code.endswith("0"):   # 우선주·신형코드 파생 배제
            continue
        pool.append((code, p3, p6, p12, rs))
    pool.sort(key=lambda x: -x[4])   # RS 높은 순으로 조회
    logger.info("1차 필터 통과 %d종목", len(pool))
    limit = int(os.environ.get("PULLBACK_LIMIT", "0")) or MAX_DETAIL
    pool = pool[:limit]

    # 이름·섹터 (scan.json 재활용 + pykrx 보완)
    sectors = {}
    try:
        scan = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
        for r in scan.get("results", []):
            c = str(r.get("ticker", "")).zfill(6)
            if r.get("name"):
                names[c] = r["name"]
            if r.get("sector"):
                sectors[c] = r["sector"]
    except Exception:
        pass

    start = (dt.datetime.now(tz=KST).date() - dt.timedelta(days=400)).strftime("%Y%m%d")
    out, fails = [], 0
    for code, p3, p6, p12, rs in pool:
        name = names.get(code)
        if not name:
            try:
                nm = pykrx.get_market_ticker_name(code)
                name = nm if isinstance(nm, str) and nm else code
            except Exception:
                name = code
        if "스팩" in name:
            continue
        try:
            det = _detail(code, start, d0)
        except Exception as e:
            logger.warning("%s 일봉 실패: %s", code, e)
            det = None
        time.sleep(PYKRX_SLEEP)
        if not det:
            fails += 1
            continue
        if det["current_price"] < MIN_PRICE:
            continue
        prox = det["proximity_52w"]
        if not (PROX_MIN <= prox <= PROX_MAX):
            continue   # 신고가 추격(>0.92) 또는 과대 조정(<0.65) 배제
        rec = {
            "ticker": code, "name": name, "sector": sectors.get(code),
            "market": market_of.get(code),
            "pct_3m": p3, "pct_6m": p6, "pct_12m": p12,
            "rs_kkangto": rs,
            "per_trailing": pers.get(code),
            "market_cap_억": round(caps[code] / 1e8),
            **det,
        }
        rec.update(_consensus_per(code, det["current_price"]))
        rec["pullback_score"], rec["score_bits"] = _score(rec)
        out.append(rec)

    if pool and fails > len(pool) / 2:
        logger.error("일봉 확보 실패 %d/%d — 일시 장애 의심, 기존 파일 보존", fails, len(pool))
        return None

    # 사양 §4.3: score → RS → PER 개선폭 → 12M 수익률
    out.sort(key=lambda r: (-r["pullback_score"], -r["rs_kkangto"],
                            -(r.get("per_delta_pct") if r.get("per_delta_pct") is not None else -1e9),
                            -r["pct_12m"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i

    # 편입일 추적 (신규 배지·90일 보유 룰 운영용) + 편입/편출 이력
    today = f"{d0[:4]}-{d0[4:6]}-{d0[6:]}"
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    for r in out:
        first = state.get(r["ticker"], today)
        r["first_seen"] = first
        r["is_new"] = int(first == today and bool(state))
        try:
            r["days_in_list"] = (dt.date.fromisoformat(today) - dt.date.fromisoformat(first)).days
        except Exception:
            r["days_in_list"] = 0
    history = _update_history(out, state, today)

    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "snap_date": today,
        "_state": {r["ticker"]: r["first_seen"] for r in out},
        "history": history,
        "market": market_direction(),
        "thresholds": {
            "pct_6m_min": PCT_6M_MIN, "pct_12m_min": PCT_12M_MIN,
            "prox_min": PROX_MIN, "prox_max": PROX_MAX, "deep_prox_max": DEEP_PROX_MAX,
            "consol_days": CONSOL_DAYS, "consol_tight": CONSOL_TIGHT,
            "dry_ratio": DRY_RATIO, "rs_leader": RS_LEADER,
            "min_market_cap_억": MIN_CAP // 100_000_000,
            "score_max": 7,
            "note": "7점 만점(PER 가속은 컨센서스 제공 종목만 가점) — 권장 진입선 4점. "
                    "백테스트(2021-01~2026-05): score≥4 + 90일 보유 + 시장방향 필터.",
        },
        "scanned": len(pool), "count": len(out), "candidates": out,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = build()
    if data is None:
        logger.error("눌림목 스캔 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    new_state = data.pop("_state", {})
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    STATE_PATH.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
    n4 = sum(1 for c in data["candidates"] if c["pullback_score"] >= 4)
    logger.info("저장: %s (후보 %d, score≥4 %d, 시장 %s)",
                OUT_PATH, data["count"], n4, data["market"]["status"])


if __name__ == "__main__":
    main()
