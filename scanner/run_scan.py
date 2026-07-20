"""
추세추종 스테이지 감지 — GitHub Actions용 1회 실행 스캐너

실행 흐름:
  1. 코스피200 + 코스닥150 구성종목 로드
  2. RS 유니버스 계산 (pykrx → KIS → FDR fallback)
  3. 장중이면 KIS 실시간 가격으로 가상 캔들 보강
  4. 전 종목 스테이지 분석
  5. 편입/이탈 원장(ledger) 갱신 — 한 번 편입되면 이탈 조건 전까지 유지
  6. docs/data/*.json 출력 (GitHub Pages가 서빙)

상태 파일(docs/data/state.json)은 Actions가 커밋해서 다음 실행으로 이어진다.
"""

import asyncio
import datetime as dt
import json
import logging
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from data_provider import (
    KST,
    fetch_index_constituents,
    fetch_kospi_status,
    fetch_market_cap,
    fetch_ohlcv,
    fetch_realtime_prices_batch,
    is_market_hours,
    load_config,
)
from stage_detector import analyze_stock
from drawdown_metrics import derive_drawdown_metrics

logger = logging.getLogger("run_scan")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "docs" / "data"
STATE_PATH = DATA_DIR / "state.json"      # stage_history + rs_history + ledger (실행 간 유지)
SCAN_PATH = DATA_DIR / "scan.json"        # 스캔 결과 (프론트가 읽음)
TRACKING_PATH = DATA_DIR / "tracking.json"  # 보유/이탈 원장 (프론트가 읽음)

RS_PERIODS = [("q1", 63, 0.4), ("q2", 126, 0.2), ("q3", 189, 0.2), ("q4", 252, 0.2)]

# ── 섹터 대표 ETF (섹터 맵 사분면용 — 시장 전체를 대표하면서 조회는 섹터당 1종목) ──
# 키는 sector_map.json의 섹터 슬러그와 일치해야 한다. 대표 ETF가 없거나 조회 실패한
# 섹터는 사분면에서 제외된다(프론트가 있는 것만 그림).
SECTOR_ETFS = {
    "semiconductor": ("091160", "KODEX 반도체"),
    "robotics": ("445290", "KODEX K-로봇액티브"),
    "it-service-sw": ("157490", "TIGER 소프트웨어"),
    "auto-mobility": ("091180", "KODEX 자동차"),
    "energy": ("377990", "TIGER Fn신재생에너지"),
    "finance": ("139270", "TIGER 200 금융"),
    "aero-defense-space": ("449450", "PLUS K방산"),
    "resource-materials": ("139240", "TIGER 200 철강소재"),
    "battery-renewable": ("305720", "KODEX 2차전지산업"),
    "bio-healthcare": ("143860", "TIGER 헬스케어"),
    "shipbuilding-shipping": ("466920", "SOL 조선TOP3플러스"),
    "telecom": ("098560", "TIGER 방송통신"),
    "construction-realestate": ("117700", "KODEX 건설"),
    "media-ent-game": ("364990", "TIGER 미디어컨텐츠"),
    "retail-fashion-beauty": ("228790", "TIGER 화장품"),
    "chem-materials": ("117460", "KODEX 에너지화학"),
    "food-agri-fishery": ("266410", "KODEX 필수소비재"),
}


async def _compute_sector_etf_rs() -> dict:
    """섹터 대표 ETF의 장기 RS(1/3/6개월 가중 수익률 백분위)와 단기 모멘텀(5일 수익률 백분위).
    사분면(부상/주도/과열/소외)용. trail = 1주/2주/3주 전 시점의 스냅샷 — '최근 흐름' 궤적 표시용."""
    closes_map = {}
    for slug, (code, name) in SECTOR_ETFS.items():
        try:
            df = await fetch_ohlcv(code, days=300)
            if df is not None and len(df) >= 150:
                closes_map[slug] = (code, name, df["close"])
        except Exception:
            continue

    def pct_rank(vals):
        order = sorted(vals)
        return {v: round(100 * i / max(1, len(order) - 1)) for i, v in enumerate(order)}

    def snapshot(offset):
        """offset 거래일 전 시점의 섹터별 (장기점수, 단기점수, 기간수익률)"""
        rows = {}
        for slug, (code, name, closes) in closes_map.items():
            c = closes.iloc[:-offset] if offset else closes
            if len(c) < 130:
                continue
            cur = float(c.iloc[-1])
            rets = {}
            for label, days in (("m1", 21), ("m3", 63), ("m6", 126)):
                if len(c) > days:
                    old = float(c.iloc[-days - 1])
                    if old > 0:
                        rets[label] = (cur / old - 1) * 100
            if "m3" not in rets:
                continue
            d5 = (cur / float(c.iloc[-6]) - 1) * 100 if len(c) > 6 else 0
            rows[slug] = {"long": rets.get("m1", 0) * 0.5 + rets.get("m3", 0) * 0.3 + rets.get("m6", 0) * 0.2,
                          "short": d5, "rets": rets}
        if not rows:
            return {}
        lp = pct_rank([r["long"] for r in rows.values()])
        sp = pct_rank([r["short"] for r in rows.values()])
        return {slug: {"long_rs": lp[r["long"]], "short_rs": sp[r["short"]], "rets": r["rets"], "d5": r["short"]}
                for slug, r in rows.items()}

    snaps = {k: snapshot(off) for k, off in (("now", 0), ("w1", 5), ("w2", 10), ("w3", 15))}
    out = {}
    for slug, (code, name, _c) in closes_map.items():
        cur = snaps["now"].get(slug)
        if not cur:
            continue
        out[slug] = {
            "etf": code, "etf_name": name,
            "long_rs": cur["long_rs"], "short_rs": cur["short_rs"],
            "ret_m1": round(cur["rets"].get("m1", 0), 2), "ret_m3": round(cur["rets"].get("m3", 0), 2),
            "ret_m6": round(cur["rets"].get("m6", 0), 2), "ret_d5": round(cur["d5"], 2),
            "trail": [{"k": k, "long_rs": snaps[k][slug]["long_rs"], "short_rs": snaps[k][slug]["short_rs"]}
                      for k in ("w1", "w2", "w3") if slug in snaps.get(k, {})],
        }
    return out


# ── JSON NaN/Inf 세이프 변환 ──
def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return _sanitize(obj.tolist())
    return obj


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("state.json 로드 실패: %s", e)
    return {"stage_history": {}, "rs_history": [], "ledger": {"holdings": [], "exited": []}}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_sanitize(data), ensure_ascii=False, indent=1), encoding="utf-8")


