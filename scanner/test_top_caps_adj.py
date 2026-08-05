"""권리락 표시 자체 점검 — 실제 사례(2026-08-05 알테오젠 30% 무상증자) 기준."""
from briefing_data import _mark_price_adj


def test_kwolrirak_flagged():
    rows = [
        {"code": "196170", "change_pct": 3.75},    # 알테오젠 — 권리락(기준가 266,923)
        {"code": "005930", "change_pct": 2.5},     # 삼성전자 — 정상
        {"code": "999999", "change_pct": 1.0},     # 전전일 종가 없음 (신규상장 등)
    ]
    _mark_price_adj(rows,
                    closes={"196170": 277000, "005930": 246000, "999999": 1000},
                    prev_closes={"196170": 347000, "005930": 240000})
    assert rows[0]["price_adj"] is True
    assert rows[0]["raw_change_pct"] == -20.17
    assert "price_adj" not in rows[1]              # 정상 종목은 건드리지 않는다
    assert "price_adj" not in rows[2]              # 데이터 없으면 조용히 넘어간다


if __name__ == "__main__":
    test_kwolrirak_flagged()
    print("ok")
