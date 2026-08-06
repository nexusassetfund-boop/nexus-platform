"""거래일 판정 — 개별 종목 캔들 기준 (KS11 지연에 오판하지 않는지)"""
import datetime as dt

import pandas as pd

from run_scan import _is_trading_day_today


def _df(day: dt.date) -> pd.DataFrame:
    return pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex([pd.Timestamp(day)]))


def test_today_candle_means_trading_day():
    today = dt.date.today()
    assert _is_trading_day_today({"005930": _df(today - dt.timedelta(days=3)),
                                  "058610": _df(today)}) is True


def test_no_today_candle_means_holiday():
    stale = dt.date.today() - dt.timedelta(days=1)
    assert _is_trading_day_today({"005930": _df(stale)}) is False


def test_empty_map_keeps_old_behavior():
    assert _is_trading_day_today({}) is True
    assert _is_trading_day_today({"005930": None}) is True


if __name__ == "__main__":
    test_today_candle_means_trading_day()
    test_no_today_candle_means_holiday()
    test_empty_map_keeps_old_behavior()
    print("ok")