# ── RS 유니버스 계산 (main.py에서 이식, 섹터 RS 집계 제외) ──
def _compute_rs_sync() -> tuple[dict, dict, dict]:
    """returns (ticker_map, sector_map, period_returns)"""
    from pykrx import stock as krx

    ticker_map: dict = {}
    sector_map: dict = {}

    # 거래일 탐색 — 전용 함수 1회 호출 (기존: 전종목 스냅샷 최대 7회, KRX 차단 유발 요인)
    end_str = None
    end_date = None
    try:
        end_str = krx.get_nearest_business_day_in_a_week()
        end_date = dt.datetime.strptime(end_str, "%Y%m%d").date()
    except Exception:
        pass

    # 종목명 + 섹터 (FDR KRX-DESC)
    try:
        import FinanceDataReader as fdr
        df_desc = fdr.StockListing("KRX-DESC")
        if df_desc is not None and not df_desc.empty:
            for _, row in df_desc.iterrows():
                code = str(row.get("Code", "")).strip()
                name = str(row.get("Name", "")).strip()
                industry = str(row.get("Industry", "")).strip()
                if len(code) == 6 and code.isdigit():
                    if name:
                        ticker_map.setdefault(code, name)
                    if industry and industry != "nan":
                        sector_map[code] = industry
    except Exception as e:
        logger.warning("FDR KRX-DESC 로드 실패: %s", e)

    if not end_str:
        logger.warning("pykrx 거래일 감지 실패 — RS는 fallback 경로 사용")
        return ticker_map, sector_map, {}

    # 종목명 보충 — 시장당 일괄 1회 조회 (기존: 미해결 종목당 개별 KRX 호출 수백 회 → 차단 유발)
    for market in ["KOSPI", "KOSDAQ"]:
        try:
            names = krx.get_market_ticker_and_name(end_str, market=market)
            for code, name in names.items():
                ticker_map.setdefault(str(code), str(name))
        except Exception:
            pass

    period_returns: dict = {}
    for label, days, _w in RS_PERIODS:
        start_str = (end_date - dt.timedelta(days=days)).strftime("%Y%m%d")
        ret_map: dict = {}
        for market in ["KOSPI", "KOSDAQ"]:
            try:
                df = krx.get_market_price_change(start_str, end_str, market=market)
                if df is not None and not df.empty:
                    for ticker in df.index:
                        try:
                            ret_map[ticker] = float(df.loc[ticker, "등락률"])
                        except Exception:
                            pass
            except Exception as e:
                logger.warning("RS %s(%dd) %s 실패: %s", label, days, market, e)
        if ret_map:
            period_returns[label] = ret_map
            logger.info("RS %s(%dd): %d 종목", label, days, len(ret_map))

    return ticker_map, sector_map, period_returns


def _rs_from_ohlcv_map(ohlcv_map: dict) -> dict:
    """이미 조회한 OHLCV로 다중기간 수익률 계산 — 추가 API 호출 없음"""
    period_returns: dict = {}
    for ticker, df in ohlcv_map.items():
        if df is None or df.empty or len(df) < 63:
            continue
        closes = df["close"]
        cur = float(closes.iloc[-1])
        for label, days, _w in RS_PERIODS:
            if len(closes) >= days:
                old = float(closes.iloc[-days])
                if old > 0:
                    period_returns.setdefault(label, {})[ticker] = (cur / old - 1) * 100
    logger.info("OHLCV 기반 RS fallback: %d 종목", len(ohlcv_map))
    return period_returns


def _composite_rs(period_returns: dict) -> dict:
    """기간별 수익률 → 가중 백분위 합산 → 최종 백분위 (0~99)"""
    period_ranks = {}
    for label, days, weight in RS_PERIODS:
        if label not in period_returns:
            continue
        ret_map = period_returns[label]
        tlist = list(ret_map.keys())
        rets = np.array([ret_map[t] for t in tlist])
        ranks = np.argsort(np.argsort(rets)) / max(len(rets) - 1, 1) * 99
        period_ranks[label] = dict(zip(tlist, ranks))

    all_tickers = set()
    for m in period_ranks.values():
        all_tickers.update(m.keys())

    composite = {}
    for ticker in all_tickers:
        weighted_sum = 0.0
        total_weight = 0.0
        for label, days, weight in RS_PERIODS:
            if label in period_ranks and ticker in period_ranks[label]:
                weighted_sum += period_ranks[label][ticker] * weight
                total_weight += weight
        if total_weight > 0:
            composite[ticker] = weighted_sum / total_weight

    if not composite:
        return {}
    tlist = list(composite.keys())
    scores = np.array([composite[t] for t in tlist])
    ranks = np.argsort(np.argsort(scores)) / max(len(scores) - 1, 1) * 99
    return {t: round(float(r), 1) for t, r in zip(tlist, ranks)}


def _rs_momentum(rs_history: list, ticker: str, current_rs: float) -> tuple[float, bool]:
    if len(rs_history) < 2:
        return 0.0, False
    oldest = rs_history[0].get("rs", {})
    old_rs = oldest.get(ticker, current_rs)
    all_hist = [snap.get("rs", {}).get(ticker, 0) for snap in rs_history]
    is_new_high = current_rs >= max(all_hist) if all_hist else False
    return round(current_rs - old_rs, 1), is_new_high


def _append_virtual_candle(df: pd.DataFrame, rt: dict) -> pd.DataFrame:
    today = dt.datetime.now(tz=KST).date()
    if not df.empty and df.index[-1].date() >= today:
        return df
    new_row = pd.DataFrame(
        [{"open": rt["open"], "high": rt["high"], "low": rt["low"],
          "close": rt["close"], "volume": rt["volume"]}],
        index=pd.DatetimeIndex([pd.Timestamp(today)]),
    )
    return pd.concat([df, new_row])


# ── 스테이지 히스토리 (체류 기간) ──
def _update_stage_history(stage_history: dict, results: list[dict]):
    today = dt.date.today()
    today_str = today.isoformat()
    for r in results:
        ticker = r.get("ticker")
        if not ticker:
            continue
        entry = stage_history.get(ticker)
        if entry is None or entry["stage"] != r.get("stage"):
            stage_history[ticker] = {"stage": r.get("stage"), "entry_date": today_str}
            r["days_in_stage"] = 0
        else:
            r["days_in_stage"] = (today - dt.date.fromisoformat(entry["entry_date"])).days


