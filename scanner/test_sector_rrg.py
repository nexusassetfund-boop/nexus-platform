"""sector_rrg 단위 테스트 — 합성 데이터로 수학·멱등성·이상치 처리 검증."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

import sector_rrg as sr


def _daily_series(days=420, daily_ret=0.0, start="2025-06-02", seed=None):
    idx = pd.bdate_range(start=start, periods=days)
    if seed is not None:
        rng = np.random.default_rng(seed)
        rets = rng.normal(daily_ret, 0.01, days)
    else:
        rets = np.full(days, daily_ret)
    return pd.Series(100.0 * np.cumprod(1 + rets), index=idx)


NOW = dt.datetime(2027, 1, 18, 17, 0)  # 월요일 — 직전 금요일 = 2027-01-15


def test_last_confirmed_friday_monday():
    assert sr.last_confirmed_friday(NOW) == dt.date(2027, 1, 15)


def test_last_confirmed_friday_friday_before_close():
    now = dt.datetime(2027, 1, 15, 10, 0)  # 금요일 장중 → 이번 주 미확정
    assert sr.last_confirmed_friday(now) == dt.date(2027, 1, 8)


def test_last_confirmed_friday_friday_after_close():
    now = dt.datetime(2027, 1, 15, 16, 0)
    assert sr.last_confirmed_friday(now) == dt.date(2027, 1, 15)


def test_clean_daily_flags_outlier():
    s = _daily_series(60)
    s.iloc[30] = s.iloc[29] * 0.5  # -50% 봉 (수정주가 오류 시뮬레이션)
    cleaned, flags = sr.clean_daily(s)
    assert len(flags) >= 1
    assert s.index[30].strftime("%Y-%m-%d") in flags
    # 이상치 봉이 직전 종가로 대체됨
    assert cleaned.iloc[30] == pytest.approx(cleaned.iloc[29])


def test_clean_daily_no_false_positive():
    _, flags = sr.clean_daily(_daily_series(200, daily_ret=0.005))
    assert flags == []


def _universe(n=6, strong=0, noise=False):
    """n개 섹터, strong번째만 추세 강세. noise=False면 결정론적(사분면 단언 가능)."""
    out = {}
    for i in range(n):
        ret = 0.004 if i == strong else 0.0002
        out[f"s{i}"] = _daily_series(daily_ret=ret, seed=i if noise else None)
    return out


def test_compute_rrg_basic():
    res = sr.compute_rrg(_universe(), NOW)
    assert res["as_of"] == "2027-01-15"
    assert len(res["sectors"]) == 6
    s0 = res["sectors"]["s0"]
    assert len(s0["tail"]) == sr.TAIL_WEEKS
    assert s0["tail"][-1]["x"] == s0["x"]
    # 지속적 강세 섹터는 벤치마크 대비 RS-Ratio > 100 (주도/약화 어느 쪽이든 우측)
    assert s0["x"] > 100
    assert s0["quadrant"] in ("leading", "weakening")
    # 약세 섹터는 좌측
    assert res["sectors"]["s3"]["x"] < 100
    # 코멘트에 판단어 없음
    for sec in res["sectors"].values():
        for word in ("매수", "매도", "관심", "비중"):
            assert word not in sec["comment"]


def test_compute_rrg_idempotent_intraday():
    """장중 어느 시각에 다시 계산해도 (같은 일봉 데이터면) 좌표 동일 — 주 1회 갱신 보장."""
    uni = _universe()
    r1 = sr.compute_rrg(uni, dt.datetime(2027, 1, 18, 10, 0))
    r2 = sr.compute_rrg(uni, dt.datetime(2027, 1, 21, 15, 0))
    assert r1["as_of"] == r2["as_of"]
    assert r1["sectors"]["s0"]["x"] == r2["sectors"]["s0"]["x"]
    assert r1["sectors"]["s0"]["tail"] == r2["sectors"]["s0"]["tail"]


def test_compute_rrg_outlier_does_not_move_coords():
    """이상치 한 봉이 좌표를 크게 흔들지 않아야 함 (Phase 0 필터의 목적)."""
    uni_clean = _universe(noise=True)
    uni_bad = {k: v.copy() for k, v in uni_clean.items()}
    uni_bad["s1"].iloc[-30] *= 0.5  # -50% 이상치 주입
    r_clean = sr.compute_rrg(uni_clean, NOW)
    r_bad = sr.compute_rrg(uni_bad, NOW)
    assert "s1" in r_bad["data_flags"]
    # 필터 덕에 이상치 주입 전후 좌표 차이가 미미해야 함
    assert abs(r_clean["sectors"]["s1"]["x"] - r_bad["sectors"]["s1"]["x"]) < 1.0


def test_compute_rrg_insufficient_data():
    uni = {f"s{i}": _daily_series(days=60, seed=i) for i in range(6)}  # ≈12주 < MIN_WEEKS
    res = sr.compute_rrg(uni, NOW)
    assert res["sectors"] == {}


def test_build_insight():
    rrg = sr.compute_rrg(_universe(), NOW)
    names = {k: f"섹터{k[-1]}" for k in rrg["sectors"]}
    ins = sr.build_insight(rrg, names, "2027-01-18T15:40:00+09:00")
    assert ins["as_of"] == rrg["as_of"]
    assert 1 <= len(ins["lines"]) <= 6
    assert "주도 사분면" in ins["lines"][0]
    joined = " ".join(ins["lines"])
    for word in ("매수", "매도", "비중"):  # 판단어 금지 (Phase 4 게이트 미통과)
        assert word not in joined
    # 이름 매핑이 적용됐는지 (슬러그 원문이 그대로 노출되지 않아야 함)
    assert "s0" not in joined.replace("섹터0", "")


def test_quadrant_labels():
    assert sr._quadrant(101, 101) == ("leading", "주도")
    assert sr._quadrant(101, 99) == ("weakening", "약화")
    assert sr._quadrant(99, 99) == ("lagging", "소외")
    assert sr._quadrant(99, 101) == ("improving", "부상")
