"""derive_drawdown_metrics 단위 테스트. 실행: python scanner/test_drawdown_metrics.py"""
from drawdown_metrics import derive_drawdown_metrics


def approx(a, b, tol=0.05):
    return a is not None and abs(a - b) <= tol


def test_off_high_and_low_from_52w():
    # 250개: 고점 200, 저점 90, 최신 종가 100 → off_high -50%, low52 90
    highs = [200.0] + [150.0] * 248 + [100.0]
    lows = [90.0] + [120.0] * 249
    closes = [180.0] * 249 + [100.0]
    r = derive_drawdown_metrics(highs, lows, closes)
    assert r["high52"] == 200, r
    assert r["low52"] == 90, r
    assert approx(r["off_high"], -50.0), r


def test_weekly_monthly_returns():
    # 최신 110, 6거래일 전(=index -6) 100 → ret_1w +10%; 21거래일 전 100 → ret_1m +10%
    closes = [100.0] * 30
    closes[-6] = 100.0
    closes[-1] = 110.0
    closes[-21] = 100.0
    highs = [120.0] * 30
    lows = [80.0] * 30
    r = derive_drawdown_metrics(highs, lows, closes)
    assert approx(r["ret_1w"], 10.0), r
    assert approx(r["ret_1m"], 10.0), r


def test_short_series_returns_none():
    r = derive_drawdown_metrics([100.0, 90.0], [80.0, 70.0], [100.0, 90.0])
    assert r["ret_1m"] is None, r  # 21개 미만
    assert r["high52"] == 100, r   # 고점은 계산됨


def test_empty_series():
    r = derive_drawdown_metrics([], [], [])
    assert r == {"high52": None, "low52": None, "off_high": None, "ret_1w": None, "ret_1m": None}, r


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("ALL PASS")
