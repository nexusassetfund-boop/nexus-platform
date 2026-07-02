"""
깡토 추세추종 스테이지 감지 — GitHub Actions용 1회 실행 스캐너

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


async def _compute_rs_via_ohlcv(tickers: list[str]) -> dict:
    """pykrx 실패 시 KIS OHLCV로 다중기간 수익률 계산 (main.py에서 이식)"""
    sem = asyncio.Semaphore(10)

    async def _fetch_one(ticker: str):
        async with sem:
            try:
                df = await fetch_ohlcv(ticker, days=300)
                if df.empty or len(df) < 63:
                    return None
                closes = df["close"]
                cur = float(closes.iloc[-1])
                rets = {}
                for label, days, _w in RS_PERIODS:
                    if len(closes) >= days:
                        old = float(closes.iloc[-days])
                        if old > 0:
                            rets[label] = (cur / old - 1) * 100
                return (ticker, rets) if rets else None
            except Exception:
                return None

    done = await asyncio.gather(*[_fetch_one(t) for t in tickers])
    period_returns: dict = {}
    for item in done:
        if item is None:
            continue
        ticker, rets = item
        for label, ret_val in rets.items():
            period_returns.setdefault(label, {})[ticker] = ret_val
    logger.info("KIS OHLCV RS fallback: %d 종목", len([x for x in done if x]))
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


# ── 편입/이탈 원장 (tracking v2) ─────────────────────────
# 규칙 (config.json params 사용):
#   편입: Stage 3 돌파 + 신뢰도 70↑ + KOSPI 진입 허용 + 클라이맥스 경고 없음
#   유지: 편입 후 스테이지가 흔들려도 원장에 유지 — 보유일은 리셋되지 않음
#   이탈: ① 손절 stop_loss_pct  ② 목표 도달 profit_target_pct
#         ③ 트레일링: 최고 수익 trailing_stop_trigger_pct 도달 후 고점 대비 -8% 마감
#         ④ 추세 이탈: 종가 < MA60  ⑤ KOSPI 청산 신호 (전체 이탈)
def _update_ledger(ledger: dict, results: list[dict], kospi: dict, params: dict) -> dict:
    today = dt.date.today()
    today_str = today.isoformat()
    stop_loss = params.get("stop_loss_pct", -8)
    profit_target = params.get("profit_target_pct", 21)
    trail_trigger = params.get("trailing_stop_trigger_pct", 14)
    trail_drop = -8.0  # 트레일링 발동 후 고점 대비 허용 하락폭 (%)

    scan_map = {r["ticker"]: r for r in results if r.get("ticker")}
    holdings = ledger.get("holdings", [])
    exited = list(ledger.get("exited", []))
    held_tickers = {h["ticker"] for h in holdings}

    new_holdings = []
    for h in holdings:
        r = scan_map.get(h["ticker"])
        price = float(r["current_price"]) if r and r.get("current_price") else h.get("last_price", h["entry_price"])
        entry_price = h["entry_price"]
        ret_pct = (price / entry_price - 1) * 100 if entry_price else 0.0
        peak_price = max(h.get("peak_price", entry_price), price)
        peak_ret = (peak_price / entry_price - 1) * 100 if entry_price else 0.0
        days_held = (today - dt.date.fromisoformat(h["entry_date"])).days

        exit_reason = None
        if kospi.get("exit_signal"):
            exit_reason = "시장 청산 신호 (KOSPI)"
        elif ret_pct <= stop_loss:
            exit_reason = f"손절 ({stop_loss}%)"
        elif ret_pct >= profit_target:
            exit_reason = f"목표 달성 (+{profit_target}%)"
        elif peak_ret >= trail_trigger and (price / peak_price - 1) * 100 <= trail_drop:
            exit_reason = f"트레일링 스탑 (고점 대비 {trail_drop}%)"
        elif r and r.get("ma60") and price < float(r["ma60"]):
            exit_reason = "추세 이탈 (MA60 하회)"

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

    # 신규 편입
    if kospi.get("entry_allowed", True):
        for r in results:
            ticker = r.get("ticker")
            if not ticker or ticker in held_tickers:
                continue
            if r.get("stage") == 3 and r.get("confidence", 0) >= 70 and not r.get("climax_warning"):
                price = float(r.get("current_price") or 0)
                if price <= 0:
                    continue
                new_holdings.append({
                    "ticker": ticker,
                    "name": r.get("name", ticker),
                    "sector": r.get("sector", ""),
                    "entry_date": today_str,
                    "entry_price": round(price, 0),
                    "entry_confidence": r.get("confidence", 0),
                    "last_price": round(price, 0),
                    "peak_price": round(price, 0),
                    "return_pct": 0.0,
                    "days_held": 0,
                    "stage_now": 3,
                    "confidence_now": r.get("confidence", 0),
                    "last_updated": today_str,
                    "signals_at_entry": r.get("signals", [])[:4],
                })
                held_tickers.add(ticker)
                logger.info("편입: %s %s @ %.0f (conf=%d)", ticker, r.get("name"), price, r.get("confidence", 0))

    # 이탈 종목은 180일 / 최근 200건만 유지
    cutoff = (today - dt.timedelta(days=180)).isoformat()
    exited = [e for e in exited if e.get("exit_date", "") >= cutoff][:200]

    new_holdings.sort(key=lambda h: h["entry_date"])
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

    # 2) RS 계산
    loop = asyncio.get_event_loop()
    tmap, sector_map, period_returns = await loop.run_in_executor(None, _compute_rs_sync)
    ticker_map.update({k: v for k, v in tmap.items() if v})
    if not period_returns:
        period_returns = await _compute_rs_via_ohlcv([t for t, _ in scan_targets])
    rs_map = _composite_rs(period_returns)
    logger.info("RS 계산 완료: %d 종목", len(rs_map))

    # RS 히스토리 (실행 간 유지 — 모멘텀 계산용, 스캔 대상만 저장)
    rs_history = state.get("rs_history", [])

    # 3) 장중 실시간 가격
    realtime: dict = {}
    if is_market_hours():
        try:
            realtime = await fetch_realtime_prices_batch(cfg, [t for t, _ in scan_targets])
        except Exception as e:
            logger.warning("실시간 조회 실패 (스캔은 계속): %s", e)

    # 4) 스테이지 분석
    sem = asyncio.Semaphore(15)

    async def _analyze_one(ticker: str, name: str):
        async with sem:
            try:
                df = await fetch_ohlcv(ticker, days=400)
                if df.empty:
                    return None
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
                return out
            except Exception as e:
                logger.warning("스캔 실패 %s: %s", ticker, e)
                return None

    done = await asyncio.gather(*[_analyze_one(t, n) for t, n in scan_targets])
    results = [r for r in done if r is not None]
    logger.info("스캔 완료: %d/%d 종목", len(results), len(scan_targets))
    if len(results) < len(scan_targets) * 0.3:
        logger.error("성공률 30%% 미만 — 데이터 소스 장애로 판단, 결과를 저장하지 않고 종료")
        sys.exit(1)

    # 5) KOSPI 상태 + 히스토리 + 원장
    kospi = await fetch_kospi_status()
    _update_stage_history(state.setdefault("stage_history", {}), results)
    ledger = _update_ledger(state.get("ledger", {"holdings": [], "exited": []}),
                            results, kospi, params)
    state["ledger"] = ledger

    # RS 히스토리 스냅샷 (스캔 대상만, 최대 8개)
    snap_rs = {t: round(rs_map.get(t, 0)) for t, _ in scan_targets if t in rs_map}
    rs_history.append({"time": dt.datetime.now(tz=KST).isoformat(timespec="seconds"), "rs": snap_rs})
    state["rs_history"] = rs_history[-8:]

    # 6) 출력
    now_str = dt.datetime.now(tz=KST).isoformat(timespec="seconds")
    results.sort(key=lambda x: (-(x.get("stage") or 0), -x.get("confidence", 0)))
    # 프론트에 필요한 필드만 추려서 용량 절감
    slim = []
    for r in results:
        slim.append({k: r.get(k) for k in (
            "ticker", "name", "sector", "stage", "stage_label", "confidence",
            "rs_rank", "rs_momentum", "rs_new_high", "current_price", "change_pct",
            "days_in_stage", "signals", "vcp_detected", "contractions", "vol_drying",
            "near_high", "volume_surge_ratio", "gap_up", "climax_warning",
            "rise_from_low_pct", "range_contraction_pct", "ma_aligned", "mtt_pass",
            "suggested_stop_pct", "position_size_pct",
        )})

    _save_json(SCAN_PATH, {
        "scan_time": now_str,
        "universe_size": len(scan_targets),
        "scanned": len(results),
        "kospi": kospi,
        "results": slim,
    })
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


if __name__ == "__main__":
    asyncio.run(main())
