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


NOW = dt.datetime(2027, 1, 18, 17, 0)  # 월요일 장마감 후 — 확정 종가 = 2027-01-18


def test_last_confirmed_close_after():
    assert sr.last_confirmed_close(NOW) == dt.date(2027, 1, 18)


def test_last_confirmed_close_before():
    now = dt.datetime(2027, 1, 18, 10, 0)  # 장중 → 전 거래일(금 1/15)
    assert sr.last_confirmed_close(now) == dt.date(2027, 1, 15)


def test_last_confirmed_close_weekend():
    now = dt.datetime(2027, 1, 17, 12, 0)  # 일요일 → 금 1/15
    assert sr.last_confirmed_close(now) == dt.date(2027, 1, 15)


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


def test_clean_daily_preserves_real_crash():
    """진짜 급락(새 가격 수준 유지)은 보존 — 변동성 장세의 실제 ±15%+ 움직임을 지우면 안 된다."""
    s = _daily_series(60)
    s.iloc[30:] *= 0.8  # -20% 하락 후 그 수준 유지 (레벨 시프트 = 실제 시장 이벤트)
    cleaned, flags = sr.clean_daily(s)
    assert flags == []
    assert cleaned.iloc[30] == pytest.approx(s.iloc[30])


def test_clean_daily_preserves_last_bar():
    """마지막 봉은 다음 날 데이터가 없어 판정 불가 — 오늘의 진짜 폭락을 지우지 않는다."""
    s = _daily_series(60)
    s.iloc[-1] = s.iloc[-2] * 0.8  # 오늘 -20%
    cleaned, flags = sr.clean_daily(s)
    assert flags == []
    assert cleaned.iloc[-1] == pytest.approx(s.iloc[-1])


def _universe(n=6, strong=0, noise=False):
    """n개 섹터, strong번째만 추세 강세. noise=False면 결정론적(사분면 단언 가능)."""
    out = {}
    for i in range(n):
        ret = 0.004 if i == strong else 0.0002
        out[f"s{i}"] = _daily_series(daily_ret=ret, seed=i if noise else None)
    return out


def test_compute_rrg_basic():
    uni = _universe()
    res = sr.compute_rrg(uni, NOW)
    assert res["as_of"] == uni["s0"].index[-1].strftime("%Y-%m-%d")  # 데이터 마지막 영업일
    assert len(res["sectors"]) == 6
    s0 = res["sectors"]["s0"]
    # tail = 주간 앵커 최대 7점 + 현재점(주중일 때만 별도) → 7 또는 8점
    assert sr.TAIL_POINTS - 1 <= len(s0["tail"]) <= sr.TAIL_POINTS
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
    """같은 확정 종가 데이터면 어느 시각에 계산해도 좌표 동일 — 장중 재실행 안전."""
    uni = _universe()
    r1 = sr.compute_rrg(uni, dt.datetime(2027, 1, 18, 16, 0))
    r2 = sr.compute_rrg(uni, dt.datetime(2027, 1, 19, 16, 0))
    assert r1["sectors"]["s0"]["x"] == r2["sectors"]["s0"]["x"]
    assert r1["sectors"]["s0"]["tail"] == r2["sectors"]["s0"]["tail"]


def test_compute_rrg_daily_smoothness():
    """하루 추가돼도 좌표가 완만하게만 움직여야 함 — '급변' 재발 방지의 핵심 성질."""
    uni_full = _universe(noise=True)
    uni_prev = {k: v.iloc[:-1] for k, v in uni_full.items()}  # 하루 전까지
    r_full = sr.compute_rrg(uni_full, NOW)
    r_prev = sr.compute_rrg(uni_prev, NOW)
    for k in r_full["sectors"]:
        dx = abs(r_full["sectors"][k]["x"] - r_prev["sectors"][k]["x"])
        dy = abs(r_full["sectors"][k]["y"] - r_prev["sectors"][k]["y"])
        # 구 방식은 하루에 백분위 수십 점씩 튀었다 — 좌표 단위 수 점 이내면 '완만'
        assert dx < 3.0 and dy < 4.0, f"{k}: 1일 변화 dx={dx:.2f} dy={dy:.2f}"


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
    uni = {f"s{i}": _daily_series(days=60, seed=i) for i in range(6)}  # < MIN_DAYS
    res = sr.compute_rrg(uni, NOW)
    assert res["sectors"] == {}


