"""트레이더 핸드북 전략 백테스트 — 한국시장 5개년 (일봉).

전략 원문: 바탕화면 트레이더핸드북_전략.md (책 규칙만, 패턴·진입전술 제외).
  · 필터: 종가>기준MA & MA상승 / ADR20 3~5% / 거래대금 20일평균 ≥30억
          / 분기 순이익 YoY +25% (최근 2개 분기, DART) / RS 백분위 ≥70
  · 시장 게이트: KOSPI 종가>MA(강: MA상승 / 보통: 종가>MA만 / OFF: 이하)
  · 비중: 강15%·보통10% 시작, 엣지(250일 최대 거래량, 연간 NI+25%, RS≥90)당 +2.5%p, 상한 20%
  · 총 개방 리스크 Σ(비중×손절폭) 한도: 강10%·보통5%
  · 손절: max(진입가−4%[ADR>4.5%면 −6%], MA−0.5%) — 종가 판정, 익일 시가 체결
  · 매도: +5% 절반매도→본전스톱 / 수익 2.5×초기손절폭→본전스톱 / MA 아래 2연속 종가→전량
  · 트랙: ema21(스윙) vs sma50(포지션), 벤치 KOSPI

데이터·PIT: 유니버스 = ~/krx_pit_*.json 의 k200+q150(월별, 신호일 이전 덤프);
  가격 FDR OHLCV(backtest.load_ohlcv, 캐시 tmp/); DART는 분기말+45일 래그
  (canslim_backtest.quarter_fiscal_for) — 룩어헤드 없음.
한계(리포트 명시): RS는 유니버스 내 백분위(책의 '60% 날짜 아웃퍼폼' 대리),
  진입전술 없음(필터 충족 시 익일 시가), 장중 스톱 불가(종가 판정),
  load_ohlcv 의 260봉 요건으로 신규상장 초기 제외, _corp_map 생존편향(상방).

실행:
  python scanner/handbook_backtest.py --warm                 # DART 캐시 워밍만
  python scanner/handbook_backtest.py --track ema21 --limit-months 6   # 스모크
  python scanner/handbook_backtest.py --track both           # 본 실행 + 리포트
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import backtest as _bt
import value_backtest as vb
from canslim_backtest import quarter_fiscal_for, NIStore
from value_backtest import load_krx_dumps, _knum, fiscal_year_for
from backtest import BUY_SLIP, SELL_SLIP, SELL_TAX, COMMISSION_RT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("handbook_backtest")

ROOT = Path(__file__).parent.parent
CACHE = ROOT / "tmp"
CACHE.mkdir(exist_ok=True)
_bt.CACHE_DIR = CACHE
vb.CACHE_DIR = CACHE

PX_START, PX_END = "2020-05-01", "2026-08-08"
SIM_START, SIM_END = "2021-08-02", "2026-08-08"

P = {
    "adr_lo": 3.0, "adr_hi": 5.0,          # 책: ADR 3~5%
    "min_turnover": 3_000_000_000,          # 책: $3M 거래대금 환산 30억
    "q_yoy": 25.0, "n_quarters": 2,         # 책: 최근 2분기 순이익 +25%
    "rs_min": 70, "rs_edge": 90,            # 책 RS 규칙의 대리 지표
    "stop_pct": 0.04, "stop_pct_hi": 0.06,  # 책: 손절 1~4%, 변동성 크면 6%
    "adr_hi_stop": 4.5,                     # ADR>4.5% → 6% 손절 허용
    "half_take": 0.05,                      # 책: +5% 절반 매도
    "be_mult": 2.5,                         # 책: 초기손절폭 2~3배 수익 → 본전
    "ma_exit_days": 2,                      # 책: MA 아래 2연속 종가 → 청산
    "base_w": {"strong": 0.15, "normal": 0.10},   # 책: 시장 강도별 시작 비중
    "edge_w": 0.025, "max_w": 0.20,         # 책: 엣지당 +2.5%p, 상한 20%
    "risk_cap": {"strong": 0.10, "normal": 0.05}, # 책: 총 개방 리스크 한도
    "max_pos": 10, "max_daily": 3,          # 책: 8~10종목 / 하루 집중 1~3(+예비)
    "cooldown": 5,                          # 손절 후 재진입 쿨다운 (backtest.py 선례)
    "pre_rs": 50,                           # DART 사전컷 (rs_min보다 느슨 — 편향 없음)
}
NUM_PARAMS = 12  # evaluate_backtest 과적합 페널티용


# ── 유니버스 (PIT k200+q150) ─────────────────────────────
def load_universe():
    dumps = load_krx_dumps()
    keys = sorted(dumps.keys())
    members, names, markets = {}, {}, {}
    for k in keys:
        v = dumps[k]
        mem = {str(c).zfill(6) for c in v.get("k200", [])} | \
              {str(c).zfill(6) for c in v.get("q150", [])}
        members[k] = mem
        for r in v["cap"]:
            code = str(r[0]).zfill(6)
            if code in mem:
                names[code] = str(r[1])
                mkt = str(r[2] or "")
                markets[code] = "KOSDAQ" if "KOSDAQ" in mkt else "KOSPI"
    return keys, members, names, markets


def key_for(keys, d: pd.Timestamp) -> str:
    ds = d.strftime("%Y%m%d")
    prior = [k for k in keys if k <= ds]
    return prior[-1] if prior else keys[0]


# ── 지표 사전계산 ────────────────────────────────────────
def precompute(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    out = pd.DataFrame(index=df.index)
    out["open"], out["close"] = df["open"], c
    out["ema21"] = c.ewm(span=21, adjust=False).mean()
    out["sma50"] = c.rolling(50).mean()
    out["adr20"] = ((h - l) / c * 100).rolling(20).mean()
    out["turn20"] = (c * v).rolling(20).mean()
    # 엣지: 250일 최대 거래량 경신 + 상승 마감 (최근 20일 내 발생)
    hv = (v > v.shift(1).rolling(250).max()) & (c > c.shift(1))
    out["hv_edge"] = hv.rolling(20).max().fillna(0)
    return out


def make_rs(ind: dict, codes: set, markets: dict, d: pd.Timestamp) -> dict[str, int]:
    """유니버스 내 시장별 가중수익률 백분위 (0.5*3M+0.3*6M+0.2*12M, canslim_backtest 방식)."""
    by_mkt: dict[str, dict[str, float]] = {}
    for c in codes:
        df = ind.get(c)
        if df is None:
            continue
        s = df["close"][df.index <= d].dropna()
        if len(s) < 253:
            continue
        cur, p60, p120, p252 = float(s.iloc[-1]), float(s.iloc[-61]), float(s.iloc[-121]), float(s.iloc[-253])
        if min(cur, p60, p120, p252) <= 0:
            continue
        by_mkt.setdefault(markets.get(c, "KOSPI"), {})[c] = (
            (cur / p60 - 1) * .5 + (cur / p120 - 1) * .3 + (cur / p252 - 1) * .2)
    rs = {}
    for vals in by_mkt.values():
        items = sorted(vals.items(), key=lambda x: x[1])
        n = len(items)
        for i, (c, _) in enumerate(items):
            rs[c] = round(i / (n - 1) * 98 + 1) if n > 1 else 50
    return rs


# ── 펀더멘털 (월별, DART) ────────────────────────────────
def _prev_quarter(y: int, reprt: str):
    order = ["11013", "11012", "11014"]
    i = order.index(reprt)
    return (y, order[i - 1]) if i > 0 else (y - 1, order[-1])


def fund_flags(store: NIStore, code: str, sig: pd.Timestamp) -> tuple[bool, bool]:
    """(최근 2분기 YoY≥25% 충족, 연간 NI YoY≥25% 엣지)."""
    qf = quarter_fiscal_for(sig)
    if qf is None:
        return False, False
    y, r = qf
    g1 = store.q_yoy(code, y, r)
    y2, r2 = _prev_quarter(y, r)
    g2 = store.q_yoy(code, y2, r2)
    ok = g1 is not None and g2 is not None and g1 >= P["q_yoy"] and g2 >= P["q_yoy"]
    a = store.annual(code, fiscal_year_for(sig))
    a_ok = bool(a and a.get("ni_growth") is not None and a["ni_growth"] >= P["q_yoy"])
    return ok, a_ok


# ── 시장 게이트 ──────────────────────────────────────────
GATE_MODE = "book"   # "book" = 규칙집 원문 / "simple" = 종가<MA 즉시 OFF


def book_gate(bench: pd.DataFrame, ma_col: str) -> pd.Series:
    """규칙집 0번: ON = 종가>MA. OFF = MA 아래 2연속 종가 & 2번째 종가 < 전일 저가.

    확정 전까지는 직전 상태를 유지한다(스티키). simple 모드는 종가<MA 즉시 OFF.
    """
    c = bench["close"].to_numpy(float)
    m = bench[ma_col].to_numpy(float)
    lo = bench["low"].to_numpy(float) if "low" in bench.columns else c
    st = np.zeros(len(c), dtype=bool)
    on = False
    for i in range(len(c)):
        if np.isnan(m[i]):
            st[i] = False
            continue
        if GATE_MODE == "simple":
            on = c[i] > m[i]
        elif not on:
            on = c[i] > m[i]
        elif i > 0 and c[i] < m[i] and c[i - 1] < m[i - 1] and c[i] < lo[i - 1]:
            on = False
        st[i] = on
    return pd.Series(st, index=bench.index)


def _test_book_gate():
    """1연속 이탈은 ON 유지, 2연속+전일저가 하회에서만 OFF, 종가>MA 면 즉시 재ON."""
    global GATE_MODE
    df = pd.DataFrame({          # ma 를 100 고정하고 종가/저가만 움직인다
        "close": [101, 99, 102, 99, 98, 99, 101],
        "low":   [100, 98, 101, 98, 97, 98, 100],
        "ma":    [100] * 7,
    })
    GATE_MODE = "book"
    g = list(book_gate(df, "ma"))
    #        0 ON  1 이탈1회→유지  2 복귀  3 이탈1회  4 이탈2회&98<98? 아님→유지
    #        5 이탈3회 99<97? 아님→유지  6 종가>MA
    assert g == [True, True, True, True, True, True, True], g
    df.loc[4, ["close", "low"]] = [96, 95]      # 2번째 종가 96 < 전일 저가 98 → OFF
    g = list(book_gate(df, "ma"))
    assert g == [True, True, True, True, False, False, True], g
    GATE_MODE = "simple"
    assert list(book_gate(df, "ma")) == [True, False, True, False, False, False, True]
    GATE_MODE = "book"
    print("book_gate 자체검증 통과")


# ── 시뮬레이션 ───────────────────────────────────────────
def simulate(track: str, days, ind, bench_ind, keys, members, names, markets,
             rs_by_key, fund_by_key, slip_mult=1.0):
    ma_col = track
    buy_slip, sell_slip = BUY_SLIP * slip_mult, SELL_SLIP * slip_mult
    sell_cost = SELL_TAX + COMMISSION_RT

    gate = book_gate(bench_ind, ma_col)
    cash, pos = 1.0, {}   # code -> dict(shares, cost, entry, stop, init_stop, half, below)
    pending = []          # (code, action['buy','sell','half'], weight|None, reason)
    trades, nav_hist = [], []
    cooldown_until: dict[str, int] = {}
    day_idx = {d: i for i, d in enumerate(days)}

    def row(c, d):
        df = ind.get(c)
        if df is None or d not in df.index:
            return None
        return df.loc[d]

    def close_px(c, d):
        r = row(c, d)
        if r is None or np.isnan(r["close"]):
            return None
        return float(r["close"])

    def book(code, fill, frac, d, reason):
        nonlocal cash
        h = pos[code]
        sh = h["shares"] * frac
        net = fill * (1 - sell_cost)
        cash += sh * net
        trades.append({"ticker": code, "name": names.get(code, code),
                       "entry_date": str(h["entry"].date()), "exit_date": str(d.date()),
                       "ret": round((net / h["cost"] - 1) * 100, 2),
                       "days": (d - h["entry"]).days, "reason": reason, "frac": frac})
        if frac >= 1.0:
            del pos[code]
        else:
            h["shares"] -= sh

    for i, d in enumerate(days):
        # 1) 전일 신호 체결 (시가)
        todo, pending = pending, []
        for code, action, w, reason in todo:
            if action in ("sell", "half"):
                if code not in pos:
                    continue
                r = row(code, d)
                fill = float(r["open"]) * (1 - sell_slip) if r is not None and not np.isnan(r["open"]) \
                    else (close_px(code, days[i - 1]) or pos[code]["cost"]) * (1 - sell_slip)
                frac = 0.5 if action == "half" else 1.0
                book(code, fill, frac, d, reason)
                if action == "half" and code in pos:
                    pos[code]["stop"] = pos[code]["cost"]   # 책: 절반매도 후 본전 스톱
                    pos[code]["half"] = True
                if action == "sell" and reason == "stop":
                    cooldown_until[code] = i + P["cooldown"]
            elif action == "buy" and code not in pos and len(pos) < P["max_pos"]:
                r = row(code, d)
                if r is None or np.isnan(r["open"]) or r["open"] <= 0:
                    continue
                fill = float(r["open"]) * (1 + buy_slip)
                nav_now = cash + sum(h["shares"] * (close_px(c2, days[i - 1]) or h["cost"])
                                     for c2, h in pos.items())
                budget = min(nav_now * w, cash)
                if budget < nav_now * 0.02:
                    continue
                ma = float(r[ma_col])
                stop_cap = P["stop_pct_hi"] if r["adr20"] > P["adr_hi_stop"] else P["stop_pct"]
                stop = max(fill * (1 - stop_cap), ma * 0.995)
                if stop >= fill:                    # MA가 진입가 위 (갭) — 고정폭 사용
                    stop = fill * (1 - stop_cap)
                pos[code] = {"shares": budget / fill, "cost": fill, "entry": d,
                             "stop": stop, "init_stop": (fill - stop) / fill,
                             "half": False, "below": 0}
                cash -= budget

        # 2) 상폐/시세 단절 강제 청산
        for code in list(pos):
            df = ind[code]
            lv = df["close"].last_valid_index()
            if lv is not None and d > lv:
                book(code, float(df.at[lv, "close"]) * (1 - sell_slip), 1.0, d, "delisted")

        # 3) 종가 판정 → 익일 주문
        for code in list(pos):
            r = row(code, d)
            if r is None or np.isnan(r["close"]):
                continue
            h = pos[code]
            c = float(r["close"])
            ma = float(r[ma_col]) if not np.isnan(r[ma_col]) else None
            if c <= h["stop"]:
                pending.append((code, "sell", None, "stop" if not h["half"] else "be_stop"))
                continue
            h["below"] = h["below"] + 1 if (ma and c < ma) else 0
            if h["below"] >= P["ma_exit_days"]:
                pending.append((code, "sell", None, "ma_exit"))
                continue
            if not h["half"] and c >= h["cost"] * (1 + P["half_take"]):
                pending.append((code, "half", None, "half_take"))
            elif not h["half"] and c >= h["cost"] * (1 + P["be_mult"] * h["init_stop"]):
                h["stop"] = max(h["stop"], h["cost"])

        # 4) 시장 게이트 + 신규 진입 신호 (종가)
        br = bench_ind.loc[d] if d in bench_ind.index else None
        strength = None
        if br is not None and bool(gate.get(d, False)):
            strength = "strong" if br[ma_col] > br[f"{ma_col}_prev"] else "normal"
        if strength and len(pos) < P["max_pos"]:
            k = key_for(keys, d)
            rs = rs_by_key.get(k, {})
            fund = fund_by_key.get(k, {})
            nav_c = cash + sum(h["shares"] * (close_px(c2, d) or h["cost"]) for c2, h in pos.items())
            open_risk = sum(h["shares"] * max(0.0, (close_px(c2, d) or h["cost"]) - h["stop"])
                            for c2, h in pos.items()) / nav_c if nav_c > 0 else 0
            cap = P["risk_cap"][strength]
            cands = []
            for code in members.get(k, ()):
                if code in pos or cooldown_until.get(code, -1) > i:
                    continue
                f = fund.get(code)
                if not f or not f[0]:
                    continue
                if rs.get(code, 0) < P["rs_min"]:
                    continue
                r = row(code, d)
                if r is None or np.isnan(r["close"]) or np.isnan(r[ma_col]):
                    continue
                df = ind[code]
                j = df.index.get_loc(d)
                if j < 5:
                    continue
                ma_prev = df[ma_col].iloc[j - 5]
                if not (r["close"] > r[ma_col] and r[ma_col] > ma_prev):
                    continue
                if not (P["adr_lo"] <= r["adr20"] <= P["adr_hi"]):
                    continue
                if r["turn20"] < P["min_turnover"]:
                    continue
                edges = int(r["hv_edge"] > 0) + int(f[1]) + int(rs.get(code, 0) >= P["rs_edge"])
                w = min(P["base_w"][strength] + P["edge_w"] * edges, P["max_w"])
                stop_cap = P["stop_pct_hi"] if r["adr20"] > P["adr_hi_stop"] else P["stop_pct"]
                cands.append((rs.get(code, 0), code, w, w * stop_cap))
            cands.sort(reverse=True)
            n_new = 0
            for _rs, code, w, add_risk in cands:
                if n_new >= P["max_daily"] or len(pos) + n_new >= P["max_pos"]:
                    break
                if open_risk + add_risk > cap:
                    continue
                pending.append((code, "buy", w, "entry"))
                open_risk += add_risk
                n_new += 1

        nav = cash + sum(h["shares"] * (close_px(c2, d) or h["cost"]) for c2, h in pos.items())
        nav_hist.append((d, nav))

    nav = pd.Series(dict(nav_hist)).sort_index()
    return nav, trades, len(pos)


# ── 지표 ─────────────────────────────────────────────────
def metrics(nav: pd.Series, trades, bench_close: pd.Series, open_pos: int) -> dict:
    daily = nav.pct_change().dropna()
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 1e-9)
    tdf = pd.DataFrame(trades) if trades else pd.DataFrame(columns=["ret", "days", "reason"])
    wins, losses = tdf[tdf.ret > 0], tdf[tdf.ret <= 0]
    b = bench_close.reindex(nav.index).ffill().dropna()
    gross_w = wins.ret.sum() if len(wins) else 0.0
    gross_l = abs(losses.ret.sum()) if len(losses) else 0.0
    yearly = {}
    prev = nav.iloc[0]
    for dd, v in nav.resample("YE").last().items():
        yearly[dd.year] = round((v / prev - 1) * 100, 1)
        prev = v
    return {
        "period": f"{nav.index[0].date()} ~ {nav.index[-1].date()} ({years:.1f}y)",
        "total_return_pct": round((nav.iloc[-1] / nav.iloc[0] - 1) * 100, 1),
        "cagr_pct": round(((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1) * 100, 2),
        "mdd_pct": round(((nav / nav.cummax() - 1).min()) * 100, 1),
        "sharpe": round(daily.mean() / daily.std() * np.sqrt(252), 2) if daily.std() > 0 else 0,
        "bench_total_pct": round((b.iloc[-1] / b.iloc[0] - 1) * 100, 1),
        "trades": len(tdf),
        "open_positions": open_pos,
        "win_rate": round(len(wins) / len(tdf) * 100, 1) if len(tdf) else None,
        "avg_win": round(wins.ret.mean(), 2) if len(wins) else 0,
        "avg_loss": round(abs(losses.ret.mean()), 2) if len(losses) else 0,
        "expectancy": round(tdf.ret.mean(), 2) if len(tdf) else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
        "avg_days": round(tdf.days.mean(), 0) if len(tdf) else None,
        "med_days": round(tdf.days.median(), 0) if len(tdf) else None,
        "exit_reasons": tdf.reason.value_counts().to_dict() if len(tdf) else {},
        "yearly_pct": yearly,
    }


# ── 메인 ─────────────────────────────────────────────────
def main():
    global GATE_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", default="both", choices=["ema21", "sma50", "both"])
    ap.add_argument("--gate", default="book", choices=["book", "simple"])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--limit-months", type=int, default=0)
    ap.add_argument("--warm", action="store_true", help="DART 캐시 워밍만")
    ap.add_argument("--no-fund", action="store_true", help="펀더멘털 게이트 생략(스모크)")
    ap.add_argument("--slip-mult", type=float, default=1.0)
    args = ap.parse_args()
    if args.selftest:
        _test_book_gate()
        return
    GATE_MODE = args.gate

    keys, members, names, markets = load_universe()
    all_codes = sorted(set().union(*members.values()))
    logger.info("유니버스: 덤프 %d개, 누적 종목 %d", len(keys), len(all_codes))

    raw = _bt.load_ohlcv({c: names.get(c, c) for c in all_codes}, PX_START, PX_END)
    ind = {c: precompute(df) for c, df in raw.items()}
    logger.info("가격 확보 %d/%d (260봉 미달·상폐 %d 제외)",
                len(ind), len(all_codes), len(all_codes) - len(ind))

    kospi = _bt.load_kospi(PX_START, PX_END)
    bench_ind = pd.DataFrame({
        "close": kospi["close"],
        "low": kospi["low"],
        "ema21": kospi["close"].ewm(span=21, adjust=False).mean(),
        "sma50": kospi["close"].rolling(50).mean(),
    })
    for m in ("ema21", "sma50"):
        bench_ind[f"{m}_prev"] = bench_ind[m].shift(5)

    end = SIM_END
    if args.limit_months:
        end = str((pd.Timestamp(SIM_START) + pd.DateOffset(months=args.limit_months)).date())
    days = [d for d in bench_ind.index if SIM_START <= str(d.date()) <= end]

    # 월별 RS·펀더멘털 (신호일 = 각 덤프 키 날짜)
    used_keys = sorted({key_for(keys, d) for d in days})
    rs_by_key = {}
    for k in used_keys:
        rs_by_key[k] = make_rs(ind, members[k], markets, pd.Timestamp(k))
    logger.info("RS 계산 완료 (%d개 신호일)", len(used_keys))

    fund_by_key: dict[str, dict] = {k: {} for k in used_keys}
    if not args.no_fund:
        store = NIStore()
        for n, k in enumerate(used_keys):
            sig = pd.Timestamp(k)
            rs = rs_by_key[k]
            for code in members[k]:
                if rs.get(code, 0) < P["pre_rs"]:   # 사전컷 — rs_min보다 느슨
                    fund_by_key[k][code] = (False, False)
                    continue
                fund_by_key[k][code] = fund_flags(store, code, sig)
            store.save()
            logger.info("DART %d/%d (%s) 호출누적 %d", n + 1, len(used_keys), k, store.calls)
        store.save()
        if args.warm:
            logger.info("워밍 완료 — DART 호출 %d회", store.calls)
            return
    else:
        for k in used_keys:
            fund_by_key[k] = {c: (True, False) for c in members[k]}

    tracks = ["ema21", "sma50"] if args.track == "both" else [args.track]
    results = {}
    for tr in tracks:
        nav, trades, open_pos = simulate(tr, days, ind, bench_ind, keys, members,
                                         names, markets, rs_by_key, fund_by_key,
                                         slip_mult=args.slip_mult)
        m = metrics(nav, trades, bench_ind["close"], open_pos)
        results[tr] = {"metrics": m, "trades": trades,
                       "nav": {str(d.date()): round(v, 6) for d, v in nav.items()}}
        tag = (f"_{args.gate}") + (f"_{args.slip_mult}" if args.slip_mult != 1.0 else "")
        out = CACHE / f"hbt_result_{tr}{tag}.json"
        out.write_text(json.dumps(results[tr], ensure_ascii=False), encoding="utf-8")
        logger.info("[%s] 거래 %s건 승률 %s%% 총수익 %s%% MDD %s%% (벤치 %s%%) → %s",
                    tr, m["trades"], m["win_rate"], m["total_return_pct"],
                    m["mdd_pct"], m["bench_total_pct"], out.name)


if __name__ == "__main__":
    main()
