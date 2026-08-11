# -*- coding: utf-8 -*-
"""전략 교차(브리핑 '넥서스 전략 브리핑' 섹션 재료) — 회귀 테스트.

배경: 이 섹션은 원래 탭별 상위 종목을 순서대로 읊는 데이터 나열이었고, 마지막 문단은
관세청 수출 데이터를 '모멘텀 원장'이라 잘못 부르며 유니버스 종목 수만 실었다.
교차 집계로 갈아끼우면서 홈 화면 '전략 교차' 카드와 같은 규칙을 지키는지 고정한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefing_data as bd  # noqa: E402

FAKE = {
    "/data/scan.json": {
        "scan_time": "2026-08-11T09:20:00+09:00",
        "results": [
            {"ticker": "005930", "name": "삼성전자", "stage": 2, "stage_label": "Stage2",
             "rs_rank": 88.4, "change_pct": -0.43},
            {"ticker": "0156T0", "name": "에이치엘지노믹스", "stage": 1, "stage_label": "Stage1",
             "change_pct": 3.1},                                  # 신형 영숫자 코드도 통과해야 한다
            {"ticker": "000660", "name": "SK하이닉스", "stage": 0, "change_pct": 1.0},  # stage 0 → 제외
        ],
    },
    "/data/tracking.json": {
        "updated": "2026-08-11T14:12:23+09:00",
        "holdings": [
            {"ticker": "005930", "name": "삼성전자", "days_held": 5, "return_pct": -6.8},
            {"ticker": "005930", "name": "삼성전자", "days_held": 2, "return_pct": 1.0},  # 2트랙 중복
        ],
    },
    "/data/quality_growth.json": {
        "updated": "2026-08-07T17:33:42+09:00",
        "candidates": [{"code": "0156T0", "name": "에이치엘지노믹스", "composite": 1.4, "roe": 22.0}],
    },
    "/data/value_screen.json": {"updated": "2026-08-07T17:29:35+09:00", "candidates": []},
    "/data/pullback.json": {"updated": "2026-08-10 17:55", "candidates": [
        {"ticker": "12345", "name": "코드깨짐", "pullback_score": 7},                    # 5자리 → 제외
    ]},
    "/data/canslim.json": {"updated": "2026-08-10 18:10", "candidates": [
        {"ticker": "000660", "name": "SK하이닉스", "canslim_score": 6, "strict": 1},     # 단독 1건
    ]},
    "/data/flow.json": {"updated": "2026-08-11 13:31", "candidates": [
        {"ticker": "005930", "name": "삼성전자", "grade": "A", "frgn_streak": 7},
        {"ticker": "000660", "name": "SK하이닉스", "grade": "C", "frgn_streak": 0},      # C는 교차 제외
    ]},
}


def test_cross():
    bd._JSON_CACHE.clear()
    bd._JSON_CACHE.update(FAKE)          # _get_json이 캐시를 먼저 보므로 네트워크를 타지 않는다
    out = bd.collect_strategy_cross()

    rows = {r["name"]: r for r in out["rows"]}
    assert set(rows) == {"삼성전자", "에이치엘지노믹스"}, rows.keys()

    # 소스당 1건 — 원장 2트랙으로 같은 종목이 두 줄이어도 배지는 하나
    s = rows["삼성전자"]
    assert len(s["strategies"]) == 3, s["strategies"]
    assert sum("전략 포트폴리오 보유" in x for x in s["strategies"]) == 1
    assert any("Stage2 · RS 88" in x for x in s["strategies"]), s["strategies"]
    assert s["change_pct"] == -0.43            # 스캔 결과에서 등락률 조인

    # 신형 영숫자 코드(0156T0)가 살아남아야 한다
    assert rows["에이치엘지노믹스"]["code"] == "0156T0"

    # 수급 C등급 단독 + stage 0이라 SK하이닉스는 CANSLIM 1건뿐 → 교차 미달
    assert "SK하이닉스" not in rows
    assert out["sources"]["flow"]["count"] == 1          # S·A만 집계
    assert out["sources"]["stage"]["count"] == 2         # stage>=1 만
    assert out["sources"]["pullback"]["count"] == 0      # 5자리 코드는 버린다

    # 소스별 기준일 — 없으면 묵은 명단이 오늘 명단처럼 읽힌다
    assert out["sources"]["quality"]["as_of"] == "2026-08-07"
    assert out["sources"]["stage"]["as_of"] == "2026-08-11"
    assert out["sources"]["pullback"]["as_of"] == "2026-08-10"


def test_source_error_does_not_kill_cross():
    bd._JSON_CACHE.clear()
    bd._JSON_CACHE.update({k: v for k, v in FAKE.items() if k != "/data/flow.json"})
    orig = bd.requests.get
    bd.requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        out = bd.collect_strategy_cross()
    finally:
        bd.requests.get = orig
    assert "error" in out["sources"]["flow"]
    assert {r["name"] for r in out["rows"]} == {"삼성전자", "에이치엘지노믹스"}


if __name__ == "__main__":
    test_cross()
    test_source_error_does_not_kill_cross()
    print("ok")
