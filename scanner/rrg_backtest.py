"""RRG 섹터 로테이션 전략 백테스트 — backtest-expert 방법론 적용.

가설 (사전 명문화):
  H1 추세추종: "RS-Ratio 상위 섹터는 이후에도 상대 초과수익을 낸다"
     → 매주(5거래일) 확정 종가에 RS-Ratio 상위 N개 동일가중 보유, 주간 리밸런싱.
  H2 평균회귀: "약화→소외 전이 직후 섹터는 벤치마크 대비 반등한다"
     → 전이 발생 주에 진입, H주 보유 후 청산. 동시 신호는 동일가중 트랜치.
  대조군: 16개 섹터 동일가중 보유(벤치마크).

재량 0 규칙: 좌표는 운영 코드(sector_rrg.daily_xy)와 동일 경로로 계산.
비용: 리밸런싱 회전율 × 편도 cost_bp (기본 30bp = 수수료+슬리피지 비관적 추정,
스트레스 60bp). 주간 종가 체결 가정 — 실제 다음날 시가 체결과의 괴리는 비용 상향으로 벌충.

한계(정직 고지): 한국 섹터 ETF 다수가 2022~23년 상장이라 공통 구간 ≈ 2.2년(114주).
스킬 기준(최소 5년·복수 국면)에 미달 — 결과는 '참고', 배포 판정은 보수적으로.

사용: python rrg_backtest.py [--days 1500]
출력: 콘솔 + reports/rrg_backtest.md
"""

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from data_provider import fetch_ohlcv
from run_scan import SECTOR_ETFS
from sector_rrg import (GATE_D5_MIN, GATE_MA_DAYS, _heading, _quadrant, _y_only,
                        clean_daily, daily_xy, last_confirmed_close)

REPORT_PATH = Path(__file__).parent.parent / "reports" / "rrg_backtest.md"
WEEK = 5  # 거래일


def _metrics(weekly_rets: pd.Series) -> dict:
    """주간 수익률 시리즈 → 성과 지표."""
    if len(weekly_rets) < 8:
        return {}
    nav = (1 + weekly_rets).cumprod()
    years = len(weekly_rets) / 52
    cagr = nav.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    vol = weekly_rets.std() * np.sqrt(52)
    sharpe = (weekly_rets.mean() * 52) / vol if vol > 0 else 0
    dd = (nav / nav.cummax() - 1).min()
    yearly = (1 + weekly_rets).groupby(weekly_rets.index.year).prod() - 1
    return {"cagr": cagr * 100, "vol": vol * 100, "sharpe": sharpe, "mdd": dd * 100,
            "win_weeks": 100 * (weekly_rets > 0).mean(),
            "yearly": {int(y): round(v * 100, 1) for y, v in yearly.items()}}


def run_h1(xy_w, closes_w, top_n, cost_bp):
    """H1 추세추종: 매주 RS-Ratio 상위 N 동일가중, 주간 리밸런싱."""
    dates = closes_w.index
    rets, turnover_costs, prev_hold = [], 0.0, set()
    n_rebalance = 0
    for i in range(len(dates) - 1):
        t = dates[i]
        ranks = {s: xy.loc[t, "x"] for s, xy in xy_w.items() if t in xy.index}
        if len(ranks) < top_n:
            rets.append(0.0)
            continue
        hold = set(sorted(ranks, key=ranks.get, reverse=True)[:top_n])
        week_ret = np.mean([closes_w[s].iloc[i + 1] / closes_w[s].iloc[i] - 1 for s in hold])
        # 회전율 비용: 교체된 종목 비중 × 왕복(매도+매수) 편도비용×2
        changed = len(hold - prev_hold) / top_n if prev_hold else 1.0
        cost = changed * 2 * cost_bp / 10000
        if hold != prev_hold:
            n_rebalance += 1
        prev_hold = hold
        rets.append(week_ret - cost)
    return pd.Series(rets, index=dates[1:]), n_rebalance


