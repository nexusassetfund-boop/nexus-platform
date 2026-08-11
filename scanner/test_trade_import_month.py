"""수입 워치 기준월 선택 단위 테스트. 실행: python scanner/test_trade_import_month.py

배경: 수입 워치는 품목별 API를, 종목 카드는 시군구별 API를 쓴다. 품목별이 먼저
갱신되므로 기준월을 공유하면 이미 받아둔 최신월을 버린다(8/11 실행에서 202607이
계열에 있는데 202606으로 표시됐다). 각자의 최신월을 쓰되 상한은 지켜야 한다.
"""
from trade_stats import latest_month_in, compute_metrics


def test_picks_newest_within_cap():
    # 시군구가 202606으로 물러나도 품목별 계열의 202607을 쓴다 — 이번 수정의 핵심.
    ser = {"202605": {}, "202606": {}, "202607": {}}
    assert latest_month_in(ser, "202607") == "202607"


def test_cap_blocks_partial_month():
    # 아직 안 끝난 달(202608)이 섞여 들어와도 상한 밖이면 고르지 않는다.
    # 고르면 부분 집계가 완결월로 찍혀 가짜 급감이 된다.
    ser = {"202606": {}, "202607": {}, "202608": {}}
    assert latest_month_in(ser, "202607") == "202607"


def test_falls_back_when_newest_missing():
    # 이 품목만 갱신이 늦으면 자기 최신월로 물러난다 — 카드가 사라지지 않는다.
    ser = {"202605": {}, "202606": {}}
    assert latest_month_in(ser, "202607") == "202606"


def test_none_when_empty_or_all_above_cap():
    assert latest_month_in({}, "202607") is None
    assert latest_month_in({"202608": {}}, "202607") is None


def test_year_boundary_is_chronological_not_lexical_accident():
    # 202512 < 202601. 문자열 비교가 시간순과 어긋나지 않는지 연말 경계로 확인.
    ser = {"202511": {}, "202512": {}, "202601": {}}
    assert latest_month_in(ser, "202601") == "202601"
    assert latest_month_in(ser, "202512") == "202512"


def test_metrics_follow_the_chosen_month():
    # 고른 달이 실제로 지표에 반영되는지 — 월을 바꾸면 금액·전년비가 함께 바뀌어야 한다.
    ser = {
        "202506": {"amt": 100.0}, "202507": {"amt": 200.0},
        "202606": {"amt": 150.0}, "202607": {"amt": 400.0},
    }
    jun = compute_metrics(ser, "202606")
    jul = compute_metrics(ser, "202607")
    assert jun["amount"] == 150.0 and jun["yoy"] == 50.0, jun     # 150 vs 100
    assert jul["amount"] == 400.0 and jul["yoy"] == 100.0, jul    # 400 vs 200


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("ALL PASS")
