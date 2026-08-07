# -*- coding: utf-8 -*-
"""야간선물 장전 리셋 판별 자체점검.

07:20 장전에 KIS가 주는 "전일 종가 + 등락 0.00 + 거래량 0"을 야간 종가로 착각해 싣는 바람에
브리핑에 매일 "야간선물 981.15(보합, 0.00%)"가 찍혔다(2026-08-06·08-07 am). 그 판별 로직.
"""
from briefing_data import night_quote_is_preopen_reset


def test_preopen_reset_detected():
    assert night_quote_is_preopen_reset(
        {"value": 981.15, "prev_close": 981.15, "change": 0, "change_pct": 0, "volume": 0})


def test_real_night_session_kept():
    # 2026-08-07 야간세션 실제 결과: 전일 종가 981.15 대비 +1.45% (≈995.4).
    # 사후 조회로는 이 값을 못 얻는다 — 07:20엔 리셋, 08:2x엔 장전에 흘러가는 다른 값이 온다.
    # 그래서 04:50 스냅샷이 필요하고, 이 판별기는 그 스냅샷이 진짜 세션 값일 때 통과시켜야 한다.
    assert not night_quote_is_preopen_reset(
        {"value": 995.38, "prev_close": 981.15, "change": 14.23, "change_pct": 1.45, "volume": 38000})


def test_genuine_flat_with_volume_kept():
    # 진짜 보합이라도 거래가 있었으면 야간세션 값이다 — 버리면 안 된다
    assert not night_quote_is_preopen_reset(
        {"value": 981.15, "prev_close": 981.15, "change": 0, "change_pct": 0, "volume": 420})


def test_empty_is_reset():
    assert night_quote_is_preopen_reset(None)
    assert night_quote_is_preopen_reset({})


if __name__ == "__main__":
    test_preopen_reset_detected()
    test_real_night_session_kept()
    test_genuine_flat_with_volume_kept()
    test_empty_is_reset()
    print("ok")
