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


def test_score_threshold_override_is_backtest_only():
    """임계치 주입은 백테스트 전용 — 기본 호출(라이브)은 동작이 변하지 않아야 한다."""
    assert cs.score(_rec(roe=15.0))[1]["A_roe"] == 0              # 라이브: 17% 미달
    assert cs.score(_rec(roe=15.0), {"A_roe": 12.0})[1]["A_roe"] == 1   # 완화 주입
    assert cs.score(_rec(), None) == cs.score(_rec())             # None = 기본
    assert cs.score(_rec(), {}) == cs.score(_rec())               # 빈 dict = 기본
    # 일부 키만 줘도 나머지는 기본 임계치를 유지한다
    s, b = cs.score(_rec(roe=15.0, q_ni_yoy=5.0), {"A_roe": 12.0})
    assert b["A_roe"] == 1 and b["C"] == 0, b
    # THRESHOLDS는 모듈 상수와 일치 (한쪽만 고치는 실수 방지)
    assert cs.THRESHOLDS["A_roe"] == cs.A_ROE and cs.THRESHOLDS["L"] == cs.L_RS_SCORE


def test_score_override_does_not_mutate_defaults():
    """주입이 전역 THRESHOLDS를 오염시키면 이후 라이브 스캔이 조용히 틀어진다."""
    before = dict(cs.THRESHOLDS)
    cs.score(_rec(), {"A_roe": 1.0, "L": 1})
    assert cs.THRESHOLDS == before, cs.THRESHOLDS


def test_ni_account_matches_real_name_variants():
    """실측 회귀 — 같은 회사(지엔씨에너지)도 연도마다 계정명이 다르다.

    2026 1Q '당기순이익' / 2025 1Q '당기순이익(손실)'.
    첫 배포에서 정확일치로 잡는 바람에 전년 값을 놓쳐 42종목 중 34종목이 결측됐다.
    """
    for nm in ("당기순이익", "당기순이익(손실)", "분기순이익", "분기순이익(손실)",
               "반기순이익", "반기순이익(손실)", "당기순이익 (손실)"):
        assert cs._is_ni_account(nm), nm


def test_ni_account_rejects_lookalikes():
    """지분 귀속분·현금흐름표 조정 항목은 순이익이 아니다 — 부분일치로 삼키면 안 된다."""
    for nm in ("비지배지분에 귀속되는 당기순이익(손실)",
               "지배기업의 소유주에게 귀속되는 당기순이익(손실)",
               "당기순이익조정을 위한 가감",
               "계속영업당기순이익",
               "총포괄손익", "영업이익(손실)"):
        assert not cs._is_ni_account(nm), nm


def test_i_gate_sign_and_missing():
    """I 관문: (+)만 통과, 0은 미통과, 결측은 판정 보류(None) — 결측을 0으로 보면 표가 빈다."""
    assert cs.i_gate(3033) == 1
    assert cs.i_gate(-12) == 0
    assert cs.i_gate(0) == 0
    assert cs.i_gate(None) is None


def test_i_gate_not_in_score():
    """관문은 7점 채점에 영향을 주지 않아야 한다 — 백테스트 비교 가능성 보존."""
    rec = {"q_ni_yoy": 50, "ni_growth": 50, "roe": 20, "proximity_52w": 0.95,
           "shares": 10_000_000, "vol_2x_bo": 1, "rs_kkangto": 95,
           "inst_frgn_net_억": -500}
    sc, bits = cs.score(rec)
    assert sc == 7 and "I" not in bits, (sc, bits)


def test_quarter_yoy_no_data_returns_empty():
    with _Stub({}):
        assert cs.quarter_ni_yoy("X", today=dt.date(2026, 5, 20)) == {}


def _member(code, rank, score=6, name=None):
    return {"ticker": code, "name": name or f"종목{code}", "rank": rank,
            "canslim_score": score}


def test_track_diff_and_deltas(tmp=None):
    """편입/편출 diff · 순위/score Δ · 최초 실행 시드 · 비확정 동결 (파일 I/O는 tmp로 격리)"""
    import json
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    orig = (cs.STATE_PATH, cs.HISTORY_PATH)
    cs.STATE_PATH, cs.HISTORY_PATH = d / "s.json", d / "h.json"
    try:
        n1 = dt.datetime(2026, 8, 7, 16, 40, tzinfo=cs.KST)
        n2 = dt.datetime(2026, 8, 8, 16, 40, tzinfo=cs.KST)
        day1 = [_member("A", 1), _member("B", 2), _member("C", 3, score=3)]  # C는 멤버 아님
        hist, st, ok = cs._track(day1, "2026-08-07", n1)
        assert ok, "마감 후 + 스냅샷 당일이면 확정"
        assert set(st) == {"A", "B"} and hist == [], (st, hist)
        cs.STATE_PATH.write_text(json.dumps(st), encoding="utf-8")

        # 다음 날: A 순위 하락 + score 상승, B 편출(score 붕괴), D 신규 편입
        day2 = [_member("A", 4, score=7), _member("B", 9, score=2), _member("D", 1)]
        hist, st, ok = cs._track(day2, "2026-08-08", n2)
        a = day2[0]
        assert a["rank_delta"] == -3 and a["score_delta"] == 1, a
        assert a["days_in_list"] == 1 and a["is_new"] == 0, a
        assert day2[2]["is_new"] == 1, day2[2]
        assert set(st) == {"A", "D"}, st
        assert [x["code"] for x in hist[-1]["added"]] == ["D"], hist
        assert hist[-1]["removed"][0]["code"] == "B" and hist[-1]["removed"][0]["days"] == 1, hist

        # 같은 날 재실행 → 행 병합 (멱등)
        cs.STATE_PATH.write_text(json.dumps(st), encoding="utf-8")
        hist, st, ok = cs._track(day2, "2026-08-08", n2)
        assert len(hist) == 1, hist
        # 장중 실행(마감 전)은 이력·상태 동결
        assert cs._track(day2, "2026-08-08", n2.replace(hour=11))[2] is False
    finally:
        cs.STATE_PATH, cs.HISTORY_PATH = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