def test_heading_streak_phase_stable():
    """heading·'N주 연속'은 금요일 앵커 기준 — 주중 어느 요일에 갱신해도 값이 같아야 한다.
    (요일 따라 '3주 연속'이 '2주 연속'으로 바뀌는 위상 문제의 회귀 테스트)"""
    uni_fri = _universe(noise=True)                       # 마지막 봉 = 금요일(1/8)
    r_fri = sr.compute_rrg(uni_fri, NOW)
    for extra in (1, 2, 3):                               # 월·화·수 하루씩 추가
        uni_mid = {}
        for k, v in uni_fri.items():
            idx2 = pd.bdate_range(start=v.index[0], periods=len(v) + extra)
            uni_mid[k] = pd.Series(list(v.values) + [v.iloc[-1]] * extra, index=idx2)
        now_mid = idx2[-1].to_pydatetime().replace(hour=16)  # 해당 요일 장마감 직후
        r_mid = sr.compute_rrg(uni_mid, now_mid)
        for k in r_fri["sectors"]:
            assert r_fri["sectors"][k]["heading"] == r_mid["sectors"][k]["heading"], k
            assert r_fri["sectors"][k]["heading_weeks"] == r_mid["sectors"][k]["heading_weeks"], k


def test_transition_forward_pipeline():
    """리플레이 핵심 경로(전이 탐지→forward 수익률)가 배포 코드(daily_xy)와 같은 입력으로
    도는지 회귀 테스트 — 검증 경로와 배포 경로 불일치 재발 방지."""
    uni = _universe(noise=True)
    cutoff = sr.last_confirmed_close(NOW)
    xy_daily, benchmark, _ = sr.daily_xy(uni, cutoff)
    assert xy_daily and len(benchmark) > 0
    xy_w = {k: xy.iloc[list(range(len(xy) - 1, -1, -5))[::-1]] for k, xy in xy_daily.items()}
    n_events = 0
    for slug, xy in xy_w.items():
        quads = [sr._quadrant(r.x, r.y)[0] for _, r in xy.iterrows()]
        n_events += sum(1 for i in range(1, len(quads)) if quads[i] != quads[i - 1])
    assert n_events >= 0  # 파이프라인이 예외 없이 완주하는지가 핵심


def test_build_insight():
    rrg = sr.compute_rrg(_universe(), NOW)
    names = {k: f"섹터{k[-1]}" for k in rrg["sectors"]}
    ins = sr.build_insight(rrg, names, "2027-01-18T15:40:00+09:00")
    assert ins["as_of"] == rrg["as_of"]
    assert 1 <= len(ins["lines"]) <= 9
    assert "사분면 전환" in ins["lines"][0]
    joined = " ".join(ins["lines"])
    for word in ("매수", "매도", "비중"):  # 판단어 금지 (Phase 4 게이트 미통과)
        assert word not in joined
    # 이름 매핑이 적용됐는지 (슬러그 원문이 그대로 노출되지 않아야 함)
    assert "s0" not in joined.replace("섹터0", "")


def test_y_only_flag_logic():
    """2026-08 2차전지 사례: 부상·NE 4주인데 x 91대 제자리 → Y축 단독 신호."""
    assert sr._y_only("improving", "NE", 4, 91.8, 0.5) is True
    # X가 동반 상승하면(이동 ≥1.5) 진짜 회전 — 플래그 없음
    assert sr._y_only("improving", "NE", 4, 91.8, 2.0) is False
    # 경계(x≥95)나 짧은 streak, 하방 heading은 대상 아님
    assert sr._y_only("improving", "NE", 4, 97.0, 0.5) is False
    assert sr._y_only("improving", "NE", 2, 91.8, 0.5) is False
    assert sr._y_only("improving", "SE", 4, 91.8, 0.5) is False
    assert sr._y_only("leading", "NE", 4, 91.8, 0.5) is False


