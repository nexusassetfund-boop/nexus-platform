# -*- coding: utf-8 -*-
"""장전 브리핑 거래일 게이트 — 회귀 테스트.

버그: pykrx 판정은 '당일 데이터 존재 여부'라 장전(07:20)엔 항상 전 거래일을 돌려주고,
KRX 계정 시크릿이 붙은 뒤부터 매일 아침 '휴장일' 오판으로 브리핑이 건너뛰어졌다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefing_data as bd  # noqa: E402


def test_am_ignores_market_data(monkeypatch=None):
    # pykrx가 (장전이라) 전 거래일을 돌려줘도 am은 진행해야 한다
    import pykrx.stock as ps
    orig = getattr(ps, "get_nearest_business_day_in_a_week", None)
    ps.get_nearest_business_day_in_a_week = lambda *a, **k: "20260804"
    try:
        assert bd.is_trading_day("2026-08-05", "am") is True   # 수요일
        assert bd.is_trading_day("2026-08-08", "am") is False  # 토요일
        assert bd.is_trading_day("2026-08-09", "am") is False  # 일요일
        # pm은 기존대로 데이터 기반 휴장 판정 유지
        assert bd.is_trading_day("2026-08-05", "pm") is False
        assert bd.is_trading_day("2026-08-04", "pm") is True
    finally:
        if orig:
            ps.get_nearest_business_day_in_a_week = orig


if __name__ == "__main__":
    test_am_ignores_market_data()
    print("ok")