# ── 편입/이탈 원장 (tracking v3) ─────────────────────────
# 스탁이지 전략실 매매 내역 역산(모멘텀 226건·피크 346건, 2025-06~2026-07) 기반:
#   - 익절 규칙 없음 — 승자는 추세선이 깨질 때까지 보유 (+244%, +277% 사례)
#   - 시장 급락일에도 전량 청산 없음 — 시장 신호는 신규 편입 차단 전용
# 규칙 (config.json params 사용):
#   편입: 독립 트랙 (스탁이지의 1호/2호 전략실 방식)
#     - 돌파(신고가) 트랙[id=3]: 감지기 Stage 3 돌파 + 신뢰도 stage3_entry_confidence(70)↑
#     - 모멘텀(웨지 팝) 트랙[id=1]: 웨지 팝 신호(10·20 EMA 동시 탈환 + 거래량 2~3x, 스테이지와 독립)
#       + RS≥70 + 웨지 신뢰도 stage1_entry_confidence(75)↑
#     공통: KOSPI 진입 허용 + 클라이맥스 경고 없음. 같은 종목이 두 트랙에 각각 편입 가능.
#   유지: 편입 후 스테이지가 흔들려도 원장에 유지 — 보유일은 리셋되지 않음
#   부분 익절(Oliver Kell): 승자는 러너로 보유하되 강세에 일부 실현
#                    ⓐ 3~5일 내 3R↑ 급등 → 잔여의 절반 익절 (1회)
#                    ⓑ 소진 확장(종가가 10 EMA +N% 이격) 진입 시 → 잔여의 절반 익절
#                    → 잔여 비중이 tp_min_frac 밑으로 떨어지면 전량 청산 마무리
#   전량 이탈(트랙 공통): ① 고점 대비 trail_stop_pct 하락 (기본 -10%, 진입 직후엔 손절 겸용)
#                    ② 웨지 드롭: 대량 거래 동반 10·20 EMA 종가 하향 이탈 (Oliver Kell)
#                    ③ 추세 이탈: 종가 < exit_ma_period 이동평균 (기본 60일선)
def _update_ledger(ledger: dict, results: list[dict], kospi: dict, params: dict) -> dict:
    today = dt.date.today()
    today_str = today.isoformat()
    trail_stop = float(params.get("trail_stop_pct", -10.0))  # 고점 대비 허용 하락폭 (%)
    exit_ma_key = f"ma{params.get('exit_ma_period', 60)}"   # 추세 이탈 기준 이동평균
    rs_min = params.get("rs_min", 70)
    # 편입 트랙 — track_id는 원장 저장/트랙 그룹 키(3=돌파, 1=모멘텀)로 유지하되,
    # 모멘텀 트랙은 감지기 스테이지가 아니라 웨지 팝(wedge_pop) 신호에 직접 물린다.
    entry_tracks = [
        {  # 돌파(신고가) — 감지기 Stage 3
            "id": 3, "label": "돌파(신고가)",
            "min_conf": params.get("stage3_entry_confidence", 70),
            "cand": lambda r, mc: (
                r.get("stage") == 3
                and r.get("confidence", 0) >= mc
                and r.get("days_in_stage", 0) <= entry_max_age   # 신규 신호만 (상태 아님)
            ),
            "conf": lambda r: r.get("confidence", 0),
            "signals": lambda r: r.get("signals", [])[:4],
        },
        {  # 모멘텀(웨지 팝) — 스테이지와 독립인 웨지 팝 신호 (Oliver Kell)
            "id": 1, "label": "모멘텀(웨지 팝)",
            "min_conf": params.get("stage1_entry_confidence", 75),
            "cand": lambda r, mc: (
                r.get("wedge_pop")                                # 당일 2~3x 거래량 폭증이 필수 → 이벤트성
                and r.get("rs_rank", 0) >= rs_min                 # 주도주만
                and r.get("wedge_confidence", 0) >= mc
            ),
            "conf": lambda r: r.get("wedge_confidence", 0),
            "signals": lambda r: r.get("wedge_signals", [])[:4],
        },
    ]
    # 편입 빈도 스로틀 — 스탁이지 실측(주당 4~6건, 동시 보유 15~17종목)에 맞춤
    entry_max_age = params.get("stage_entry_max_age_days", 2)      # 스테이지 진입 후 N일 이내만 편입 (상태→이벤트)
    reentry_cooldown = params.get("reentry_cooldown_days", 5)      # 이탈 후 N일간 같은 트랙 재편입 금지
    max_per_track = params.get("max_holdings_per_track", 10)       # 트랙당 동시 보유 상한
    max_daily = params.get("max_daily_entries_per_track", 3)       # 트랙당 하루 신규 편입 상한 (conf 상위 우선)
    # 부분 익절 (Oliver Kell) — 승자는 러너로 끌고 가되 강세에 일부 실현
    tp_r_mult = float(params.get("tp_r_multiple", 3.0))            # 3R 급등 시 부분익절 (리스크 대비 배수)
    tp_fast_days = int(params.get("tp_fast_days", 5))             # N일 이내 급등에만 3R 부분익절 적용
    tp_frac = float(params.get("tp_partial_frac", 0.5))           # 부분익절 1회당 잔여 대비 매도 비중
    tp_ext_pct = float(params.get("exhaustion_ext_pct", 20))     # 소진 확장: 종가가 10 EMA 대비 +N% 이격
    tp_min_frac = float(params.get("tp_min_frac", 0.1))          # 잔여 비중 하한 → 전량 청산

    scan_map = {r["ticker"]: r for r in results if r.get("ticker")}
    holdings = ledger.get("holdings", [])
    exited = list(ledger.get("exited", []))
    # 기존(v2) 원장은 entry_stage가 없음 — 전부 Stage 3 편입이었으므로 3으로 보정
    for h in holdings:
        h.setdefault("entry_stage", 3)
    held_keys = {(h["ticker"], h["entry_stage"]) for h in holdings}

    new_holdings = []
    for h in holdings:
        # 부분 익절 상태 (기존 원장 호환 — 없으면 전량 보유로 간주)
        h.setdefault("qty_frac", 1.0)          # 잔여 보유 비중 (1.0 = 전량)
        h.setdefault("partials", [])           # 부분익절 이력
        h.setdefault("realized_pct", 0.0)      # 원포지션(=1.0) 기준 실현 수익 기여도(%)
        h.setdefault("tp_3r_done", False)      # 3R 급등 부분익절 1회 소진 여부
        h.setdefault("was_extended", False)    # 직전일 소진 확장 상태 (엣지 감지용)

        r = scan_map.get(h["ticker"])
        price = float(r["current_price"]) if r and r.get("current_price") else h.get("last_price", h["entry_price"])
        entry_price = h["entry_price"]
        ret_pct = (price / entry_price - 1) * 100 if entry_price else 0.0   # 잔여분 미실현 수익률
        peak_price = max(h.get("peak_price", entry_price), price)
        peak_ret = (peak_price / entry_price - 1) * 100 if entry_price else 0.0
        days_held = (today - dt.date.fromisoformat(h["entry_date"])).days
        drop_from_peak = (price / peak_price - 1) * 100 if peak_price else 0.0

        qty_frac = float(h["qty_frac"])
        realized = float(h["realized_pct"])
        partials = list(h["partials"])
        tp_3r_done = bool(h["tp_3r_done"])
        was_extended = bool(h["was_extended"])

        # ── 전량 청산 조건 (트랙 공통): 트레일링 / 웨지 드롭 / MA60 ──
        exit_reason = None
        if drop_from_peak <= trail_stop + 1e-9:
            if peak_ret > abs(trail_stop):
                exit_reason = f"트레일링 스탑 (고점 대비 {trail_stop}%)"
            else:
                exit_reason = f"손절 ({round(ret_pct, 2)}%)"
        elif r and r.get("wedge_drop"):
            # 웨지 드롭: 대량 거래 동반 10·20 EMA 종가 하향 이탈 → 추세 종료 (Oliver Kell)
            exit_reason = "웨지 드롭 (대량 거래 + 10·20 EMA 이탈)"
        elif r and r.get(exit_ma_key) and price < float(r[exit_ma_key]):
            exit_reason = f"추세 이탈 ({exit_ma_key.upper()} 하회)"

        # ── 부분 익절 (Oliver Kell) — 전량 청산이 아닐 때만, 강세에 일부 실현 ──
        tp_note = None
        if not exit_reason and qty_frac > tp_min_frac:
            one_r = abs(float(h.get("init_stop_pct", trail_stop))) or abs(trail_stop)  # 1R (%)
            r_mult = ret_pct / one_r if one_r else 0.0
            ext_now = bool(r and r.get("ema10") and price >= float(r["ema10"]) * (1 + tp_ext_pct / 100))
            sell_frac, reason = 0.0, None
            if not tp_3r_done and days_held <= tp_fast_days and r_mult >= tp_r_mult:
                # 3~5일 내 3R↑ 급등 → 잔여의 절반 익절 (쿠션 확보), 1회만
                sell_frac, reason, tp_3r_done = tp_frac, f"{tp_r_mult:.0f}R 급등 부분익절", True
            elif ext_now and not was_extended:
                # 소진 확장(과열) 진입 엣지 → 강세에 잔여의 절반 익절
                sell_frac, reason = tp_frac, f"소진 확장 익절 (10 EMA +{tp_ext_pct:.0f}% 이격)"
            was_extended = ext_now
            if sell_frac > 0:
                sold = qty_frac * sell_frac
                realized += sold * ret_pct       # 원포지션(=1.0) 기준 실현 기여도
                qty_frac -= sold
                partials.append({"date": today_str, "price": round(price, 0),
                                 "frac": round(sold, 3), "ret_pct": round(ret_pct, 2), "reason": reason})
                tp_note = reason
                logger.info("부분익절: %s %s %d%% @ %.0f (%.1f%%) — %s",
                            h["ticker"], h.get("name"), round(sold * 100), price, ret_pct, reason)
                if qty_frac <= tp_min_frac:       # 잔여 미미 → 전량 청산 마무리
                    exit_reason = "익절 완료 (분할 매도 소진)"

        total_return = round(realized + qty_frac * ret_pct, 2)   # 원포지션 기준 총수익률(실현+미실현)

        h2 = dict(h)
        h2.update({
            "last_price": round(price, 0),
            "return_pct": round(ret_pct, 2),
            "total_return_pct": total_return,
            "qty_frac": round(qty_frac, 3),
            "realized_pct": round(realized, 2),
            "partials": partials,
            "tp_3r_done": tp_3r_done,
            "was_extended": was_extended,
            "peak_price": round(peak_price, 0),
            "days_held": days_held,
            "stage_now": r.get("stage") if r else None,
            "confidence_now": r.get("confidence", 0) if r else 0,
            "last_updated": today_str,
        })
        if tp_note and not exit_reason:
            h2["last_action"] = f"{today_str} · {tp_note} (잔여 {round(qty_frac*100)}%)"
        if exit_reason:
            h2.update({"exit_date": today_str, "exit_price": round(price, 0),
                       "exit_reason": exit_reason, "return_pct": total_return})
            exited.insert(0, h2)
            logger.info("이탈: %s %s (총 %.1f%%) — %s", h["ticker"], h.get("name"), total_return, exit_reason)
        else:
            new_holdings.append(h2)

    # 신규 편입 — 시장 청산 신호는 전량 매도 대신 신규 편입 차단으로만 사용
    if kospi.get("entry_allowed", True) and not kospi.get("exit_signal"):
        # 재편입 쿨다운: 트랙별 가장 최근 이탈일
        last_exit = {}
        for e in exited:
            key = (e["ticker"], e.get("entry_stage", 3))
            if e.get("exit_date", "") > last_exit.get(key, ""):
                last_exit[key] = e["exit_date"]
        cooldown_cut = (today - dt.timedelta(days=reentry_cooldown)).isoformat()

        for track in entry_tracks:
            tid, min_conf = track["id"], track["min_conf"]
            track_count = sum(1 for h in new_holdings if h.get("entry_stage", 3) == tid)
            slots = min(max_per_track - track_count, max_daily)
            if slots <= 0:
                continue
            cands = [
                r for r in results
                if r.get("ticker")
                and not r.get("climax_warning")
                and track["cand"](r, min_conf)
                and (r["ticker"], tid) not in held_keys
                and last_exit.get((r["ticker"], tid), "") < cooldown_cut
                and float(r.get("current_price") or 0) > 0
            ]
            cands.sort(key=lambda r: -track["conf"](r))
            for r in cands[:slots]:
                ticker = r["ticker"]
                price = float(r["current_price"])
                conf_v = track["conf"](r)
                # 초기 손절(1R) — 모멘텀(웨지)은 돌파 당일 저가 기준, 돌파는 트레일링과 동일
                init_stop = r.get("wedge_stop_pct") if (tid == 1 and r.get("wedge_stop_pct")) else trail_stop
                new_holdings.append({
                    "ticker": ticker,
                    "name": r.get("name", ticker),
                    "sector": r.get("sector", ""),
                    "entry_stage": tid,
                    "entry_date": today_str,
                    "entry_price": round(price, 0),
                    "entry_confidence": conf_v,
                    "init_stop_pct": init_stop,
                    "last_price": round(price, 0),
                    "peak_price": round(price, 0),
                    "return_pct": 0.0,
                    "total_return_pct": 0.0,
                    "qty_frac": 1.0,
                    "realized_pct": 0.0,
                    "partials": [],
                    "tp_3r_done": False,
                    "was_extended": False,
                    "days_held": 0,
                    "stage_now": r.get("stage"),
                    "confidence_now": conf_v,
                    "last_updated": today_str,
                    "signals_at_entry": track["signals"](r),
                })
                held_keys.add((ticker, tid))
                logger.info("편입[%s]: %s %s @ %.0f (conf=%d)",
                            track["label"], ticker, r.get("name"), price, conf_v)

    # 이탈 종목은 180일 / 최근 200건만 유지
    cutoff = (today - dt.timedelta(days=180)).isoformat()
    exited = [e for e in exited if e.get("exit_date", "") >= cutoff][:200]

    new_holdings.sort(key=lambda h: (-h.get("entry_stage", 3), h["entry_date"]))
    return {"holdings": new_holdings, "exited": exited}


