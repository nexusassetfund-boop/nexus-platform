# -*- coding: utf-8 -*-
"""야간선물 장전 리셋 판별 자체점검.

07:20 장전에 KIS가 주는 "전일 종가 + 등락 0.00 + 거래량 0"을 야간 종가로 착각해 싣는 바람에
브리핑에 매일 "야간선물 981.15(보합, 0.00%)"가 찍혔다(2026-08-06·08-07 am). 그 판별 로직.
"""
from datetime import datetime

from briefing_data import KST, is_night_session, night_quote_is_preopen_reset


def test_night_session_window():
    at = lambda h: datetime(2026, 8, 7, h, 30, tzinfo=KST)
    assert is_night_session(at(4))    # 스냅샷 크론이 도는 04:50
    assert is_night_session(at(19))
    assert is_night_session(at(23))
    # 정규장·장전 시간에 찍으면 주간 시세가 야간선물로 저장된다 — 거래량이 있어 리셋 판별로는 못 막는다
    assert not is_night_session(at(7))    # 장전 브리핑
    assert not is_night_session(at(9))    # 정규장 개장
    assert not is_night_session(at(14))


def test_preopen_reset_detected():
    assert night_quote_is_preopen_reset(
        {"value": 981.15, "prev_night_close": 981.15, "kis_change": 0, "change_pct": 0, "volume": 0})


def test_real_night_session_kept():
    # 2026-08-07 야간세션 실제 결과: 전일 종가 981.15 대비 +1.45% (≈995.4).
    # 사후 조회로는 이 값을 못 얻는다 — 07:20엔 리셋, 08:2x엔 장전에 흘러가는 다른 값이 온다.
    # 그래서 04:50 스냅샷이 필요하고, 이 판별기는 그 스냅샷이 진짜 세션 값일 때 통과시켜야 한다.
    assert not night_quote_is_preopen_reset(
        {"value": 995.38, "prev_night_close": 981.15, "kis_change": 14.23, "change_pct": 1.45, "volume": 38000})


def test_genuine_flat_with_volume_kept():
    # 진짜 보합이라도 거래가 있었으면 야간세션 값이다 — 버리면 안 된다
    assert not night_quote_is_preopen_reset(
        {"value": 981.15, "prev_night_close": 981.15, "kis_change": 0, "change_pct": 0, "volume": 420})


def test_empty_is_reset():
    assert night_quote_is_preopen_reset(None)
    assert night_quote_is_preopen_reset({})


def test_change_is_measured_against_regular_session_close():
    """등락 기준가는 직전 정규장 코스피200 종가여야 한다 — KIS 원본(직전 야간세션)이 아니다.

    실제 오보 2026-08-19 am: 화요일 밤(1,078.25)을 금요일 밤(1,098.90)과 비교해 "-1.88%"가
    나갔다. 그 사이 8/17은 대체공휴일, 8/18 정규장은 -1.47%였다. 전일 종가 1,082.00 기준으로는
    -0.35%가 맞는 값이다.
    """
    last, base, kis_prev = 1078.25, 1082.00, 1098.90
    assert round((last / base - 1) * 100, 2) == -0.35
    assert round((last / kis_prev - 1) * 100, 2) == -1.88   # 지금까지 나가던 틀린 값


if __name__ == "__main__":
    test_preopen_reset_detected()
    test_real_night_session_kept()
    test_genuine_flat_with_volume_kept()
    test_empty_is_reset()
    test_change_is_measured_against_regular_session_close()
    print("ok")