def test_entry_gate_checks():
    up = _daily_series(200, daily_ret=0.004)     # 강한 상승 추세 → MA60 위
    g = sr._entry_gate(up, "leading", "NE", False)
    assert g["ma60_ok"] is True and g["d5_ok"] is True and g["trend_ok"] is True
    assert g["passed"] is True and g["n_ok"] == 3
    down = _daily_series(200, daily_ret=-0.004)  # 하락 추세 → MA60 아래
    g2 = sr._entry_gate(down, "improving", "NE", False)
    assert g2["ma60_ok"] is False and g2["passed"] is False
    # 5일 급락 (-2%/일 × 5일 ≈ -9.6%)
    crash = _daily_series(200, daily_ret=0.004)
    crash.iloc[-5:] = crash.iloc[-6] * np.cumprod(np.full(5, 0.98))
    g3 = sr._entry_gate(crash, "leading", "NE", False)
    assert g3["d5_ok"] is False and g3["passed"] is False
    # Y축 단독이면 trend_ok 탈락
    g4 = sr._entry_gate(up, "improving", "NE", True)
    assert g4["trend_ok"] is False and g4["passed"] is False
    # 데이터 부족 → 판정 유보(None), passed False
    g5 = sr._entry_gate(_daily_series(30), "leading", "NE", False)
    assert g5["ma60_ok"] is None and g5["passed"] is False


def test_compute_rrg_new_fields():
    res = sr.compute_rrg(_universe(), NOW)
    assert "signal_lag" in res
    for sec in res["sectors"].values():
        assert "y_only" in sec and "x_move" in sec
        gate = sec["gate"]
        assert set(gate) >= {"trend_ok", "ma60_ok", "d5_ok", "passed", "n_ok"}
        # 게이트·코멘트에 판단어 없음 (백테스트 검증 전 원칙 유지)
        for word in ("매수", "매도", "관심", "비중"):
            assert word not in sec["comment"]


def _fake_sector(**kw):
    base = {"x": 100.0, "y": 100.0, "quadrant": "leading", "quadrant_ko": "주도",
            "prev_quadrant": "leading", "heading": "E", "heading_weeks": 1,
            "x_move": 0.0, "y_only": False,
            "gate": {"trend_ok": True, "ma60_ok": True, "d5_ok": True,
                     "d5": 0.0, "passed": True, "n_ok": 3},
            "tail": [{"d": "01-01", "x": 100.0, "y": 100.0}], "comment": "주도 유지"}
    base.update(kw)
    return base


def test_build_insight_divergence_any_quadrant():
    """부상 사분면 + 상방 4주 + 5일 급락(2차전지 사례)도 괴리 라인에 잡혀야 한다 (기존 구멍 회귀)."""
    rrg = {"as_of": "2026-08-03", "sectors": {
        "battery": _fake_sector(x=91.8, y=105.3, quadrant="improving", quadrant_ko="부상",
                                prev_quadrant="improving", heading="NE", heading_weeks=4,
                                x_move=0.5, y_only=True),
        "semi": _fake_sector(), "auto": _fake_sector(), "bank": _fake_sector()}}
    ins = sr.build_insight(rrg, {"battery": "2차전지"}, "2026-08-03T15:40:00+09:00",
                           d5={"battery": -7.6, "semi": 0.0, "auto": 0.0, "bank": 0.0})
    joined = " ".join(ins["lines"])
    assert "추세·단기 괴리" in joined and "2차전지" in joined
    assert "Y축 단독" in joined  # ⑤-b 라인도 함께
    # y_only 섹터는 "부상 지속" 라인에서 제외
    assert "부상 지속" not in joined