# ── 트랙별 누적 수익률 히스토리 (전략 포트폴리오 차트) ─────────
# 트레이드당 동일 비중(1단위) 가정, 수익률(%p) 단순 합산.
#   - 최초 실행: 현존 이탈 기록으로 과거 실현 누적 곡선을 백필 (당시 보유 평가손익은 소급 불가)
#   - 이후 매 장마감: (전일까지 실현 누적 + 오늘 실현 + 보유 평가손익) 스냅샷을 1점 추가
#   실현 누적 베이스(cum_base)는 메타에 확정 저장 — 이탈 기록이 180일 보존정책으로
#   잘려나가도 과거 누적치가 함께 꺼지지 않는다. 같은 날 재실행은 오늘 점만 갱신(멱등).
def _is_trading_day_today() -> bool:
    """오늘이 한국 증시 거래일인지 — KOSPI 최신 캔들 날짜로 판별 (공휴일 캘린더 불필요)"""
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader("KS11", dt.date.today() - dt.timedelta(days=10))
        if df is None or df.empty:
            return True  # 판별 실패 시 기존 동작 유지
        return df.index[-1].date() == dt.date.today()
    except Exception:
        return True


def _update_track_history(state: dict, ledger: dict):
    """트랙별 성과 곡선 — 트레이드당 평균 수익률 (실현 + 보유 평가) ÷ 트레이드 수.

    이전의 %p 단순 합산 방식은 동시 보유 종목 수에 비례해 과장돼(-94%p 같은 값)
    포트폴리오 수익률로 읽을 수 없었다. 평균 방식은 '이 전략의 트레이드는
    평균 몇 %를 벌고 있나'로 읽히고 트랙 간 비교도 공정하다.
    """
    today_str = dt.date.today().isoformat()
    hist = state.setdefault("track_history", {})
    meta = state.setdefault("track_history_meta", {})
    for tid in (3, 1):
        key = str(tid)
        ex = [e for e in ledger.get("exited", [])
              if e.get("entry_stage", 3) == tid and e.get("exit_date")]
        holds = [h for h in ledger.get("holdings", [])
                 if h.get("entry_stage", 3) == tid]
        m = meta.get(key) or {}
        if m.get("mode") != "avg":
            # 구(%p 합산) 시리즈 폐기 → 이탈 기록으로 평균 곡선 백필
            series, cum_sum, n = [], 0.0, 0
            for e in sorted(ex, key=lambda e: e["exit_date"]):
                cum_sum += float(e.get("return_pct") or 0)
                n += 1
                point = {"date": e["exit_date"], "cum": round(cum_sum / n, 2)}
                if series and series[-1]["date"] == point["date"]:
                    series[-1] = point
                else:
                    series.append(point)
            series = [p for p in series if p["date"] < today_str]
        else:
            series = hist.get(key) or []
        realized_sum = sum(float(e.get("return_pct") or 0) for e in ex)
        open_sum = sum(float(h.get("total_return_pct", h.get("return_pct", 0)) or 0)
                       for h in holds)
        n_total = len(ex) + len(holds)
        avg = (realized_sum + open_sum) / n_total if n_total else 0.0
        point = {"date": today_str, "cum": round(avg, 2)}
        if series and series[-1]["date"] == today_str:
            series[-1] = point
        else:
            series.append(point)
        hist[key] = series[-250:]
        meta[key] = {"mode": "avg", "last_date": today_str}


