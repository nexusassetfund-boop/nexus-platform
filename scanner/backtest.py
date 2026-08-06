"""
스테이지 감지기 S1/S3 트랙 walk-forward 백테스트

검증 대상 (stage_detector.py + run_scan.py _update_ledger와 동일 규칙):
  편입: S3 = stage3 & conf>=70 / S1 = stage1 & conf>=75
        + 클라이맥스 없음 + days_in_stage<=2 + 이탈 후 5일 쿨다운
        + 트랙당 최대 10종목 + 하루 최대 3종목(conf 상위) + KOSPI 게이트
  이탈: 고점(종가) 대비 -8% / 종가 < MA20. 익절 없음. 시장신호는 편입차단만.

체결 가정 (punish the strategy):
  신호 다음날 시가 ±0.5% 슬리피지, 매도세 0.23% + 왕복 수수료 0.03%

한계 (보고서 명시):
  - 현재 시점 지수 구성종목 → 생존 편향(상방)
  - RS를 전체 시장이 아닌 유니버스(~350종목) 내 백분위로 계산
  - rs_momentum=0, rs_new_high=False 단순화 (신뢰도 보수적)
"""

import argparse
import datetime as dt
import json
import os
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

CACHE_DIR = Path(os.environ.get("CLAUDE_JOB_DIR", str(ROOT))) / "tmp"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

RS_PERIODS = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]
BUY_SLIP = 0.005
SELL_SLIP = 0.005
SELL_TAX = 0.0023
COMMISSION_RT = 0.0003


# ── 데이터 ───────────────────────────────────────────────
def fetch_universe() -> dict:
    """프로덕션 스캐너와 동일한 구성종목 소스 (pykrx 지수 → 실패 시 FDR fallback)"""
    from data_provider import _fetch_index_constituents_sync
    return {t: n for t, n in _fetch_index_constituents_sync()}


def load_ohlcv(tickers: dict, start: str, end: str) -> dict:
    cache = CACHE_DIR / f"bt_ohlcv_{start}_{end}_{len(tickers)}.pkl"
    if cache.exists():
        with open(cache, "rb") as f:
            return pickle.load(f)
    import FinanceDataReader as fdr
    data = {}
    for i, t in enumerate(tickers):
        try:
            df = fdr.DataReader(t, start, end)
            if df is not None and len(df) >= 260:
                df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
                data[t] = df.astype(float)
        except Exception:
            pass
        if (i + 1) % 50 == 0:
            print(f"  다운로드 {i+1}/{len(tickers)} (수집 {len(data)})", flush=True)
    with open(cache, "wb") as f:
        pickle.dump(data, f)
    return data


def load_kospi(start: str, end: str) -> pd.DataFrame:
    cache = CACHE_DIR / f"bt_kospi_{start}_{end}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    import FinanceDataReader as fdr
    df = fdr.DataReader("KS11", start, end).rename(columns=str.lower)
    df.to_pickle(cache)
    return df


