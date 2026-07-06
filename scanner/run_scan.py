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
    fetch_ohlcv,
    fetch_realtime_prices_batch,
    is_market_hours,
    load_config,
)
from stage_detector import analyze_stock

logger = logging.getLogger("run_scan")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "docs" / "data"
STATE_PATH = DATA_DIR / "state.json"      # stage_history + rs_history + ledger (실행 간 유지)
SCAN_PATH = DATA_DIR / "scan.json"        # 스캔 결과 (프론트가 읽음)
TRACKING_PATH = DATA_DIR / "tracking.json"  # 보유/이탈 원장 (프론트가 읽음)

RS_PERIODS = [("q1", 63, 0.4), ("q2", 126, 0.2), ("q3", 189, 0.2), ("q4", 252, 0.2)]


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

    today = dt.date.today()
    end_str = None
    end_date = None
    for delta in range(0, 7):
        check = today - dt.timedelta(days=delta)
        check_str = check.strftime("%Y%m%d")
        try:
            tlist = krx.get_market_ticker_list(check_str, market="KOSPI")
            if tlist and len(tlist) > 100:
                end_str = check_str
                end_date = check
                break
        except Exception:
            continue

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

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            for code in krx.get_market_ticker_list(end_str, market=market):
                if code not in ticker_map:
                    try:
                        ticker_map[code] = krx.get_market_ticker_name(code)
                    except Exception:
                        ticker_map[code] = code
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
#   편입: 스테이지별 독립 트랙 (스탁이지의 1호/2호 전략실 방식)
#     - Stage 3 트랙: 돌파 + 신뢰도 stage3_entry_confidence(70)↑ — 피크 Easy식
#     - Stage 1 트랙: 초기 추세 + 신뢰도 stage1_entry_confidence(60)↑ — 모멘텀 Easy식
#     공통: KOSPI 진입 허용 + 클라이맥스 경고 없음. 같은 종목이 두 트랙에 각각 편입 가능.
#   유지: 편입 후 스테이지가 흔들려도 원장에 유지 — 보유일은 리셋되지 않음
#   이탈(트랙 공통): ① 고점 대비 trail_stop_pct 하락 (기본 -8%, 진입 직후엔 손절 겸용)
#                    ② 추세 이탈: 종가 < exit_ma_period 이동평균 (기본 20일선)
def _update_ledger(ledger: dict, results: list[dict], kospi: dict, params: dict) -> dict:
    today = dt.date.today()
    today_str = today.isoformat()
    trail_stop = float(params.get("trail_stop_pct", -8.0))  # 고점 대비 허용 하락폭 (%)
    exit_ma_key = f"ma{params.get('exit_ma_period', 20)}"   # 추세 이탈 기준 이동평균
    entry_tracks = [  # (편입 스테이지, 최소 신뢰도)
        (3, params.get("stage3_entry_confidence", 70)),
        (1, params.get("stage1_entry_confidence", 60)),
    ]
    # 편입 빈도 스로틀 — 스탁이지 실측(주당 4~6건, 동시 보유 15~17종목)에 맞춤
    entry_max_age = params.get("stage_entry_max_age_days", 2)      # 스테이지 진입 후 N일 이내만 편입 (상태→이벤트)
    reentry_cooldown = params.get("reentry_cooldown_days", 5)      # 이탈 후 N일간 같은 트랙 재편입 금지
    max_per_track = params.get("max_holdings_per_track", 10)       # 트랙당 동시 보유 상한
    max_daily = params.get("max_daily_entries_per_track", 3)       # 트랙당 하루 신규 편입 상한 (conf 상위 우선)

    scan_map = {r["ticker"]: r for r in results if r.get("ticker")}
    holdings = ledger.get("holdings", [])
    exited = list(ledger.get("exited", []))
    # 기존(v2) 원장은 entry_stage가 없음 — 전부 Stage 3 편입이었으므로 3으로 보정
    for h in holdings:
        h.setdefault("entry_stage", 3)
    held_keys = {(h["ticker"], h["entry_stage"]) for h in holdings}

    new_holdings = []
    for h in holdings:
        r = scan_map.get(h["ticker"])
        price = float(r["current_price"]) if r and r.get("current_price") else h.get("last_price", h["entry_price"])
        entry_price = h["entry_price"]
        ret_pct = (price / entry_price - 1) * 100 if entry_price else 0.0
        peak_price = max(h.get("peak_price", entry_price), price)
        peak_ret = (peak_price / entry_price - 1) * 100 if entry_price else 0.0
        days_held = (today - dt.date.fromisoformat(h["entry_date"])).days

        drop_from_peak = (price / peak_price - 1) * 100 if peak_price else 0.0

        exit_reason = None
        if drop_from_peak <= trail_stop + 1e-9:
            if peak_ret > abs(trail_stop):
                exit_reason = f"트레일링 스탑 (고점 대비 {trail_stop}%)"
            else:
                exit_reason = f"손절 ({round(ret_pct, 2)}%)"
        elif r and r.get(exit_ma_key) and price < float(r[exit_ma_key]):
            exit_reason = f"추세 이탈 ({exit_ma_key.upper()} 하회)"

        h2 = dict(h)
        h2.update({
            "last_price": round(price, 0),
            "return_pct": round(ret_pct, 2),
            "peak_price": round(peak_price, 0),
            "days_held": days_held,
            "stage_now": r.get("stage") if r else None,
            "confidence_now": r.get("confidence", 0) if r else 0,
            "last_updated": today_str,
        })
        if exit_reason:
            h2.update({"exit_date": today_str, "exit_price": round(price, 0),
                       "exit_reason": exit_reason})
            exited.insert(0, h2)
            logger.info("이탈: %s %s (%.1f%%) — %s", h["ticker"], h.get("name"), ret_pct, exit_reason)
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

        for entry_stage, min_conf in entry_tracks:
            track_count = sum(1 for h in new_holdings if h.get("entry_stage", 3) == entry_stage)
            slots = min(max_per_track - track_count, max_daily)
            if slots <= 0:
                continue
            cands = [
                r for r in results
                if r.get("ticker")
                and not r.get("climax_warning")
                and r.get("stage") == entry_stage
                and r.get("confidence", 0) >= min_conf
                and r.get("days_in_stage", 0) <= entry_max_age      # 신규 신호만 (상태 아님)
                and (r["ticker"], entry_stage) not in held_keys
                and last_exit.get((r["ticker"], entry_stage), "") < cooldown_cut
                and float(r.get("current_price") or 0) > 0
            ]
            cands.sort(key=lambda r: -r.get("confidence", 0))
            for r in cands[:slots]:
                ticker = r["ticker"]
                price = float(r["current_price"])
                new_holdings.append({
                    "ticker": ticker,
                    "name": r.get("name", ticker),
                    "sector": r.get("sector", ""),
                    "entry_stage": entry_stage,
                    "entry_date": today_str,
                    "entry_price": round(price, 0),
                    "entry_confidence": r.get("confidence", 0),
                    "last_price": round(price, 0),
                    "peak_price": round(price, 0),
                    "return_pct": 0.0,
                    "days_held": 0,
                    "stage_now": r.get("stage"),
                    "confidence_now": r.get("confidence", 0),
                    "last_updated": today_str,
                    "signals_at_entry": r.get("signals", [])[:4],
                })
                held_keys.add((ticker, entry_stage))
                logger.info("편입[S%d]: %s %s @ %.0f (conf=%d, 신호 %d일차)",
                            entry_stage, ticker, r.get("name"), price,
                            r.get("confidence", 0), r.get("days_in_stage", 0))

    # 이탈 종목은 180일 / 최근 200건만 유지
    cutoff = (today - dt.timedelta(days=180)).isoformat()
    exited = [e for e in exited if e.get("exit_date", "") >= cutoff][:200]

    new_holdings.sort(key=lambda h: (-h.get("entry_stage", 3), h["entry_date"]))
    return {"holdings": new_holdings, "exited": exited}


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
            results.append(out)
        except Exception as e:
            logger.warning("분석 실패 %s: %s", ticker, e)
    logger.info("스캔 완료: %d/%d 종목", len(results), len(scan_targets))
    if len(results) < len(scan_targets) * 0.3:
        logger.error("성공률 30%% 미만 — 데이터 소스 장애로 판단, 결과를 저장하지 않고 종료")
        sys.exit(1)

    # 5) KOSPI 상태 + 히스토리 + 원장
    kospi = await fetch_kospi_status()
    _update_stage_history(state.setdefault("stage_history", {}), results)

    # 전략 포트폴리오(원장)는 장마감(15:30 KST) 이후 실행에서만 하루 1회 갱신한다.
    # 장중 스캔은 감지기(scan.json)만 새로고침하고 원장은 직전 마감 상태를 그대로 유지.
    now = dt.datetime.now(tz=KST)
    is_close_run = (now.hour, now.minute) >= (15, 30)
    ledger = state.get("ledger", {"holdings": [], "exited": []})
    if is_close_run:
        ledger = _update_ledger(ledger, results, kospi, params)
        state["ledger"] = ledger
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
            "stats": {
                "holding_count": len(ledger["holdings"]),
                "exited_count": len(ledger["exited"]),
                "avg_return": round(sum(h["return_pct"] for h in ledger["holdings"]) / len(ledger["holdings"]), 2) if ledger["holdings"] else 0,
                "win_rate": round(sum(1 for e in ledger["exited"] if e.get("return_pct", 0) > 0) / len(ledger["exited"]) * 100, 1) if ledger["exited"] else 0,
            },
        })
    _save_json(STATE_PATH, state)
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

    # 가치투자 재무 스캔 — DART 키가 있을 때만 (없으면 기존 value.json 유지)
    if os.environ.get("DART_API_KEY", "").strip():
        try:
            import fetch_value
            vv = fetch_value.build()
            _save_json(fetch_value.OUT_PATH, vv)
            logger.info("value.json 갱신 (유니버스 %d, 포트폴리오 %d)",
                        len(vv.get("universe", [])), len(vv.get("portfolio", [])))
        except Exception as e:
            logger.warning("가치투자 스캔 실패(무시): %s", e)


if __name__ == "__main__":
    asyncio.run(main())
