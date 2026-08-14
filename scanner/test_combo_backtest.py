"""전략 조합 백테스트 순수 로직 테스트 (네트워크 없음 — 덤프 의존부는 주입).
실행: python scanner/test_combo_backtest.py
"""
import numpy as np
import pandas as pd

import combo_backtest as cx
import newhigh_fetcher as nf


# ── 눌림목: 합성 패널로 하드필터 + 6점 비트 검증 ──
def _pullback_fixture():
    """A = 교과서적 눌림목(급등 → 20% 눌림 → 저거래량 횡보), B = 무추세 대조군."""
    n = 400
    dates = pd.bdate_range("2024-01-02", periods=n)
    a = [100 * 1.015 ** i for i in range(365)]              # 급등 (일 1.5%)
    peak = a[-1]
    a += [peak * (1 - 0.02 * (i + 1)) for i in range(10)]   # 10일간 -20% 눌림 (day 365~374)
    a += [peak * 0.80] * (n - 375)                          # 이후 25일 횡보 (베이스)
    # vcp가 성립하려면 '직전 20일(눌림 포함, 변동 큼) vs 최근 20일(횡보, 변동 0)' 대비가 필요
    # — 횡보 꼬리를 너무 길게 두면 비교 구간도 평탄해져 vcp·vol_dry가 빠진다
    closes = pd.DataFrame({"000010": a, "000020": [100.0] * n}, index=dates)
    vol = pd.DataFrame({"000010": [1e6] * 375 + [1e5] * (n - 375),
                        "000020": [1e6] * n}, index=dates)
    big = pd.DataFrame(1e12, index=dates, columns=closes.columns)       # 시총 1조 (게이트 통과)
    ok = pd.DataFrame(True, index=dates, columns=closes.columns)
    mkt = {"000010": "KOSPI", "000020": "KOSPI"}
    return closes, vol, big, ok, mkt


def test_pullback_textbook_setup_scores_six():
    closes, vol, big, ok, mkt = _pullback_fixture()
    hard, score, rs = cx.pullback_panels(closes, vol, cap_est=big, price_ok=ok, mkt=mkt)
    last = closes.index[-1]
    # A: prox 0.80(딥 눌림 밴드 내), 상승률·RS 충족 → 하드필터 통과 + 6점 만점
    assert bool(hard.loc[last, "000010"]), (hard.loc[last], score.loc[last])
    assert int(score.loc[last, "000010"]) == 6, score.loc[last]
    assert int(rs.loc[last, "000010"]) == 99 and int(rs.loc[last, "000020"]) == 1
    # B: 무추세 — pct6/pct12 미달로 하드필터 탈락
    assert not bool(hard.loc[last, "000020"])


def test_pullback_prox_band_excludes_fresh_high():
    """신고가 부근(prox>0.92)은 눌림목이 아니다 — 급등 직후(눌림 전) 시점은 탈락."""
    closes, vol, big, ok, mkt = _pullback_fixture()
    hard, _, _ = cx.pullback_panels(closes, vol, cap_est=big, price_ok=ok, mkt=mkt)
    at_peak = closes.index[364]
    assert not bool(hard.loc[at_peak, "000010"])


# ── 신고가 후보: 합성 marcap 프레임으로 편입 게이트 + carry 검증 ──
def _newhigh_fixture():
    n = 400
    dates = pd.bdate_range("2024-01-02", periods=n)
    px = [50 + i * 50 / 349 for i in range(350)]     # 350일간 50→100 우상향 (매일 신고가권)
    px += [100 / 1.15] * 4                           # gap ≈ 15% (편입 상한 12% 초과, carry 상한 20% 이내)
    px += [100 / 1.30] * (n - 354)                   # gap ≈ 30% — carry 상한도 초과
    rows = []
    prev = px[0]
    for d, c in zip(dates, px):
        # Changes = 전일 대비 변화 — 0으로 두면 add_adjusted가 가격 변동 전체를
        # 수정계수(분할)로 해석해 수정주가가 평탄해진다 (marcap 실데이터 스키마와 동일하게)
        rows.append({"Code": "111110", "Name": "테스트", "Date": d, "High": c, "Low": c,
                     "Close": c, "Changes": c - prev, "Volume": 1e6, "Amount": 60e8,
                     "Marcap": 2_500e8, "Market": "KOSPI"})
        prev = c
    df = pd.DataFrame(rows)
    return nf.add_adjusted(df), dates