def run_h2(xy_w, closes_w, hold_weeks, cost_bp):
    """H2 평균회귀: 약화→소외 전이 주에 진입, hold_weeks 보유. 무신호 주는 현금(0%)."""
    dates = closes_w.index
    # 전이 신호 주 탐지
    entries = []  # (date_idx, slug)
    trades = 0
    for slug, xy in xy_w.items():
        q = [_quadrant(r.x, r.y)[0] for _, r in xy.iterrows()]
        qi = {d: j for j, d in enumerate(xy.index)}
        for j in range(1, len(q)):
            if q[j] == "lagging" and q[j - 1] == "weakening":
                d = xy.index[j]
                if d in dates:
                    entries.append((dates.get_loc(d), slug))
                    trades += 1
    rets = []
    for i in range(len(dates) - 1):
        active = [s for (ei, s) in entries if ei <= i < ei + hold_weeks]
        if not active:
            rets.append(0.0)
            continue
        week_ret = np.mean([closes_w[s].iloc[i + 1] / closes_w[s].iloc[i] - 1 for s in active])
        # 진입/청산 주에만 비용 (근사: 주당 활성 트랜치 중 신규+만기 비율)
        opens = sum(1 for (ei, s) in entries if ei == i)
        closes_n = sum(1 for (ei, s) in entries if ei + hold_weeks - 1 == i)
        cost = ((opens + closes_n) / max(1, len(active))) * cost_bp / 10000
        rets.append(week_ret - cost)
    return pd.Series(rets, index=dates[1:]), trades


def _trend_pass(xy_w_sec, i_w):
    """주간 xy 격자에서 i_w 시점의 RRG 트렌드 조건 (운영 코드와 동일 규칙).

    부상/주도 + 상방 heading + Y축 단독 아님. 룩어헤드 없음 — i_w까지의 점만 사용."""
    pts = xy_w_sec.iloc[: i_w + 1]
    if len(pts) < 2:
        return False
    x, y = float(pts["x"].iloc[-1]), float(pts["y"].iloc[-1])
    quadrant = _quadrant(x, y)[0]
    heading = _heading(x - float(pts["x"].iloc[-2]), y - float(pts["y"].iloc[-2]))
    streak = 1
    for j in range(len(pts) - 2, 0, -1):
        h = _heading(float(pts["x"].iloc[j]) - float(pts["x"].iloc[j - 1]),
                     float(pts["y"].iloc[j]) - float(pts["y"].iloc[j - 1]))
        if h != heading:
            break
        streak += 1
    win = pts.iloc[-7:]
    x_move = float(win["x"].iloc[-1]) - float(win["x"].iloc[0])
    y_only = _y_only(quadrant, heading, streak, x, x_move)
    return quadrant in ("improving", "leading") and "N" in heading and not y_only