TRACK_NAV_BASE = "2026-07-01"  # 기준가 1000 기점


def _update_track_nav(state: dict, ledger: dict, ohlcv_map: dict):
    """트랙별 기준가(NAV) 곡선 — 7/1=1000, 일별 동일가중 리밸런스 포트폴리오.

    매 마감 실행마다 원장(보유+이탈)과 OHLCV로 전 구간을 재계산한다(증분 누적 드리프트 방지).
    트레이드의 일간 수익률: 진입일 = 종가/진입가, 보유중 = 종가/전일종가, 청산일 = 청산가/전일종가.
    OHLCV가 없는 이탈 종목은 청산일에 트레이드 수익률을 일괄 반영(근사).
    """
    # 거래일 캘린더 — 가장 긴 OHLCV의 날짜 인덱스 사용
    cal = None
    for df in ohlcv_map.values():
        if df is not None and len(df) and (cal is None or len(df) > len(cal)):
            cal = df
    if cal is None:
        return
    dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10] for d in cal.index]
    dates = [d for d in dates if d >= TRACK_NAV_BASE]
    if not dates:
        return
    # 종목별 종가 맵 {ticker: {date: close}}
    closes: dict = {}

    def close_map(ticker):
        if ticker in closes:
            return closes[ticker]
        df = ohlcv_map.get(ticker)
        m = {}
        if df is not None and len(df):
            for idx, c in zip(df.index, df["close"]):
                ds = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                m[ds] = float(c)
        closes[ticker] = m
        return m

    nav_out = {}
    for tid in (3, 1):
        trades = []
        for e in ledger.get("exited", []):
            if e.get("entry_stage", 3) == tid and e.get("entry_date") and e.get("exit_date"):
                trades.append({"ticker": e.get("ticker"), "entry": e["entry_date"], "exit": e["exit_date"],
                               "entry_price": float(e.get("entry_price") or 0), "exit_price": float(e.get("exit_price") or 0),
                               "ret": float(e.get("return_pct") or 0)})
        for h in ledger.get("holdings", []):
            if h.get("entry_stage", 3) == tid and h.get("entry_date"):
                trades.append({"ticker": h.get("ticker"), "entry": h["entry_date"], "exit": None,
                               "entry_price": float(h.get("entry_price") or 0), "exit_price": None, "ret": None})
        nav = 1000.0
        series = [{"date": TRACK_NAV_BASE, "nav": 1000.0}]
        prev_date = None
        for d in dates:
            rets = []
            for t in trades:
                if d < t["entry"] or (t["exit"] and d > t["exit"]):
                    continue
                cm = close_map(t["ticker"])
                if not cm:
                    # OHLCV 없음 — 청산일에 총수익률 일괄 반영
                    if t["exit"] == d and t["ret"] is not None:
                        rets.append(t["ret"] / 100)
                    continue
                if d == t["entry"]:
                    if t["entry_price"] > 0 and d in cm:
                        rets.append(cm[d] / t["entry_price"] - 1)
                elif t["exit"] == d:
                    pc = cm.get(prev_date)
                    if pc and t["exit_price"]:
                        rets.append(t["exit_price"] / pc - 1)
                else:
                    pc, cc = cm.get(prev_date), cm.get(d)
                    if pc and cc:
                        rets.append(cc / pc - 1)
            if rets:
                nav *= 1 + sum(rets) / len(rets)
            if not series or series[-1]["date"] != d:
                series.append({"date": d, "nav": round(nav, 2)})
            else:
                series[-1] = {"date": d, "nav": round(nav, 2)}
            prev_date = d
        nav_out[str(tid)] = series[-260:]
    state["track_nav"] = nav_out