def test_newhigh_member_carry_then_drop():
    df, dates = _newhigh_fixture()
    member, gap, rs = cx.newhigh_panels(df)
    c = "111110"
    assert bool(member.loc[dates[349], c]), "신고가 갱신일은 멤버여야 한다"
    assert bool(member.loc[dates[352], c]), "gap 15%라도 carry 5일 이내면 멤버 유지"
    assert not bool(member.loc[dates[349 + 6], c]), "carry 5일 경과 후에는 탈락"
    assert not bool(member.loc[dates[360], c]), "gap 30%는 carry 상한(20%) 초과 — 탈락"
    assert float(rs.loc[dates[349], c]) >= nf.MIN_RS


def test_newhigh_liquidity_gate():
    """당일 거래대금 50억 미달 종목은 신고가여도 편입 불가."""
    df, dates = _newhigh_fixture()
    df.loc[df["Date"] == dates[349], "Amount"] = 10e8
    member, _, _ = cx.newhigh_panels(df)
    assert not bool(member.loc[dates[349], "111110"])


# ── 창 필터·조립 ──
def test_window_rows_min_ago_wins():
    dates = pd.bdate_range("2025-01-02", periods=50)
    mask = pd.DataFrame(False, index=dates, columns=["000010"])
    mask.iloc[30] = True   # 신호일(마지막 날)로부터 ago=19
    mask.iloc[45] = True   # ago=4 — 더 최근이 이겨야 한다
    score = pd.DataFrame(7.0, index=dates, columns=["000010"])
    rebals = [(dates[-1], dates[-1] + pd.Timedelta(days=3))]
    rows = cx._window_rows(mask, score, rebals)
    sig = str(dates[-1].date())
    assert rows[sig]["000010"][0] == 4, rows


def test_sig_dict_monthly_prev_needs_wide_window():
    mj = {"kind": "monthly", "sigs": {
        "2025-01-31": {"m": {"000010": [0, 1.0]}},
        "2025-02-28": {"m": {"000020": [0, 2.0]}},
    }}
    keys = ["2025-01-31", "2025-02-28"]
    assert set(cx._sig_dict(mj, keys, 1, 20, "m")) == {"000020"}
    wide = cx._sig_dict(mj, keys, 1, 40, "m")
    assert set(wide) == {"000010", "000020"}
    assert wide["000010"][0] == cx.MONTHLY_PREV_AGO


def test_intersection_and_gate_ranker():
    def monthly(codes_scores):
        return {"kind": "monthly", "window_max": 40,
                "sigs": {"2025-01-31": {"m": {c: [0, s] for c, s in codes_scores.items()},
                                        "p": {c: [0, s] for c, s in codes_scores.items()}}}}
    members = {
        "quality": monthly({"A": 1.0, "B": 0.5}),
        "canslim": monthly({"B": 6000, "C": 5000}),
        "pullback": monthly({"B": 5080, "D": 4070}),
        "newhigh": monthly({"E": 99000}),
        "rs_only": monthly({"A": 99}),
    }
    defs = cx.portfolio_defs(members, ["2025-01-31"], 20)
    assert defs["x_QC"][0](0) == ["B"], "퀄리티∩CANSLIM = B"
    assert defs["x_QN"][0](0) == [], "겹침 없으면 공집합"
    # gate×ranker: 퀄리티 멤버(A·B)를 CANSLIM 풀로 정렬 — A는 풀에 없어 탈락, B만
    assert defs["g_QrC"][0](0) == ["B"]
    # blend2: 2개 이상 풀 커버 = B(3개)·나머지 1개씩 → B만
    assert defs["blend2"][0](0) == ["B"]
    assert defs["blend3"][0](0) == ["B"]


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"=== {len(fns)}개 테스트 통과 ===")


if __name__ == "__main__":
    main()