def run_h3(xy_w, closes_w, daily_clean, cost_bp, use_gate):
    """H3 게이트 전략: 매주 [RRG 트렌드 조건 (+ 절대가격 게이트)] 통과 섹터 동일가중.

    use_gate=False → RRG 조건 단독(H3a), True → +MA60 위·5일 > -5% (H3b).
    무통과 주는 현금(0%). 게이트의 독립 기여를 a/b 비교로 분리한다."""
    dates = closes_w.index
    rets, prev_hold, n_rebalance = [], set(), 0
    for i in range(len(dates) - 1):
        t = dates[i]
        hold = set()
        for s, xy in xy_w.items():
            if t not in xy.index:
                continue
            i_w = xy.index.get_loc(t)
            if not _trend_pass(xy, i_w):
                continue
            if use_gate:
                dc = daily_clean[s]
                hist = dc[dc.index <= t]
                if len(hist) < GATE_MA_DAYS + 6:
                    continue
                cur = float(hist.iloc[-1])
                if cur <= float(hist.iloc[-GATE_MA_DAYS:].mean()):
                    continue
                if (cur / float(hist.iloc[-6]) - 1) * 100 <= GATE_D5_MIN:
                    continue
            hold.add(s)
        if not hold:
            rets.append(0.0)
            prev_hold = set()
            continue
        week_ret = np.mean([closes_w[s].iloc[i + 1] / closes_w[s].iloc[i] - 1 for s in hold])
        changed = len(hold - prev_hold) / len(hold) if prev_hold else 1.0
        cost = changed * 2 * cost_bp / 10000
        if hold != prev_hold:
            n_rebalance += 1
        prev_hold = hold
        rets.append(week_ret - cost)
    return pd.Series(rets, index=dates[1:]), n_rebalance


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1500)
    args = ap.parse_args()

    daily = {}
    for slug, (code, name) in SECTOR_ETFS.items():
        try:
            df = await fetch_ohlcv(code, days=args.days)
            if df is not None and len(df) >= 150:
                daily[slug] = df["close"]
        except Exception as e:
            print(f"[조회실패] {slug}: {e}")
    cutoff = last_confirmed_close(dt.datetime.now())

    lines = ["# RRG 섹터 로테이션 백테스트 (backtest-expert 방법론)",
             f"\n- 실행: {dt.date.today().isoformat()} · 섹터 {len(daily)}개 · 비용 = 편도 bp(수수료+슬리피지)",
             "- 체결 가정: 주간 확정 종가 (다음날 시가 괴리는 비용 상향으로 벌충)",
             "- **한계**: 공통 데이터 구간이 짧아(신생 ETF) 스킬 기준(5년+) 미달 — 참고용 판정\n"]

    # 파라미터 스윕: SMA 창 × 전략 파라미터 × 비용
    for sma in (40, 60, 80):
        xy_daily, benchmark, _ = daily_xy(daily, cutoff, sma_days=sma, roc_days=sma // 3)
        if not xy_daily:
            continue
        # 주간 격자
        xy_w = {s: xy.iloc[list(range(len(xy) - 1, -1, -WEEK))[::-1]] for s, xy in xy_daily.items()}
        cdf = pd.DataFrame({s: clean_daily(daily[s])[0][clean_daily(daily[s])[0].index.date <= cutoff]
                            for s in xy_w}).dropna()
        closes_w = cdf.iloc[list(range(len(cdf) - 1, -1, -WEEK))[::-1]]
        # 벤치마크 주간 수익률 (동일가중 16개)
        bm_rets = (closes_w / closes_w.shift(1) - 1).mean(axis=1).dropna()
        mb = _metrics(bm_rets)
        lines.append(f"\n## SMA {sma}일 (ROC {sma // 3}일) — 표본 {len(closes_w)}주")
        lines.append(f"- 벤치마크(동일가중 보유): CAGR {mb['cagr']:+.1f}% · Sharpe {mb['sharpe']:.2f} · MDD {mb['mdd']:.1f}% · 연도별 {mb['yearly']}")
        for cost in (15, 30, 60):
            for n in (3, 5):
                r, nreb = run_h1(xy_w, closes_w, n, cost)
                m = _metrics(r)
                if m:
                    lines.append(f"- H1 추세추종 top{n} (비용 {cost}bp, 리밸런스 {nreb}회): "
                                 f"CAGR {m['cagr']:+.1f}% · Sharpe {m['sharpe']:.2f} · MDD {m['mdd']:.1f}% · 연도별 {m['yearly']}")
            for h in (4, 8):
                r, ntr = run_h2(xy_w, closes_w, h, cost)
                m = _metrics(r)
                if m:
                    lines.append(f"- H2 평균회귀 hold{h}주 (비용 {cost}bp, 진입 {ntr}건): "
                                 f"CAGR {m['cagr']:+.1f}% · Sharpe {m['sharpe']:.2f} · MDD {m['mdd']:.1f}% · 연도별 {m['yearly']}")
            daily_clean = {s: clean_daily(daily[s])[0] for s in xy_w}
            for use_gate, tag in ((False, "H3a RRG조건 단독"), (True, "H3b RRG+절대가격 게이트")):
                r, nreb = run_h3(xy_w, closes_w, daily_clean, cost, use_gate)
                m = _metrics(r)
                if m:
                    lines.append(f"- {tag} (비용 {cost}bp, 리밸런스 {nreb}회): "
                                 f"CAGR {m['cagr']:+.1f}% · Sharpe {m['sharpe']:.2f} · MDD {m['mdd']:.1f}% · 연도별 {m['yearly']}")

    lines.append("\n## 판정 기준 (backtest-expert)")
    lines.append("- 전략이 벤치마크를 이기려면: 전 비용 구간·전 SMA 창에서 벤치마크 대비 Sharpe 우위(플래토), "
                 "연도별 과반 양(+), 표본 100+ 거래. 하나라도 미달이면 배포 불가(관찰 도구 유지).")
    report = "\n".join(lines)
    print(report)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\n저장: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
