"""Phase 4 — RRG 리플레이 검증 (1회성/수동 실행).

새 방식(주간 RRG)과 구 방식(5일 수익률 백분위)의 과거 성적을 비교한다.
순환논리 배제를 위해 안정성 지표 외에 forward return을 함께 측정한다.

지표:
  1. 사분면 전환 빈도 — 섹터당 10주 평균 전환 횟수 (신 vs 구)
  2. 부상→주도(Improving→Leading) 진입 이벤트의 4/8/12주 forward
     상대수익률(섹터/동일가중 벤치마크) — 판단어 코멘트 해금 여부의 게이트

사용: python replay_rrg.py [--days 1095]
      (기본 1095일 ≈ 3년 백필 시도 — 데이터 소스가 300일만 주면 그 범위로 축소됨)
출력: 콘솔 + reports/rrg_replay.md
"""

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp949 콘솔 대응

import pandas as pd

from data_provider import fetch_ohlcv
from run_scan import SECTOR_ETFS
from sector_rrg import _quadrant, clean_daily, daily_xy, last_confirmed_close

REPORT_PATH = Path(__file__).parent.parent / "reports" / "rrg_replay.md"


def _legacy_quadrant_series(weekly_closes: pd.DataFrame) -> dict[str, pd.Series]:
    """구 방식 근사를 주간 격자에서 재현: 장기(1/3/6M 가중 수익률 백분위) × 단기(1주 수익률 백분위).
    (운영은 일간 5일 수익률이었으나 주간 격자 비교를 위해 1주 수익률로 등가 치환)"""
    long_score = (weekly_closes.pct_change(4) * 0.5
                  + weekly_closes.pct_change(13) * 0.3
                  + weekly_closes.pct_change(26) * 0.2)
    short_score = weekly_closes.pct_change(1)
    lr = long_score.rank(axis=1, pct=True) * 100
    sr_ = short_score.rank(axis=1, pct=True) * 100
    out = {}
    for slug in weekly_closes.columns:
        q = pd.Series(index=weekly_closes.index, dtype=object)
        for t in weekly_closes.index:
            x, y = sr_.at[t, slug], lr.at[t, slug]
            if pd.isna(x) or pd.isna(y):
                continue
            # 구 방식 사분면: 축 스케일이 다르므로 50 기준
            q[t] = _quadrant(100 + (x - 50), 100 + (y - 50))[0]
        out[slug] = q.dropna()
    return out


