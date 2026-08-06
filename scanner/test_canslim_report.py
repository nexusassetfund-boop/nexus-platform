"""CANSLIM 리포트 판정 로직 테스트 (네트워크·결과파일 불필요).
실행: python scanner/test_canslim_report.py

판정은 grid를 보기 전에 고정한 규칙이어야 한다 — 합성 지표로 세 결론을 모두 강제해
경계에서 뒤집히지 않는지 확인한다.
"""
import canslim_backtest_report as rpt


def _m(excess, mdd=-20.0, trades=200, hold=18.0, cash=5.0):
    return {"excess_cagr_pct": excess, "mdd_pct": mdd, "closed_trades": trades,
            "avg_holdings": hold, "avg_cash_pct": cash, "cagr_pct": excess + 5,
            "win_rate": 55.0, "avg_win": 12.0, "avg_loss": 8.0}


def test_gate0_rejects_empty_strategy():
    """후보가 상시 0이면 현금 100% — '무손실'이 아니라 '평가 불가'다."""
    ok, why = rpt._gate0(_m(0.0, trades=0, hold=0.0, cash=100.0))
    assert not ok and "현금" in why and "거래" in why, why
    assert rpt._gate0(_m(1.0))[0] is True


def test_verdict_loosening_effective():
    c = {"orig5": _m(1.0), "loose5": _m(6.0), "rs_only": _m(2.0)}
    g = {"slip_x2": _m(3.0)}
    v, _ = rpt.verdict(c, g)
    assert v == "완화가 유효", v


def test_verdict_slippage_kills_it():
    """완화하면 소형주 비중이 커져 실제 슬리피지가 나쁘다 — 2배에서 뒤집히면 무효."""
    c = {"orig5": _m(1.0), "loose5": _m(6.0), "rs_only": _m(2.0)}
    v, _ = rpt.verdict(c, {"slip_x2": _m(-0.5)})
    assert v.startswith("완화 무효"), v


def test_verdict_no_meaningful_difference():
    c = {"orig5": _m(4.0), "loose5": _m(4.5), "rs_only": _m(1.0)}
    v, lines = rpt.verdict(c, {"slip_x2": _m(2.0)})
    assert v == "완화 무효 (원전 유지)", v
    assert any("min_score" in x for x in lines)


def test_verdict_canslim_has_no_alpha():
    """모든 변형이 rs_only를 못 이기면 임계치 논쟁 자체가 무의미하다."""
    c = {"orig5": _m(1.0), "loose5": _m(2.0), "rs_only": _m(5.0)}
    v, lines = rpt.verdict(c, {"roe8": _m(3.0)})
    assert v == "CANSLIM 자체가 알파 없음", v
    assert any("부가가치" in x for x in lines)


def test_verdict_negative_excess_is_no_alpha():
    c = {"orig5": _m(-3.0), "loose5": _m(-1.0), "rs_only": _m(-5.0)}
    v, _ = rpt.verdict(c, {})
    assert v == "CANSLIM 자체가 알파 없음", v


def test_verdict_mdd_blowout_blocks_pass():
    """수익이 좋아도 MDD가 rs_only 대비 크게 나빠지면 통과시키지 않는다."""
    c = {"orig5": _m(1.0), "loose5": _m(6.0, mdd=-40.0), "rs_only": _m(2.0, mdd=-20.0)}
    v, _ = rpt.verdict(c, {"slip_x2": _m(3.0)})
    assert v.startswith("완화 무효"), v


def test_verdict_missing_controls():
    assert rpt.verdict({"orig5": _m(1.0)}, {})[0] == "판정 불가"


def test_eval_compat_maps_metric_names():
    e = rpt._eval_compat({"closed_trades": 120, "win_rate": 51.0, "avg_win": 10.0,
                          "avg_loss": 7.0, "mdd_pct": -22.0, "yearly_pct": {2021: 3}})
    assert e["trades"] == 120 and e["mdd_pct"] == -22.0 and e["yearly"] == {2021: 3}, e
    assert rpt._eval_compat({}) == {}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
