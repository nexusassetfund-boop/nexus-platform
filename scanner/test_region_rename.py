"""행정구역 개편으로 지명이 바뀌어도 시계열이 끊기지 않는지. 실행: python scanner/test_region_rename.py

배경(2026-07-01 개편, tradedata.go.kr 실측):
  인천 서구 → 서해구 개칭 (HS370790 동진쎄미켐: 202606 서구 69 → 202607 서해구 75)
  인천 중구 → 제물포구·영종구 분할 (HS848620 한미반도체: 202606 중구 30 → 202607 제물포구 20 + 영종구 11)
  광주광역시(29)·전라남도(46) → 전남광주통합특별시(12)

series_for 가 지명을 정확히 일치시켜 고르기 때문에, 이름이 바뀌면 개편 이전 행이
한 건도 안 잡혀 60개월 시계열이 한 달로 잘린다. 그러면 YoY·TTM·역대최고가 전부
무의미해진다 — 5종목이 카드에서 사라진 것과 별개의 두 번째 고장이다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trade_stats import series_for

# 실측값 그대로 (천USD)
ROWS = [
    {"sgg": "인천광역시 서구",   "period": "202605", "exp_amt": 62.0, "exp_cnt": 3},
    {"sgg": "인천광역시 서구",   "period": "202606", "exp_amt": 69.0, "exp_cnt": 3},
    {"sgg": "인천광역시 서해구", "period": "202607", "exp_amt": 75.0, "exp_cnt": 4},
    {"sgg": "인천광역시 연수구", "period": "202607", "exp_amt": 25.0, "exp_cnt": 1},
    {"sgg": "인천광역시 검단구", "period": "202607", "exp_amt": 12.0, "exp_cnt": 1},
]


def test_without_alias_series_collapses():
    # 이 동작이 바로 고장의 정체다 — 고쳤다고 착각하지 않도록 명시적으로 고정한다.
    s = series_for(ROWS, "인천광역시 서해구")
    assert list(s) == ["202607"], s


def test_alias_bridges_the_rename():
    s = series_for(ROWS, "인천광역시 서해구", ["인천광역시 서구"])
    assert sorted(s) == ["202605", "202606", "202607"], s
    assert s["202606"]["amt"] == 69.0 and s["202607"]["amt"] == 75.0, s


def test_other_districts_never_leak_in():
    # 별칭을 넣어도 같은 시도의 남의 지역이 섞이면 프록시가 무너진다.
    s = series_for(ROWS, "인천광역시 서해구", ["인천광역시 서구"])
    assert s["202607"]["amt"] == 75.0, "연수구·검단구가 섞였다"


def test_empty_alias_is_same_as_none():
    assert series_for(ROWS, "인천광역시 서해구", []) == series_for(ROWS, "인천광역시 서해구")


def test_split_district_sums_old_name_once():
    # 중구 → 제물포구 + 영종구 분할. 옛 중구 물량은 제물포구 계열에 한 번만 들어간다.
    rows = [
        {"sgg": "인천광역시 중구",     "period": "202606", "exp_amt": 30.0, "exp_cnt": 2},
        {"sgg": "인천광역시 제물포구", "period": "202607", "exp_amt": 20.0, "exp_cnt": 1},
        {"sgg": "인천광역시 영종구",   "period": "202607", "exp_amt": 11.0, "exp_cnt": 1},
    ]
    s = series_for(rows, "인천광역시 제물포구", ["인천광역시 중구"])
    assert s["202606"]["amt"] == 30.0 and s["202607"]["amt"] == 20.0, s


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("ALL PASS")