def _transition_rate(quads: pd.Series) -> float:
    """10주당 사분면 전환 횟수."""
    if len(quads) < 2:
        return 0.0
    changes = (quads != quads.shift(1)).iloc[1:].sum()
    return 10.0 * changes / (len(quads) - 1)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    args = ap.parse_args()

    daily = {}
    weekly_raw = {}
    for slug, (code, name) in SECTOR_ETFS.items():
        try:
            df = await fetch_ohlcv(code, days=args.days)
        except Exception as e:
            print(f"[조회실패] {slug}: {e}")
            continue
        if df is not None and len(df) >= 150:
            daily[slug] = df["close"]
    print(f"조회 완료: {len(daily)}/{len(SECTOR_ETFS)} 섹터, "
          f"최장 {max(len(s) for s in daily.values())}일")

    cutoff = last_confirmed_close(dt.datetime.now())
    xy_daily, benchmark, data_flags = daily_xy(daily, cutoff)
    if not xy_daily:
        print("데이터 부족 — 리플레이 불가")
        return
    # 주간 격자(5거래일 간격)로 샘플링 — 전환·이벤트 판정 단위는 주간 유지
    xy_map = {slug: xy.iloc[list(range(len(xy) - 1, -1, -5))[::-1]] for slug, xy in xy_daily.items()}

    # 벤치마크와 정렬된 주간 종가 (forward return 계산용)
    daily_closes_df = pd.DataFrame({
        slug: clean_daily(daily[slug])[0][clean_daily(daily[slug])[0].index.date <= cutoff]
        for slug in xy_map
    }).dropna()
    weekly_closes = daily_closes_df.iloc[list(range(len(daily_closes_df) - 1, -1, -5))[::-1]]

    # ── 1) 전환 빈도: 신 vs 구 ──
    new_rates, old_rates = [], []
    legacy = _legacy_quadrant_series(weekly_closes)
    for slug, xy in xy_map.items():
        quads_new = pd.Series([_quadrant(r.x, r.y)[0] for _, r in xy.iterrows()], index=xy.index)
        new_rates.append(_transition_rate(quads_new))
        if slug in legacy and len(legacy[slug]) >= 2:
            old_rates.append(_transition_rate(legacy[slug]))
    avg_new = sum(new_rates) / len(new_rates)
    avg_old = sum(old_rates) / len(old_rates) if old_rates else float("nan")

    # ── 2) 부상→주도 진입 이벤트의 forward 상대수익률 ──
    horizons = (4, 8, 12)
    events = []
    for slug, xy in xy_map.items():
        quads = pd.Series([_quadrant(r.x, r.y)[0] for _, r in xy.iterrows()], index=xy.index)
        for i in range(1, len(quads)):
            if quads.iloc[i] == "leading" and quads.iloc[i - 1] == "improving":
                t = quads.index[i]
                if t not in weekly_closes.index:
                    continue
                ti = weekly_closes.index.get_loc(t)
                row = {"slug": slug, "date": t.strftime("%Y-%m-%d")}
                for h in horizons:
                    if ti + h < len(weekly_closes):
                        sec_r = weekly_closes[slug].iloc[ti + h] / weekly_closes[slug].iloc[ti] - 1
                        bm = benchmark.reindex(weekly_closes.index)
                        bm_r = bm.iloc[ti + h] / bm.iloc[ti] - 1
                        row[f"fwd{h}w"] = (sec_r - bm_r) * 100
                events.append(row)

    lines = [
        "# RRG 리플레이 검증 리포트 (Phase 4)",
        f"\n- 실행일: {dt.date.today().isoformat()} / 데이터: {len(daily)}개 섹터, "
        f"주간 {len(weekly_closes)}주 (cutoff {cutoff})",
        f"- 이상치 플래그: {data_flags if data_flags else '없음'}",
        "\n## 1. 사분면 전환 빈도 (10주당 평균 전환 횟수, 낮을수록 안정)",
        f"- 신 방식(주간 RRG): **{avg_new:.2f}회**",
        f"- 구 방식(수익률 백분위, 주간 등가 재현): **{avg_old:.2f}회**",
        "\n## 2. 부상→주도 진입 후 forward 상대수익률 (vs 동일가중 벤치마크, %p)",
        f"- 이벤트 수: {len(events)}건",
    ]
    for h in horizons:
        vals = [e[f"fwd{h}w"] for e in events if f"fwd{h}w" in e]
        if vals:
            mean = sum(vals) / len(vals)
            win = 100 * sum(1 for v in vals if v > 0) / len(vals)
            lines.append(f"- +{h}주: 평균 {mean:+.2f}%p, 승률 {win:.0f}% (n={len(vals)})")
        else:
            lines.append(f"- +{h}주: 표본 없음")
    lines.append("\n## 이벤트 상세")
    for e in events:
        fwd = ", ".join(f"+{h}w {e.get(f'fwd{h}w', float('nan')):+.1f}%p"
                        for h in horizons if f"fwd{h}w" in e)
        lines.append(f"- {e['date']} {e['slug']}: {fwd or '(forward 구간 부족)'}")
    lines.append("\n## 게이트 판정 기준")
    lines.append("- forward 상대수익률이 유의미하게 양(+)이고 승률 > 50%면 v2에서 "
                 "매매어 코멘트 해금 검토. 아니면 상태 서술 유지 (계획안 Phase 4).")

    report = "\n".join(lines)
    print("\n" + report)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\n저장: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
