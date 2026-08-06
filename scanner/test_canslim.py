"""CANSLIM 순수 로직 단위 테스트 (네트워크 없음). 실행: python scanner/test_canslim.py"""
import datetime as dt

import canslim_screener as cs


def _rec(**kw):
    base = {"q_ni_yoy": 30.0, "ni_growth": 30.0, "roe": 20.0,
            "proximity_52w": 0.95, "shares": 10_000_000, "vol_2x_bo": 1,
            "rs_kkangto": 90}
    base.update(kw)
    return base


def test_score_all_pass():
    s, b = cs.score(_rec())
    assert s == 7 and all(b.values()), (s, b)


def test_score_missing_is_fail():
    # 재무 미확보(DART 결측)는 미충족으로 — 통과로 새지 않아야 한다
    s, b = cs.score(_rec(q_ni_yoy=None, ni_growth=None, roe=None))
    assert b["C"] == 0 and b["A_growth"] == 0 and b["A_roe"] == 0, b
    assert s == 4, s


def test_score_boundaries():
    assert cs.score(_rec(q_ni_yoy=cs.C_NI_YOY))[1]["C"] == 1
    assert cs.score(_rec(q_ni_yoy=cs.C_NI_YOY - 0.1))[1]["C"] == 0
    assert cs.score(_rec(proximity_52w=cs.N_PROXIMITY))[1]["N"] == 1
    assert cs.score(_rec(rs_kkangto=cs.L_RS_SCORE - 1))[1]["L"] == 0
    assert cs.score(_rec(shares=cs.S1_SHARES))[1]["S1"] == 1
    assert cs.score(_rec(shares=cs.S1_SHARES + 1))[1]["S1"] == 0
    assert cs.score(_rec(shares=0))[1]["S1"] == 0        # 결측(0)은 미충족


def _stub(table):
    """(year, reprt) → (당기누적, 전년동기누적) 스텁으로 _acnt_q 대체."""
    return lambda corp, year, reprt: table.get((year, reprt), (None, None))


def test_quarter_yoy_decumulates_3q(monkeypatch=None):
    # 2026년 3Q 누적 300(전년 200), 반기 누적 180(전년 140)
    #  → 3분기 단독: 당기 120, 전년 60 → +100%
    orig = cs._acnt_q
    cs._acnt_q = _stub({(2026, "11014"): (300.0, 200.0), (2026, "11012"): (180.0, 140.0)})
    try:
        r = cs.quarter_ni_yoy("X", today=dt.date(2026, 11, 20))
    finally:
        cs._acnt_q = orig
    assert r["q_period"] == "2026-11014", r
    assert r["q_ni"] == 120.0 and r["q_ni_prev"] == 60.0, r
    assert r["q_ni_yoy"] == 100.0, r


def test_quarter_yoy_1q_uses_cumulative_directly():
    orig = cs._acnt_q
    cs._acnt_q = _stub({(2026, "11013"): (50.0, 40.0)})
    try:
        r = cs.quarter_ni_yoy("X", today=dt.date(2026, 5, 20))
    finally:
        cs._acnt_q = orig
    assert r["q_ni"] == 50.0 and r["q_ni_prev"] == 40.0 and r["q_ni_yoy"] == 25.0, r


def test_quarter_yoy_prev_loss_is_turnaround_not_pct():
    # 전년 동기 적자 → YoY %는 무의미(None), 흑자전환 플래그로만 표기
    orig = cs._acnt_q
    cs._acnt_q = _stub({(2026, "11013"): (50.0, -40.0)})
    try:
        r = cs.quarter_ni_yoy("X", today=dt.date(2026, 5, 20))
    finally:
        cs._acnt_q = orig
    assert r["q_ni_yoy"] is None and r["q_turnaround"] == 1, r
    assert cs.score(_rec(q_ni_yoy=None))[1]["C"] == 0     # 흑전은 C 통과가 아니다


def test_quarter_yoy_no_data_returns_empty():
    orig = cs._acnt_q
    cs._acnt_q = _stub({})
    try:
        assert cs.quarter_ni_yoy("X", today=dt.date(2026, 5, 20)) == {}
    finally:
        cs._acnt_q = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
