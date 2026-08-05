# -*- coding: utf-8 -*-
"""발표일 추정 보정 자체검증 — 주말 발표일이 나오지 않는지."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from earnings_calendar import _bizday, _YEAR_52W  # noqa: E402


def test():
    # 52주는 요일을 보존한다 (365d는 하루 밀린다 — 8/8 토요일 버그의 원인)
    fri = dt.date(2025, 8, 8)
    assert fri.weekday() == 4, "전제: 2025-08-08은 금요일"
    assert (fri + dt.timedelta(days=365)).weekday() == 5, "365d는 토요일로 밀린다"
    assert (fri + _YEAR_52W).weekday() == 4, "52주는 금요일 유지"
    assert fri + _YEAR_52W == dt.date(2026, 8, 7)

    # 주말은 다음 월요일로
    assert _bizday(dt.date(2026, 8, 8)) == dt.date(2026, 8, 10)   # 토 → 월
    assert _bizday(dt.date(2026, 8, 9)) == dt.date(2026, 8, 10)   # 일 → 월
    assert _bizday(dt.date(2026, 8, 7)) == dt.date(2026, 8, 7)    # 평일은 그대로

    # 어떤 날짜를 넣어도 결과는 항상 평일
    d = dt.date(2026, 1, 1)
    while d < dt.date(2027, 1, 1):
        assert _bizday(d).weekday() < 5, d
        assert _bizday(d + _YEAR_52W).weekday() < 5, d
        d += dt.timedelta(days=1)

    print("OK: estimated dates are always weekdays; 52-week shift preserves weekday")


if __name__ == "__main__":
    test()
