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


class _Stub:
    """(year, reprt) → 분기 단독 순이익 스텁으로 _acnt_all_ni 대체. 호출 이력도 기록."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __enter__(self):
        self.orig = cs._acnt_all_ni
        cs._acnt_all_ni = self
        return self

    def __exit__(self, *a):
        cs._acnt_all_ni = self.orig

    def __call__(self, corp, year, reprt):
        self.calls.append((year, reprt))
        return self.table.get((year, reprt))


def test_quarter_yoy_compares_same_quarter_across_years():
    # 최신 제출은 2026 3Q. 전년 동기는 같은 reprt_code의 2025년 값 — 차분 없음.
    with _Stub({(2026, "11014"): 120.0, (2025, "11014"): 60.0}) as s:
        r = cs.quarter_ni_yoy("X", today=dt.date(2026, 11, 20))
    assert r["q_period"] == "2026 3Q", r
    assert r["q_ni"] == 120.0 and r["q_ni_prev"] == 60.0 and r["q_ni_yoy"] == 100.0, r
    # 직전 분기 보고서는 건드리지 않는다 (누적 차분 회귀 방지)
    assert (2026, "11012") not in s.calls, s.calls


def test_quarter_yoy_falls_back_to_older_reprt():
    # 3Q 미제출 → 반기로 내려가고, 그때도 같은 분기끼리 비교
    with _Stub({(2026, "11012"): 50.0, (2025, "11012"): 40.0}):
        r = cs.quarter_ni_yoy("X", today=dt.date(2026, 8, 20))
    assert r["q_period"] == "2026 2Q" and r["q_ni_yoy"] == 25.0, r


def test_quarter_yoy_skips_when_prior_year_missing():
    # 당기만 있고 전년 동기가 없으면 그 분기는 건너뛰고 다음 후보로
    with _Stub({(2026, "11014"): 120.0, (2026, "11012"): 50.0, (2025, "11012"): 40.0}):
        r = cs.quarter_ni_yoy("X", today=dt.date(2026, 11, 20))
    assert r["q_period"] == "2026 2Q", r


def test_quarter_yoy_prev_loss_is_turnaround_not_pct():
    # 전년 동기 적자 → YoY %는 무의미(None), 흑자전환 플래그로만 표기
    with _Stub({(2026, "11013"): 50.0, (2025, "11013"): -40.0}):
        r = cs.quarter_ni_yoy("X", today=dt.date(2026, 5, 20))
    assert r["q_ni_yoy"] is None and r["q_turnaround"] == 1, r
    assert cs.score(_rec(q_ni_yoy=None))[1]["C"] == 0     # 흑전은 C 통과가 아니다


def test_quarter_yoy_matches_real_dart_samsung_3q():
    """실측 회귀 — DART fnlttSinglAcntAll 삼성전자(00126380) 3분기보고서 분기순이익.

    2025 3Q 12,225,747백만 / 2024 3Q 10,100,904백만 → +21.0%.
    같은 보고서의 매출액도 86.1조 vs 79.1조로 실제 분기 실적과 일치 —
    이 값들이 '3분기 누적'이 아니라 '3분기 단독'임을 확인해 준 근거다.
    """
    with _Stub({(2025, "11014"): 12_225_747_000_000.0,
                (2024, "11014"): 10_100_904_000_000.0}):
        r = cs.quarter_ni_yoy("00126380", today=dt.date(2025, 11, 20))
    assert r["q_period"] == "2025 3Q", r
    assert r["q_ni_yoy"] == 21.0, r
    assert cs.score(_rec(q_ni_yoy=r["q_ni_yoy"]))[1]["C"] == 1     # C 임계 20% 통과


def test_quarter_yoy_no_data_returns_empty():
    with _Stub({}):
        assert cs.quarter_ni_yoy("X", today=dt.date(2026, 5, 20)) == {}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
