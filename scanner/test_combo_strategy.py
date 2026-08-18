"""조합 전략 순수 로직 테스트 (네트워크 없음).
실행: python scanner/test_combo_strategy.py
"""
import combo_strategy as cs

COLS = ["code", "status", "gap", "price"]


def _nhc(daily: dict, today: str, today_codes: list[str], meta=None):
    return {"data_last_date": today, "daily_cols": COLS,
            "daily": {d: {"items": [[c, "watch", 5.0, 1000] for c in codes]}
                      for d, codes in daily.items()},
            "candidates": [{"code": c} for c in today_codes],
            "meta": meta or {}}


def _qg(pairs):
    return {"base_date": "20260731", "updated": "2026-07-31T16:00:00+09:00",
            "pool": [{"code": c, "name": f"종목{c}", "composite": s,
                      "quality_z": s, "mom_z": 0.0} for c, s in pairs]}


def test_window_counts_trading_days_not_calendar_days():
    """창은 거래일 20개 — 날짜 간격이 아니라 daily 키 개수로 센다."""
    daily = {f"2026-06-{d:02d}": [f"{d:06d}"] for d in range(1, 26)}
    nhc = _nhc(daily, "2026-06-26", ["999999"])
    win = cs.window_members(nhc, window=20)
    assert "999999" in win, "오늘 명단은 항상 창에 포함"
    assert f"{25:06d}" in win and f"{7:06d}" in win, win     # 최근 20개(7~25일) 안
    assert f"{6:06d}" not in win, "창 밖(21번째 이전)은 제외"


def test_window_asof_ignores_later_dates():
    """asof를 주면 그 이후 데이터는 안 본다 — 월 첫 거래일에 전월 신호를 재현할 때 필수."""
    daily = {"2026-06-29": ["000010"], "2026-06-30": ["000020"], "2026-07-01": ["000030"]}
    nhc = _nhc(daily, "2026-07-01", ["000030"])
    assert cs.window_members(nhc, asof="2026-06-30") == {"000010", "000020"}
    assert "000030" in cs.window_members(nhc, asof=None)


def test_select_intersects_and_ranks_by_composite():
    daily = {"2026-07-30": ["000010", "000020", "000030"]}
    nhc = _nhc(daily, "2026-07-31", ["000040"])
    qg = _qg([("000010", 0.5), ("000020", 1.9), ("000040", 1.2), ("000090", 3.0)])
    picks = cs.select(nhc, qg, top=10)
    codes = [p["code"] for p in picks]
    assert codes == ["000020", "000040", "000010"], codes   # composite 내림차순
    assert "000030" not in codes, "퀄리티 풀에 없으면 탈락"
    assert "000090" not in codes, "신고가 창에 없으면 탈락(점수가 높아도)"
    assert picks[0]["rank"] == 1


def test_select_respects_top_n():
    daily = {"2026-07-30": [f"{i:06d}" for i in range(1, 21)]}
    nhc = _nhc(daily, "2026-07-31", [])
    qg = _qg([(f"{i:06d}", float(i)) for i in range(1, 21)])
    assert len(cs.select(nhc, qg, top=10)) == 10


def _bars(code_px: dict):
    return {c: {d: {"open": o, "close": cl} for d, (o, cl) in days.items()}
            for c, days in code_px.items()}


def test_rebalance_only_on_new_month():
    daily = {"2026-07-31": ["000010"], "2026-08-03": ["000010"]}
    nhc = _nhc(daily, "2026-08-03", ["000010"])
    qg = _qg([("000010", 1.0)])
    bars = _bars({"000010": {"2026-08-03": (100.0, 110.0)}})
    # 같은 달이면 교체하지 않는다
    state = {"month": "2026-08", "holdings": [
        {"code": "000010", "name": "종목000010", "entry_date": "2026-08-03", "entry_price": 90.0}], "ledger": []}
    out, _ = cs.build(nhc, qg, state, bars)
    assert out["rebalanced_today"] is False
    assert out["portfolio"][0]["entry_price"] == 90.0, "기존 진입가 유지"
    # 달이 바뀌면 교체 + 원장 적립
    state2 = {"month": "2026-07", "holdings": [
        {"code": "000010", "name": "종목000010", "entry_date": "2026-07-01", "entry_price": 50.0}], "ledger": []}
    out2, st2 = cs.build(nhc, qg, state2, bars)
    assert out2["rebalanced_today"] is True
    assert out2["portfolio"][0]["entry_price"] == 100.0, "교체일 시가로 진입"
    assert len(st2["ledger"]) == 1
    assert st2["ledger"][0]["holdings"][0]["ret_pct"] == 100.0, "50 → 100 청산 = +100%"


def test_signal_uses_previous_month_window():
    """교체일 신호는 전월 마지막 거래일 기준 — 당일에야 뜬 종목은 이번 달에 못 들어온다."""
    daily = {"2026-07-31": ["000010"], "2026-08-03": ["000020"]}
    nhc = _nhc(daily, "2026-08-03", ["000020"])
    qg = _qg([("000010", 1.0), ("000020", 9.0)])
    bars = _bars({"000010": {"2026-08-03": (100.0, 100.0)},
                  "000020": {"2026-08-03": (100.0, 100.0)}})
    state = {"month": "2026-07", "holdings": [], "ledger": []}
    out, _ = cs.build(nhc, qg, state, bars)
    assert [h["code"] for h in out["portfolio"]] == ["000010"], out["portfolio"]
    assert [p["code"] for p in out["preview"]][0] == "000020", "미리보기는 오늘 기준"


def test_portfolio_flags_are_informational():
    """이탈 종목도 보유는 유지되고, 상태 플래그로만 표시된다(중도 청산 규칙 없음)."""
    daily = {"2026-08-03": ["000020"]}
    nhc = _nhc(daily, "2026-08-04", ["000020"])
    qg = _qg([("000010", 1.0), ("000020", 9.0)])
    bars = _bars({"000010": {"2026-08-04": (100.0, 80.0)}})
    state = {"month": "2026-08", "holdings": [
        {"code": "000010", "name": "종목000010", "entry_date": "2026-08-03", "entry_price": 100.0}], "ledger": []}
    out, _ = cs.build(nhc, qg, state, bars)
    h = out["portfolio"][0]
    assert h["code"] == "000010", "게이트를 벗어나도 계속 보유"
    assert h["in_gate"] is False and h["in_current_pick"] is False
    assert h["ret_pct"] == -20.0


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"=== {len(fns)}개 테스트 통과 ===")


if __name__ == "__main__":
    main()