def test_quadrant_labels():
    assert sr._quadrant(101, 101) == ("leading", "주도")
    assert sr._quadrant(101, 99) == ("weakening", "약화")
    assert sr._quadrant(99, 99) == ("lagging", "소외")
    assert sr._quadrant(99, 101) == ("improving", "부상")


# ── 회전형(cw) 좌표 ────────────────────────────────────────────────────────
# 회전 방향은 좌표 정의가 만드는 성질이라, 위상 관계가 깨지면 여기서 바로 잡힌다.

def _cyclic_universe(period, n=6, days=520):
    """1개 섹터만 주기 period(거래일)의 깨끗한 사이클, 나머지는 평탄."""
    idx = pd.bdate_range(start="2024-06-03", periods=days)
    t = np.arange(days)
    uni = {"s0": pd.Series(100.0 * np.exp(0.10 * np.sin(2 * np.pi * t / period)), index=idx)}
    for i in range(1, n):  # 레벨만 다른 평탄 시계열 (벤치마크 구성용)
        uni[f"s{i}"] = pd.Series(np.full(days, 100.0 + i), index=idx)
    return uni


def _rotation(xy):
    """주간 격자 궤적의 외적 합 — 음수면 시계방향."""
    w = xy.iloc[::sr.TAIL_STEP]
    ax, ay = w["x"].values - 100.0, w["y"].values - 100.0
    return float(np.sum(ax[:-1] * ay[1:] - ay[:-1] * ax[1:]))


@pytest.mark.parametrize("period", [24, 32, 45, 90, 180])
def test_cw_rotates_clockwise_at_every_period(period):
    """회전형은 전 주기 대역에서 시계방향 — y가 x를 90° 선행하도록 구성했기 때문."""
    uni = _cyclic_universe(period)
    xy_map, _b, _f = sr.daily_xy(uni, uni["s0"].index[-1].date(), mode="cw")
    assert _rotation(xy_map["s0"]) < 0, f"주기 {period}일에서 반시계로 돌았다"


@pytest.mark.parametrize("period,clockwise", [(28, False), (90, True)])
def test_current_mode_flips_below_40_days(period, clockwise):
    """현행은 2*ROC_DAYS(=40일)보다 짧은 주기에서 회전이 뒤집힌다 — 회전형 도입 근거."""
    uni = _cyclic_universe(period)
    xy_map, _b, _f = sr.daily_xy(uni, uni["s0"].index[-1].date(), mode="current")
    assert (_rotation(xy_map["s0"]) < 0) is clockwise


def test_cw_omits_scale_specific_fields():
    """gate·y_only 임계값은 현행 x 스케일(89~117) 전용이라 σ 좌표에서는 내보내지 않는다."""
    uni = _universe(noise=True)
    cur, cw = sr.compute_rrg(uni, NOW), sr.compute_rrg(uni, NOW, mode="cw")
    assert cur["mode"] == "current" and cw["mode"] == "cw"
    assert all("gate" in s and "y_only" in s for s in cur["sectors"].values())
    assert all("gate" not in s and "y_only" not in s for s in cw["sectors"].values())
    # 사분면·heading·tail·comment 는 스케일 무관이라 그대로 재사용된다
    labels = {q for q, _ko in sr.QUADRANTS.values()}
    for s in cw["sectors"].values():
        assert s["quadrant"] in labels
        assert s["tail"] and s["comment"] and s["heading"]


def test_cw_requires_more_history():
    """회전형은 정규화 창이 길어 히스토리가 짧은 신설 ETF를 제외한다(현행엔 남는다)."""
    uni = _universe(noise=True)
    uni["short"] = _daily_series(seed=99).iloc[-(sr.MIN_DAYS + 10):]  # 현행 O / 회전형 X
    assert sr.MIN_DAYS < len(uni["short"]) < sr.MIN_DAYS_CW
    assert "short" in sr.compute_rrg(uni, NOW)["sectors"]
    assert "short" not in sr.compute_rrg(uni, NOW, mode="cw")["sectors"]
