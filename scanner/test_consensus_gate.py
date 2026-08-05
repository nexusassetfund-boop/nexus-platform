# -*- coding: utf-8 -*-
"""컨센서스 타당성 게이트 자체검증.

급성장(정상)은 통과시키고 오염은 계속 걸러내는지 — 실제 관측값 기반.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from earnings_calendar import _consensus_sane, _last_actual_quarter  # noqa: E402

A = False  # consensus=False → 실적
E = True   # consensus=True  → 추정

# 삼성전자 실측 (2026-08-05 네이버, 2026Q1은 DART 정기보고서와 일치 확인)
SAMSUNG = {
    "2025Q1": {"revenue": 791405.0, "op": 66853.0, "np": 82229.0, "consensus": A},
    "2025Q2": {"revenue": 745663.0, "op": 46761.0, "np": 51164.0, "consensus": A},
    "2025Q3": {"revenue": 860617.0, "op": 121661.0, "np": 122257.0, "consensus": A},
    "2025Q4": {"revenue": 938374.0, "op": 200737.0, "np": 196417.0, "consensus": A},
    "2026Q1": {"revenue": 1338734.0, "op": 572328.0, "np": 472253.0, "consensus": A},
    "2026Q2": {"revenue": 1738644.0, "op": 850494.0, "np": 734933.0, "consensus": E},
}


def test():
    # 직전 실적 분기 탐색
    assert _last_actual_quarter(SAMSUNG, 2026, 2) is SAMSUNG["2026Q1"]
    assert _last_actual_quarter(SAMSUNG, 2026, 1) is SAMSUNG["2025Q4"]
    assert _last_actual_quarter({}, 2026, 2) is None

    # 진짜 급성장은 통과해야 한다 (YoY 2.33배지만 직전 분기 대비 1.30배)
    cons = {k: SAMSUNG["2026Q2"][k] for k in ("revenue", "op", "np")}
    assert _consensus_sane(cons, SAMSUNG, 2026, 2), "실제 급성장이 폐기되면 안 된다"

    # 형상 오염(지주사 402340형: op ≫ rev)은 계속 걸러야 한다
    assert not _consensus_sane({"revenue": 3650.0, "op": 99084.0, "np": 0.0}, {}, 2026, 2)

    # 스케일 오염 — 직전 실적 대비 3배 매출은 폐기
    assert not _consensus_sane({"revenue": 1338734.0 * 3, "op": 572328.0, "np": 0.0},
                               SAMSUNG, 2026, 2)
    # 직전 실적 대비 4배 영업이익도 폐기
    assert not _consensus_sane({"revenue": 1500000.0, "op": 572328.0 * 4, "np": 0.0},
                               SAMSUNG, 2026, 2)

    # 직전 실적이 없으면 기존 YoY 기준으로 판단 (전년 대비 3배 → 폐기)
    only_py = {"2025Q2": {"revenue": 100.0, "op": 10.0, "consensus": A}}
    assert not _consensus_sane({"revenue": 300.0, "op": 10.0, "np": 0.0}, only_py, 2026, 2)
    assert _consensus_sane({"revenue": 150.0, "op": 10.0, "np": 0.0}, only_py, 2026, 2)

    print("OK: gate passes real hypergrowth, still rejects contamination")


if __name__ == "__main__":
    test()
