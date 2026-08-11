# -*- coding: utf-8 -*-
"""수출 데이터 신선도 게이트 — 회귀 테스트.

관세청 확정치는 월 1회 들어오는데 브리핑은 매일 아침 나간다. 게이트가 없으면 같은 달
수치를 한 달 내내 반복해 그 자체가 노이즈가 된다. 갱신된 직후에만 나가는지 고정한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefing_data as bd  # noqa: E402

TRADE = {
    "data_month": "202606",
    "stocks": [
        {"name": "현대로템", "label": "철도차량 부분품", "q_sum_yoy": 5837.8, "flags": ["lumpy"]},
        {"name": "인텔리안테크", "label": "안테나", "q_sum_yoy": -48.1, "flags": ["q_drop"]},
        {"name": "이름없음", "label": "기타", "q_sum_yoy": None},
    ],
}


def _fake(report_updated, report_month="202606"):
    bd._JSON_CACHE.clear()
    bd._JSON_CACHE["/data/trade.json"] = TRADE
    bd._JSON_CACHE["/data/trade_report.json"] = {
        "updated": report_updated,
        "items": [{"id": f"trade-{report_month}", "month": report_month,
                   "title": "양극재는 신기록, 반도체 후공정은 역주행", "summary": "6월 확정치 요약"}],
    }


def test_fresh_report_passes():
    _fake("2026-07-30 14:14")
    out = bd.collect_trade_export("2026-08-01")      # 리포트 2일 전 → 통과
    assert out is not None
    assert out["data_month"] == "202606"
    assert out["surge"][0]["name"] == "현대로템"      # q_sum_yoy None인 종목은 정렬에서 제외
    assert [d["name"] for d in out["drop"]] == ["인텔리안테크"]
    assert out["report"]["title"].startswith("양극재")


def test_stale_report_omitted():
    _fake("2026-07-30 14:14")
    assert bd.collect_trade_export("2026-08-11") is None   # 12일 전 → 생략
    _fake("2026-07-30 14:14")
    assert bd.collect_trade_export("2026-08-07") is None      # 8일 → 생략
    _fake("2026-07-30 14:14")
    assert bd.collect_trade_export("2026-08-06") is not None  # 경계 7일 → 통과


def test_data_rolled_ahead_of_report():
    # trade.json은 7월분으로 굴렀는데 리포트는 아직 6월분 — 리포트가 묵었어도 새 데이터다
    bd._JSON_CACHE.clear()
    bd._JSON_CACHE["/data/trade.json"] = {**TRADE, "data_month": "202607"}
    bd._JSON_CACHE["/data/trade_report.json"] = {
        "updated": "2026-06-30 10:00",
        "items": [{"month": "202606", "title": "6월 리포트", "summary": "-"}],
    }
    out = bd.collect_trade_export("2026-08-11")
    assert out is not None
    assert "202607" in out["why_now"]
    assert "report" not in out    # 리포트가 다른 달이면 인용하지 않는다


def test_missing_report_omits():
    # 리포트를 못 가져오면 조용히 반복 언급하느니 빠진다
    bd._JSON_CACHE.clear()
    bd._JSON_CACHE["/data/trade.json"] = TRADE
    bd._JSON_CACHE["/data/trade_report.json"] = {}
    assert bd.collect_trade_export("2026-08-11") is None


if __name__ == "__main__":
    test_fresh_report_passes()
    test_stale_report_omitted()
    test_data_rolled_ahead_of_report()
    test_missing_report_omits()
    print("ok")