# ── 가치투자 시세/저평가 워치 (장마감 스냅샷) ──────────────────
def _fnum(x, allow_zero: bool = False):
    """pykrx 셀 값 → float. NaN·결측·(기본) 0 이하이면 None (비율 지표 결측 처리)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    if not allow_zero and v <= 0:
        return None
    return v


def _calc_fair_value(eps, bps, roe_pct, ke: float = 9.0, roe_cap: float = 25.0):
    """자체 적정가 — RIM(잔여이익 영구환원)과 Graham Number 중 보수적(낮은) 값.
    RIM은 경기순환 이익 과대평가를 막기 위해 ROE를 roe_cap%로 상한.
    반환: (fair, rim, graham) — 계산 불가 항목은 None."""
    rim = graham = None
    if bps and roe_pct and roe_pct > 0:
        rim = round(bps * (1 + (min(roe_pct, roe_cap) - ke) / ke))
        if rim <= 0:
            rim = None
    if eps and bps and eps > 0:
        graham = round((22.5 * eps * bps) ** 0.5)
    cands = [v for v in (rim, graham) if v]
    return (min(cands) if cands else None), rim, graham


async def _build_value_price(ohlcv_map: dict, realtime: dict) -> None:
    """value_universe.json 종목의 현재가·52주·상승여력·안전마진·저평가 워치를
    장마감 시점 시세로 계산해 value_price.json에 저장 (프론트가 value.json과 병합)."""
    vpath = ROOT / "value_universe.json"
    if not vpath.exists():
        return
    vdata = json.loads(vpath.read_text(encoding="utf-8"))
    meta: dict = {}
    for r in vdata.get("universe", []) + vdata.get("portfolio", []):
        code = r.get("code")
        if not code:
            continue
        m = meta.setdefault(code, {"target": None, "fair": None})
        if r.get("target_price") is not None:
            m["target"] = r["target_price"]
        if r.get("fair_value") is not None:
            m["fair"] = r["fair_value"]
    # 밸류 지표(PER·PBR·EPS·BPS·배당)·시총 (pykrx, 최근 영업일) — 실패해도 나머지는 진행
    fund_map, cap_map = {}, {}
    try:
        from pykrx import stock as _pykrx
        for back in range(0, 6):
            d = (dt.datetime.now(tz=KST).date() - dt.timedelta(days=back)).strftime("%Y%m%d")
            fdf = _pykrx.get_market_fundamental_by_ticker(d)
            if fdf is not None and len(fdf):
                for t in fdf.index:
                    fund_map[str(t).zfill(6)] = {
                        "per": _fnum(fdf.loc[t, "PER"]),
                        "pbr": _fnum(fdf.loc[t, "PBR"]),
                        "eps": _fnum(fdf.loc[t, "EPS"]),
                        "bps": _fnum(fdf.loc[t, "BPS"]),
                        "div": _fnum(fdf.loc[t, "DIV"], allow_zero=True),
                    }
                try:
                    cdf = _pykrx.get_market_cap_by_ticker(d)
                    cap_map = {str(t).zfill(6): cdf.loc[t, "시가총액"] for t in cdf.index}
                except Exception:
                    pass
                break
    except Exception as e:
        logger.warning("pykrx 밸류 지표 실패(무시): %s", e)

    # pykrx 결측 종목은 KIS 개별 조회로 보완 (KRX 클라우드 IP 차단 대비 — 가치 종목은 10개 미만이라 저렴)
    missing = [c for c in meta if not fund_map.get(c)]
    if missing:
        try:
            from data_provider import fetch_fundamental_kis, load_config as _load_cfg
            _cfg = _load_cfg()
            for c in missing:
                fb = await fetch_fundamental_kis(_cfg, c)
                if fb:
                    fund_map[c] = {k: fb.get(k) for k in ("per", "pbr", "eps", "bps", "div")}
                    if fb.get("cap") and c not in cap_map:
                        cap_map[c] = fb["cap"]
        except Exception as e:
            logger.warning("KIS 밸류 폴백 실패(무시): %s", e)

    # DART 확정 ROE (value.json metrics) — RIM 적정가 입력. 없으면 EPS/BPS로 근사.
    roe_map: dict = {}
    try:
        vj = json.loads((DATA_DIR / "value.json").read_text(encoding="utf-8"))
        for r in vj.get("universe", []) + vj.get("portfolio", []):
            roe = (r.get("metrics") or {}).get("roe")
            if r.get("code") and isinstance(roe, (int, float)):
                roe_map[r["code"]] = float(roe)
    except Exception:
        pass

    items: dict = {}
    for code, m in meta.items():
        df = ohlcv_map.get(code)
        if df is None:
            try:
                df = await fetch_ohlcv(code, days=400)
            except Exception:
                df = None
        if df is None or df.empty:
            continue
        price = realtime.get(code) or float(df.iloc[-1]["close"])
        change_pct = round((float(df.iloc[-1]["close"]) / float(df.iloc[-2]["close"]) - 1) * 100, 2) if len(df) >= 2 and float(df.iloc[-2]["close"]) else None
        f = fund_map.get(code, {})
        eps, bps, div_v = f.get("eps"), f.get("bps"), f.get("div")
        # PER·PBR: pykrx 우선, 결측 시 가격/EPS·BPS 역산 (신규 상장 등 KRX 결측 보완)
        per_v = f.get("per") or (round(price / eps, 1) if eps else None)
        per_str = f"{round(per_v, 1)}x" if per_v else None
        pbr_v = f.get("pbr") or (round(price / bps, 2) if bps else None)
        cap_v = cap_map.get(code)
        mktcap_str = f"{round(cap_v / 1e8):,}억" if cap_v else None
        tail = df.tail(250)
        high52 = float(tail["high"].max())
        low52 = float(tail["low"].min())
        # 기술적 위치 (눌림목 매매 준비) — MA·이격도·고점대비·RSI
        closes = df["close"].astype(float)
        ma20 = round(float(closes.tail(20).mean())) if len(closes) >= 20 else None
        ma60 = round(float(closes.tail(60).mean())) if len(closes) >= 60 else None
        ma60_prev = float(closes.iloc[-70:-10].mean()) if len(closes) >= 70 else None
        gap20 = round((price / ma20 - 1) * 100, 1) if ma20 else None
        gap60 = round((price / ma60 - 1) * 100, 1) if ma60 else None
        off_high = round((price / high52 - 1) * 100, 1) if high52 else None
        rsi14 = None
        if len(closes) >= 15:
            delta = closes.diff().tail(14)
            up_avg = float(delta.clip(lower=0).mean())
            dn_avg = float((-delta.clip(upper=0)).mean())
            rsi14 = round(100 - 100 / (1 + up_avg / dn_avg), 1) if dn_avg else 100.0
        # 눌림목 판정: 상승추세(MA60 위 + MA60 상승) 속 MA20 부근/MA20~60 사이 조정
        trend = "flat"
        if ma60 and ma60_prev:
            if price > ma60 and ma60 > ma60_prev:
                trend = "up"
            elif price < ma60 and ma60 < ma60_prev:
                trend = "down"
        zone = None
        if trend == "up" and gap20 is not None:
            if -3 <= gap20 <= 2:
                zone = "MA20 눌림"
            elif gap20 < -3 and gap60 is not None and gap60 >= 0:
                zone = "MA20~60 조정대"
        pullback = {"trend": trend, "zone": zone, "ready": bool(zone)}
        target = m["target"]
        upside = round((target / price - 1) * 100, 1) if (target and price) else None
        # 안전마진 — 수동 적정가 우선, 없으면 자체 적정가(RIM·Graham 보수값)로 계산
        roe = roe_map.get(code) or (round(eps / bps * 100, 1) if (eps and bps) else None)
        fair_calc, fair_rim, fair_graham = _calc_fair_value(eps, bps, roe)
        fair = m["fair"] or fair_calc
        fair_src = "manual" if m["fair"] else ("calc" if fair else None)
        margin = round((fair - price) / fair * 100, 1) if (fair and price) else None
        near_low = bool(low52 and price <= low52 * 1.1)
        watch = []
        # 자체 적정가(RIM·Graham)가 현재가보다 낮으면(안전마진 음수) 내재가치 기준 고평가다.
        # 이때 목표가(수동, 낙관적일 수 있음)·52주저점 신호로 "저평가"를 붙이면 적정가와
        # 모순 → 배지를 붙이지 않는다. (수동 적정가면 margin이 그 기준이므로 fair_src로 구분)
        overvalued = fair_src == "calc" and margin is not None and margin < 0
        if not overvalued:
            if upside is not None and upside >= 20:
                watch.append("목표가 대비 저평가")
            if margin is not None and margin >= 20:
                watch.append("안전마진 충분")
            if near_low:
                watch.append("52주 저점 근접")
        items[code] = {
            "price": round(price), "change_pct": change_pct, "per": per_str, "mktcap": mktcap_str,
            "pbr": pbr_v, "div": div_v, "eps": eps, "bps": bps, "roe": roe,
            "fair": fair, "fair_src": fair_src, "fair_rim": fair_rim, "fair_graham": fair_graham,
            "high52": round(high52), "low52": round(low52),
            "upside": upside, "margin": margin, "near_low": near_low, "watch": watch,
            "ma20": ma20, "ma60": ma60, "gap20": gap20, "gap60": gap60,
            "off_high": off_high, "rsi14": rsi14, "pullback": pullback,
        }
    _save_json(DATA_DIR / "value_price.json",
               {"updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"), "items": items})
    logger.info("value_price.json 갱신 (%d 종목)", len(items))


# ── 메인 ─────────────────────────────────────────────────
async def main():
    cfg = load_config()
    params = cfg.get("params", {})
    state = _load_state()

    # 1) 지수 구성종목 + 관심종목
    constituents = await fetch_index_constituents()
    ticker_map = dict(constituents)
    seen = set(ticker_map)
    scan_targets = list(constituents)
    for t in cfg.get("watchlist", []):
        if t not in seen:
            seen.add(t)
            scan_targets.append((t, t))
    if not scan_targets:
        logger.error("스캔 대상 없음 — 종료")
        sys.exit(1)
    logger.info("스캔 대상: %d 종목", len(scan_targets))

    # 2) 시세 일괄 조회 — 종목당 1회만 조회해서 RS와 스테이지 분석에 재사용
    #    (KIS 호출량을 절반으로 줄여 rate limit 차단 방지)
    sem = asyncio.Semaphore(8)

    async def _fetch_one(ticker: str):
        async with sem:
            try:
                df = await fetch_ohlcv(ticker, days=400)
                return ticker, (df if not df.empty else None)
            except Exception as e:
                logger.warning("시세 조회 실패 %s: %s", ticker, e)
                return ticker, None

    fetched = await asyncio.gather(*[_fetch_one(t) for t, _ in scan_targets])
    ohlcv_map = {t: df for t, df in fetched if df is not None}
    logger.info("시세 조회 완료: %d/%d 종목", len(ohlcv_map), len(scan_targets))

    # 3) RS 계산 — pykrx 일괄 등락률 우선, 실패 시 조회해둔 OHLCV 재사용
    loop = asyncio.get_event_loop()
    tmap, sector_map, period_returns = await loop.run_in_executor(None, _compute_rs_sync)
    ticker_map.update({k: v for k, v in tmap.items() if v})
    if not period_returns:
        period_returns = _rs_from_ohlcv_map(ohlcv_map)
    rs_map = _composite_rs(period_returns)
    logger.info("RS 계산 완료: %d 종목", len(rs_map))

    # RS 히스토리 (실행 간 유지 — 모멘텀 계산용, 스캔 대상만 저장)
    rs_history = state.get("rs_history", [])

    # 4) 장중 실시간 가격 (성공한 종목만)
    realtime: dict = {}
    if is_market_hours() and ohlcv_map:
        try:
            realtime = await fetch_realtime_prices_batch(cfg, list(ohlcv_map.keys()))
        except Exception as e:
            logger.warning("실시간 조회 실패 (스캔은 계속): %s", e)

    # 5) 스테이지 분석 — 조회해둔 OHLCV 사용 (추가 API 호출 없음)
    results = []
    for ticker, name in scan_targets:
        df = ohlcv_map.get(ticker)
        if df is None:
            continue
        try:
            rt = realtime.get(ticker)
            if rt:
                df = _append_virtual_candle(df, rt)
            rs = rs_map.get(ticker, 50.0) if rs_map else 80.0
            momentum, new_high = _rs_momentum(rs_history, ticker, rs)
            result = analyze_stock(ticker, ticker_map.get(ticker, name), df, rs, params,
                                   rs_momentum=momentum, rs_new_high=new_high)
            result.sector = sector_map.get(ticker, "")
            out = _sanitize(result.to_dict())
            if len(df) >= 2:
                prev = df.iloc[-2]["close"]
                out["change_pct"] = round((float(df.iloc[-1]["close"]) / float(prev) - 1) * 100, 2) if prev else 0.0
            else:
                out["change_pct"] = 0.0
            # 낙폭 스크리너용 파생 (추가 API 호출 없음 — 조회해둔 df 재사용)
            try:
                out.update(derive_drawdown_metrics(
                    df["high"].tolist() if "high" in df else [],
                    df["low"].tolist() if "low" in df else [],
                    df["close"].tolist(),
                ))
            except Exception as e:
                logger.warning("낙폭 파생 실패 %s: %s", ticker, e)
            results.append(out)
        except Exception as e:
            logger.warning("분석 실패 %s: %s", ticker, e)
    logger.info("스캔 완료: %d/%d 종목", len(results), len(scan_targets))
    if len(results) < len(scan_targets) * 0.3:
        logger.error("성공률 30%% 미만 — 데이터 소스 장애로 판단, 결과를 저장하지 않고 종료")
        sys.exit(1)
    # 열화 스캔 가드: 지수 유니버스 수집 실패(관심종목만 남음) 또는 종목명이 대부분 코드로 미해결
    # → FDR/KRX 일시 장애로 판단, 기존 scan.json(정상본)을 덮어쓰지 않고 종료(워크플로 실패로 신호).
    watch_n = len(cfg.get("watchlist", []))
    names_ok = sum(1 for r in results if r.get("name") and r.get("name") != r.get("ticker"))
    if len(scan_targets) <= watch_n + 5 or (results and names_ok < len(results) * 0.5):
        logger.error("열화 스캔(유니버스 %d≈관심 %d, 종목명해결 %d/%d) — scan.json 미갱신, 기존 데이터 보존",
                     len(scan_targets), watch_n, names_ok, len(results))
        sys.exit(1)

    # 시가총액 (섹터 맵 트리맵용) — KRX 일괄 1회, 실패해도 스캔은 계속
    try:
        mc_df = await fetch_market_cap()
        if mc_df is not None and not mc_df.empty and "시가총액" in mc_df.columns:
            mc = mc_df["시가총액"].to_dict()
            attached = 0
            for r in results:
                v = mc.get(r["ticker"])
                if v:
                    r["mktcap_100m"] = round(float(v) / 1e8)  # 억원 단위
                    attached += 1
            logger.info("시가총액 부착: %d/%d 종목", attached, len(results))
    except Exception as e:
        logger.warning("시가총액 조회 실패 (섹터 맵은 표시 생략): %s", e)

    # 섹터 대표 ETF RS (섹터 맵 사분면용 — 17개 ETF, KIS 우선이라 KRX 차단 무관)
    sector_etf_rs = {}
    try:
        sector_etf_rs = await _compute_sector_etf_rs()
        logger.info("섹터 ETF RS: %d/%d 섹터", len(sector_etf_rs), len(SECTOR_ETFS))
    except Exception as e:
        logger.warning("섹터 ETF RS 실패 (섹터 맵 사분면 생략): %s", e)

    # 5) KOSPI 상태 + 히스토리 + 원장
    kospi = await fetch_kospi_status()
    _update_stage_history(state.setdefault("stage_history", {}), results)

    # 전략 포트폴리오(원장)는 장마감(15:30 KST) 이후 실행에서만 하루 1회 갱신한다.
    # 장중 스캔은 감지기(scan.json)만 새로고침하고 원장은 직전 마감 상태를 그대로 유지.
    # 공휴일 가드: 오늘이 거래일이 아니면(휴장) 원장을 건드리지 않는다 —
    # 휴장일 크론 실행 시 전일 종가로 편입/이탈이 잘못 기록되는 것 방지.
    # 스트래들 가드: 장중에 시작해 가상 캔들(실시간 중간가)을 붙인 실행이
    # 지연되어 15:30을 넘겨 끝나면, 비종가 데이터로 이탈·부분익절이 확정되는 것을 막는다.
    now = dt.datetime.now(tz=KST)
    is_close_run = (now.hour, now.minute) >= (15, 30) and _is_trading_day_today() and not realtime
    ledger = state.get("ledger", {"holdings": [], "exited": []})
    if is_close_run:
        ledger = _update_ledger(ledger, results, kospi, params)
        state["ledger"] = ledger
        _update_track_history(state, ledger)
        _update_track_nav(state, ledger, ohlcv_map)  # 기준가(7/1=1000) 곡선
        logger.info("장마감 이후 실행 — 전략 포트폴리오 갱신")
    else:
        logger.info("장중 실행 — 전략 포트폴리오는 직전 마감 상태 유지 (감지기만 갱신)")

    # RS 히스토리 스냅샷 (스캔 대상만, 최대 8개)
    snap_rs = {t: round(rs_map.get(t, 0)) for t, _ in scan_targets if t in rs_map}
    rs_history.append({"time": dt.datetime.now(tz=KST).isoformat(timespec="seconds"), "rs": snap_rs})
    state["rs_history"] = rs_history[-8:]

    # 6) 출력
    now_str = dt.datetime.now(tz=KST).isoformat(timespec="seconds")
    results.sort(key=lambda x: (-(x.get("stage") or 0), -x.get("confidence", 0)))

    # 상세 모달이 MA/MTT 필드까지 쓰므로 전체 필드를 그대로 내보낸다
    _save_json(SCAN_PATH, {
        "scan_time": now_str,
        "universe_size": len(scan_targets),
        "scanned": len(results),
        "kospi": kospi,
        "sector_etf_rs": sector_etf_rs,
        "results": results,
    })
    # 전략 포트폴리오는 장마감 실행에서만 tracking.json을 새로 쓴다.
    # 장중 실행은 기존 tracking.json(직전 마감본)을 건드리지 않아 하루 1회만 변경된다.
    if is_close_run:
        _save_json(TRACKING_PATH, {
            "updated": now_str,
            "kospi": kospi,
            "holdings": ledger["holdings"],
            "exited": ledger["exited"],
            "track_history": state.get("track_history", {}),
            "track_nav": state.get("track_nav", {}),
            "nav_base": TRACK_NAV_BASE,
            "stats": {
                "holding_count": len(ledger["holdings"]),
                "exited_count": len(ledger["exited"]),
                "avg_return": round(sum(h.get("total_return_pct", h["return_pct"]) for h in ledger["holdings"]) / len(ledger["holdings"]), 2) if ledger["holdings"] else 0,
                "win_rate": round(sum(1 for e in ledger["exited"] if e.get("return_pct", 0) > 0) / len(ledger["exited"]) * 100, 1) if ledger["exited"] else 0,
            },
        })
    _save_json(STATE_PATH, state)

    # 가치투자 시세/저평가 워치 — 장마감 실행에서만 (매일 1회, 전략 포트폴리오와 동일 캐이던스)
    if is_close_run:
        try:
            await _build_value_price(ohlcv_map, realtime)
        except Exception as e:
            logger.warning("가치투자 시세 갱신 실패(무시): %s", e)

    logger.info("저장 완료: scan.json / tracking.json / state.json (보유 %d, 이탈 %d)",
                len(ledger["holdings"]), len(ledger["exited"]))

    # 모델 포트폴리오(구글 시트 미러) 갱신 — 실패해도 스캔 결과에는 영향 없음
    try:
        import fetch_sheet
        pf = fetch_sheet.build()
        _save_json(fetch_sheet.OUT_PATH, pf)
        logger.info("portfolio.json 갱신 (보유 %d, 유니버스 %d)",
                    len(pf.get("holdings", [])), len(pf.get("universe", [])))
    except Exception as e:
        logger.warning("모델 포트폴리오 미러 실패(무시): %s", e)

    # 가치투자 재무(value.json)는 여기서 갱신하지 않는다.
    # 재무제표는 분기보고서 기준이라 별도 워크플로(update-value.yml, 분기·수동)에서 fetch_value로 갱신.


if __name__ == "__main__":
    asyncio.run(main())