# ── 지표 사전계산 (stage_detector.analyze_stock 벡터화) ──
def precompute(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, o, v = df["close"], df["high"], df["low"], df["open"], df["volume"]
    n = len(df)
    out = pd.DataFrame(index=df.index)
    out["close"], out["open"] = c, o

    for p in (5, 10, 20, 60, 120, 150, 200):
        out[f"ma{p}"] = c.rolling(p).mean()

    ma200 = out["ma200"]
    out["ma200_rising"] = (ma200 > ma200.shift(19)) & (np.arange(n) >= 219)

    out["ma_aligned"] = (out["ma5"] > out["ma20"]) & (out["ma20"] > out["ma60"]) & \
                        (out["ma60"] > out["ma120"]) & out["ma120"].notna()

    low60 = c.rolling(60).min()
    out["rise_from_low"] = (c / low60 - 1) * 100

    # VCP: 최근 60일을 20일 3구간으로
    seg_hi = h.rolling(20).max()
    seg_lo = l.rolling(20).min()
    rng = (seg_hi - seg_lo) / seg_lo * 100
    r0, r1, r2 = rng.shift(40), rng.shift(20), rng
    contr = (r1 < r0 * 0.85).astype(int) + (r2 < r1 * 0.85).astype(int)
    out["vcp_contractions"] = contr.where(np.arange(n) >= 59, 0)
    out["vcp_detected"] = (out["vcp_contractions"] >= 2)
    out["range_contraction_pct"] = ((1 - r2 / r0) * 100).round(1)
    vol20m = v.rolling(20).mean()
    out["vol_drying"] = vol20m < vol20m.shift(40) * 0.5

    # 돌파: highs[-60:-5] 최고가 = rolling(55).max() @ t-5
    lb_high = h.rolling(55).max().shift(5)
    out["near_high"] = c >= lb_high * 0.97
    broke = c > lb_high
    avg_vol20 = v.rolling(20).mean().shift(5)
    surge = v / avg_vol20
    out["volume_surge_ratio"] = surge
    out["gap_up"] = o > h.shift(1)
    out["breakout"] = broke & (surge >= 2.0) & (np.arange(n) >= 59)

    # 클라이맥스
    vol_ratio = v / v.rolling(20).mean()
    bearish = c < o
    wick = h - np.maximum(o, c)
    body = (c - o).abs()
    long_wick = (wick > body * 1.5) & (body > 0)
    drop = c.pct_change() * 100
    out["climax"] = ((vol_ratio >= 3.0) & bearish & long_wick) | (drop <= -10)

    # 보너스 요소
    slope = (ma200 / ma200.shift(19) - 1) * 100
    out["ma200_slope_bonus"] = np.where(np.arange(n) >= 219,
                                        np.where(slope >= 2.0, 10, np.where(slope >= 0.5, 5, 0)), 0)
    above5 = (c > out["ma5"]).rolling(10).sum()
    out["ma_quality_bonus"] = np.where((above5 >= 8) & (np.arange(n) >= 14), 5, 0)
    out["enough"] = np.arange(n) >= 59
    return out


def stage_and_conf(row, rs: float, rs_min=70) -> tuple:
    """사전계산 행 + RS → (stage, confidence, climax) — analyze_stock 판정부 복제"""
    if not row["enough"] or np.isnan(row["close"]):
        return None, 0, False
    c = row["close"]
    ma150, ma200 = row["ma150"], row["ma200"]
    mtt = (not np.isnan(ma150)) and (not np.isnan(ma200)) and c > ma150 > 0 and c > ma200 > 0 \
        and ma150 > ma200 and bool(row["ma200_rising"]) and rs >= rs_min
    if not mtt:
        return None, 0, bool(row["climax"])

    rs_bonus = 15 if rs >= 90 else (10 if rs >= 80 else 0)
    slope_b = int(row["ma200_slope_bonus"])
    mq = int(row["ma_quality_bonus"]) if row["ma_aligned"] else 0
    aligned = bool(row["ma_aligned"])
    rise = row["rise_from_low"]

    if row["breakout"]:
        conf = 70
        if row["gap_up"]:
            conf += 10
        if row["vcp_detected"]:
            conf += 10
        if aligned:
            conf += 5
        if row["volume_surge_ratio"] >= 3.0:
            conf += 5
        conf += rs_bonus + slope_b
        stage = 3
    elif row["vcp_detected"] and aligned:
        conf = 50
        if row["vol_drying"]:
            conf += 10
        if row["near_high"]:
            conf += 15
        if row["vcp_contractions"] >= 3:
            conf += 5
        if row["range_contraction_pct"] >= 50:
            conf += 5
        conf += rs_bonus + slope_b + mq
        stage = 2
    elif aligned and rise >= 50:
        conf = 45
        conf += 15 if rise >= 100 else (10 if rise >= 80 else (5 if rise >= 60 else 0))
        conf += rs_bonus + slope_b + mq
        stage = 1
    elif aligned:
        conf = 30
        conf += 10 if rise >= 30 else (5 if rise >= 15 else 0)
        conf += rs_bonus + slope_b
        stage = 1
    else:
        return None, 15, bool(row["climax"])

    if row["climax"]:
        conf = max(conf - 20, 0)
    return stage, min(conf, 100), bool(row["climax"])


def build_rs_matrix(closes: pd.DataFrame) -> pd.DataFrame:
    """일자×종목 RS 백분위 (run_scan._composite_rs 방식, 유니버스 내 순위)"""
    comp_num = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    comp_den = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for days, w in RS_PERIODS:
        ret = closes / closes.shift(days) - 1
        pct = ret.rank(axis=1, pct=True) * 99
        has = pct.notna()
        comp_num += pct.fillna(0) * w
        comp_den += has * w
    comp = comp_num / comp_den.replace(0, np.nan)
    return comp.rank(axis=1, pct=True) * 99


# ── 시뮬레이션 ───────────────────────────────────────────
def simulate(dates, ind, rs_mat, stage_mat, conf_mat, climax_mat, opens, closes,
             kospi_gate, entry_stage, min_conf, trail_stop=-8.0, exit_ma="ma20",
             max_age=2, cooldown=5, max_track=10, max_daily=3):
    """일별 walk-forward. 신호=당일 종가, 체결=익일 시가."""
    # days_in_stage: 스테이지 값이 바뀐 후 경과 거래일
    stage_vals = stage_mat.fillna(-1)
    changed = stage_vals.ne(stage_vals.shift())
    grp = changed.cumsum()
    days_in_stage = grp.groupby(grp.columns.tolist(), axis=1).transform(lambda x: x) if False else None
    # 벡터화: 각 컬럼별 그룹 내 누적 카운트
    dis = pd.DataFrame({col: stage_vals[col].groupby(changed[col].cumsum()).cumcount()
                        for col in stage_vals.columns}, index=stage_vals.index)

    # exit_ma가 None/""/"ma0"이면 MA 이탈 비활성 (트레일링 단독)
    use_ma = bool(exit_ma) and exit_ma != "ma0"
    ma_exit = {t: ind[t][exit_ma] for t in ind} if use_ma else {}
    holdings = {}   # ticker -> dict
    pending_buy, pending_sell = [], []
    trades = []
    last_exit_date = {}
    weekly_entries = {}
    equity_realized = 0.0
    equity_curve = []

    for i, d in enumerate(dates):
        # 1) 전일 신호 체결 (오늘 시가)
        for t in pending_sell:
            if t not in holdings:
                continue
            h = holdings.pop(t)
            o = opens.at[d, t] if t in opens.columns else np.nan
            fill = o * (1 - SELL_SLIP) if not np.isnan(o) else h["last_close"]
            net = fill * (1 - SELL_TAX) / h["fill"] - 1 - COMMISSION_RT
            trades.append({"ticker": t, "entry_date": h["date"], "exit_date": d,
                           "ret": net * 100, "days": (d - h["date"]).days,
                           "reason": h["exit_reason"]})
            last_exit_date[t] = d
            equity_realized += net * 0.1
        pending_sell = []
        for t, conf in pending_buy:
            if t in holdings or len(holdings) >= max_track:
                continue
            o = opens.at[d, t] if t in opens.columns else np.nan
            if np.isnan(o) or o <= 0:
                continue
            fill = o * (1 + BUY_SLIP)
            holdings[t] = {"fill": fill, "date": d, "peak": fill, "conf": conf,
                           "last_close": fill}
            wk = d.isocalendar()[:2]
            weekly_entries[wk] = weekly_entries.get(wk, 0) + 1
        pending_buy = []

        # 2) 오늘 종가 기준 이탈 신호
        for t, h in holdings.items():
            c = closes.at[d, t] if t in closes.columns else np.nan
            if np.isnan(c):
                continue
            h["peak"] = max(h["peak"], c)
            h["last_close"] = c
            drop = (c / h["peak"] - 1) * 100
            ma = ma_exit[t].at[d] if t in ma_exit else np.nan
            if drop <= trail_stop + 1e-9:
                ret = (c / h["fill"] - 1) * 100
                h["exit_reason"] = "trail_stop" if (h["peak"] / h["fill"] - 1) * 100 > abs(trail_stop) else "stop_loss"
                pending_sell.append(t)
            elif not np.isnan(ma) and c < ma:
                h["exit_reason"] = "ma_exit"
                pending_sell.append(t)

        # 3) 오늘 종가 기준 편입 신호
        gate = kospi_gate.get(d, (True, False))
        if gate[0] and not gate[1]:
            slots = min(max_track - (len(holdings) - len(pending_sell)), max_daily)
            if slots > 0:
                cands = []
                for t in stage_mat.columns:
                    if t in holdings:
                        continue
                    le = last_exit_date.get(t)
                    if le is not None and (d - le).days <= cooldown:
                        continue
                    st = stage_mat.at[d, t]
                    if st != entry_stage:
                        continue
                    cf = conf_mat.at[d, t]
                    if cf < min_conf or climax_mat.at[d, t]:
                        continue
                    if dis.at[d, t] > max_age:
                        continue
                    cands.append((t, cf))
                cands.sort(key=lambda x: -x[1])
                pending_buy = cands[:slots]

        # 4) 평가금
        unreal = sum((h["last_close"] / h["fill"] - 1) * 0.1 for h in holdings.values())
        equity_curve.append(1 + equity_realized + unreal)

    return trades, weekly_entries, pd.Series(equity_curve, index=dates)


def metrics(trades, equity, kospi_close, weekly_entries):
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    wins = df[df.ret > 0]
    losses = df[df.ret <= 0]
    pf = wins.ret.sum() / abs(losses.ret.sum()) if len(losses) and losses.ret.sum() != 0 else float("inf")
    peak = equity.cummax()
    mdd = ((equity / peak - 1).min()) * 100
    yearly = df.groupby(df.exit_date.astype("datetime64[ns]").dt.year).ret.agg(["count", "sum", "mean"])
    kospi_ret = (kospi_close.iloc[-1] / kospi_close.iloc[0] - 1) * 100
    return {
        "trades": len(df),
        "win_rate": round(len(wins) / len(df) * 100, 1),
        "avg_ret": round(df.ret.mean(), 2),
        "med_ret": round(df.ret.median(), 2),
        "avg_win": round(wins.ret.mean(), 2) if len(wins) else 0,
        "avg_loss": round(abs(losses.ret.mean()), 2) if len(losses) else 0,
        "expectancy": round(df.ret.mean(), 2),
        "profit_factor": round(pf, 2),
        "avg_days": round(df.days.mean(), 1),
        "med_days": df.days.median(),
        "total_return_pct": round((equity.iloc[-1] - 1) * 100, 1),
        "mdd_pct": round(mdd, 1),
        "kospi_bh_pct": round(kospi_ret, 1),
        "exit_reasons": df.reason.value_counts().to_dict(),
        "yearly": {int(y): {"n": int(r["count"]), "sum": round(r["sum"], 1)} for y, r in yearly.iterrows()},
        "entries_per_week": round(np.mean(list(weekly_entries.values())), 2) if weekly_entries else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--trail", type=float, default=None, help="trail_stop_pct 오버라이드")
    ap.add_argument("--exit-ma", type=int, default=None, help="exit MA 기간 오버라이드 (0=MA 이탈 비활성)")
    ap.add_argument("--tag", default="", help="결과 파일 접미사")
    args = ap.parse_args()
    end = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    p = dict(cfg["params"])
    if args.trail is not None:
        p["trail_stop_pct"] = args.trail
    if args.exit_ma is not None:
        p["exit_ma_period"] = args.exit_ma

    print("유니버스 로드...", flush=True)
    tickers = fetch_universe()
    print(f"  {len(tickers)}종목", flush=True)
    data = load_ohlcv(tickers, args.start, end)
    print(f"OHLCV {len(data)}종목", flush=True)
    kospi = load_kospi(args.start, end)

    print("지표 사전계산...", flush=True)
    ind = {t: precompute(df) for t, df in data.items()}
    closes = pd.DataFrame({t: df["close"] for t, df in data.items()})
    opens = pd.DataFrame({t: df["open"] for t, df in data.items()})
    rs_mat = build_rs_matrix(closes)

    print("스테이지/신뢰도 매트릭스...", flush=True)
    stage_mat = pd.DataFrame(index=closes.index, columns=closes.columns, dtype=float)
    conf_mat = pd.DataFrame(0, index=closes.index, columns=closes.columns, dtype=float)
    climax_mat = pd.DataFrame(False, index=closes.index, columns=closes.columns)
    for t, idf in ind.items():
        rs_col = rs_mat[t].reindex(idf.index)
        st_list, cf_list, cx_list = [], [], []
        for (idx, row), rs in zip(idf.iterrows(), rs_col):
            st, cf, cx = stage_and_conf(row, rs if not np.isnan(rs) else 0)
            st_list.append(st); cf_list.append(cf); cx_list.append(cx)
        stage_mat[t] = pd.Series(st_list, index=idf.index, dtype="float")
        conf_mat[t] = pd.Series(cf_list, index=idf.index)
        climax_mat[t] = pd.Series(cx_list, index=idf.index)

    kc = kospi["close"]
    ma200 = kc.rolling(200).mean()
    ma50 = kc.rolling(50).mean()
    gate = {}
    for d in closes.index:
        if d in kc.index:
            ea = bool(kc.at[d] > ma200.at[d]) if not np.isnan(ma200.at[d]) else True
            ma50d = ma50.at[d] < ma50.shift(10).at[d] if not np.isnan(ma50.shift(10).at[d]) else False
            ex = bool(kc.at[d] < ma50.at[d] and ma50d) if not np.isnan(ma50.at[d]) else False
            gate[d] = (ea, ex)
    kospi_aligned = kc.reindex(closes.index).dropna()

    dates = list(closes.index)
    tracks = {
        "S3": (3, p.get("stage3_entry_confidence", 70)),
        "S1": (1, p.get("stage1_entry_confidence", 75)),
    }
    results = {}
    for name, (st, mc) in tracks.items():
        print(f"[{name}] 시뮬레이션...", flush=True)
        tr, wk, eq = simulate(dates, ind, rs_mat, stage_mat, conf_mat, climax_mat,
                              opens, closes, gate, st, mc,
                              trail_stop=p.get("trail_stop_pct", -8),
                              exit_ma=f"ma{p.get('exit_ma_period', 20)}",
                              max_age=p.get("stage_entry_max_age_days", 2),
                              cooldown=p.get("reentry_cooldown_days", 5),
                              max_track=p.get("max_holdings_per_track", 10),
                              max_daily=p.get("max_daily_entries_per_track", 3))
        results[name] = {"metrics": metrics(tr, eq, kospi_aligned, wk), "trades": tr}
        (CACHE_DIR / f"bt_result_{name}{args.tag}.json").write_text(
            json.dumps(results[name], default=str, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(results[name]["metrics"], ensure_ascii=False, indent=1, default=str), flush=True)

    if args.grid:
        grid_out = {}
        for name, (st, mc) in tracks.items():
            for ts in (-6, -8, -10):
                for ma in (10, 20, 60):
                    tr, wk, eq = simulate(dates, ind, rs_mat, stage_mat, conf_mat, climax_mat,
                                          opens, closes, gate, st, mc, trail_stop=ts, exit_ma=f"ma{ma}")
                    m = metrics(tr, eq, kospi_aligned, wk)
                    grid_out[f"{name}_ts{ts}_ma{ma}"] = {k: m.get(k) for k in
                        ("trades", "win_rate", "expectancy", "profit_factor", "total_return_pct", "mdd_pct", "avg_days")}
                    print(f"grid {name} ts={ts} ma={ma}: exp={m.get('expectancy')} pf={m.get('profit_factor')} ret={m.get('total_return_pct')}", flush=True)
            confs = (65, 75, 85) if name == "S1" else (60, 70, 80)
            for cc in confs:
                tr, wk, eq = simulate(dates, ind, rs_mat, stage_mat, conf_mat, climax_mat,
                                      opens, closes, gate, st, cc)
                m = metrics(tr, eq, kospi_aligned, wk)
                grid_out[f"{name}_conf{cc}"] = {k: m.get(k) for k in
                    ("trades", "win_rate", "expectancy", "profit_factor", "total_return_pct", "mdd_pct", "avg_days")}
                print(f"grid {name} conf={cc}: exp={m.get('expectancy')} pf={m.get('profit_factor')} ret={m.get('total_return_pct')}", flush=True)
        (CACHE_DIR / "bt_grid.json").write_text(json.dumps(grid_out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
