"""CANSLIM 백테스트 순수 로직 테스트 (네트워크 없음).
실행: python scanner/test_canslim_backtest.py
"""
import pandas as pd

import canslim_backtest as cbt
import canslim_screener as cs

T = pd.Timestamp


def test_quarter_fiscal_for_respects_filing_deadline():
    """분기보고서 법정 제출기한(분기말+45일)이 지나야 그 분기를 쓴다.

    라이브 quarter_ni_yoy는 '최신 제출본 탐색'이라 연도 해상도다 — 백테스트에 그대로
    쓰면 최대 ~11개월 미래를 본다. 여기가 look-ahead의 유일한 실질 위험 지점.
    """
    assert cbt.quarter_fiscal_for(T("2023-05-14")) == (2022, "11014")   # 1Q 기한(5/15) 전
    assert cbt.quarter_fiscal_for(T("2023-05-15")) == (2023, "11013")   # 기한 당일 = 확정
    assert cbt.quarter_fiscal_for(T("2023-05-16")) == (2023, "11013")
    assert cbt.quarter_fiscal_for(T("2023-08-13")) == (2023, "11013")   # 2Q 기한(8/14) 전
    assert cbt.quarter_fiscal_for(T("2023-08-16")) == (2023, "11012")
    assert cbt.quarter_fiscal_for(T("2023-11-16")) == (2023, "11014")
    # 연초 — 직전 연도 3Q가 가장 최근 확정 분기 (4Q는 사업보고서라 C에서 제외)
    assert cbt.quarter_fiscal_for(T("2024-02-10")) == (2023, "11014")


def test_quarter_fiscal_for_never_returns_future_quarter():
    """어떤 신호일에도 '분기말이 신호일보다 뒤'인 분기를 반환하면 안 된다."""
    ends = {"11013": (3, 31), "11012": (6, 30), "11014": (9, 30)}
    for d in pd.date_range("2021-01-31", "2026-06-30", freq="ME"):
        y, r = cbt.quarter_fiscal_for(d)
        m, dd = ends[r]
        qend = T(year=y, month=m, day=dd)
        assert qend <= d, (d, y, r)
        assert qend + pd.Timedelta(days=45) <= d, (d, y, r)   # 기한도 지났어야 한다


def test_quarter_fiscal_for_lag_is_configurable():
    assert cbt.quarter_fiscal_for(T("2023-05-14"), lag_days=30) == (2023, "11013")
    assert cbt.quarter_fiscal_for(T("2023-05-14"), lag_days=90) == (2022, "11014")


def _closes(series: dict, dates):
    return pd.DataFrame(series, index=dates)


def test_make_rs_reproduces_kkangto_percentile():
    """깡토 RS = 0.5*3M+0.3*6M+0.2*12M 의 시장별 백분위(1~99). 상승폭 순서가 곧 순위."""
    dates = pd.bdate_range("2022-01-03", periods=300)
    n = len(dates)
    # A가 가장 가파르고 C가 가장 완만 — RS도 그 순서여야 한다
    data = {"000010": [100 * (1 + 0.004 * i) for i in range(n)],
            "000020": [100 * (1 + 0.002 * i) for i in range(n)],
            "000030": [100 * (1 + 0.001 * i) for i in range(n)]}
    closes = _closes(data, dates)
    uni = [{"code": c, "market": "KOSPI"} for c in data]
    rs = cbt.make_rs(closes, dates[-1], uni)
    assert rs["000010"] > rs["000020"] > rs["000030"], rs
    assert rs["000010"] == 99 and rs["000030"] == 1, rs      # 최상·최하 = 99·1
    # 시장이 다르면 각자 백분위 — KOSDAQ 단독 종목은 50
    uni2 = [{"code": "000010", "market": "KOSPI"}, {"code": "000020", "market": "KOSDAQ"}]
    rs2 = cbt.make_rs(closes, dates[-1], uni2)
    assert rs2["000010"] == 50 and rs2["000020"] == 50, rs2


def test_make_rs_and_prox_ignore_future_bars():
    """신호일 이후 봉이 결과를 바꾸면 look-ahead다."""
    dates = pd.bdate_range("2022-01-03", periods=400)
    n = len(dates)
    vals = [100 * (1 + 0.002 * i) for i in range(n)]
    closes = _closes({"000010": vals, "000020": vals[::-1]}, dates)
    sig = dates[300]
    uni = [{"code": "000010", "market": "KOSPI"}, {"code": "000020", "market": "KOSPI"}]
    rs_full = cbt.make_rs(closes, sig, uni)
    prox_full = cbt.make_prox(closes, sig)
    trimmed = closes[closes.index <= sig]
    assert cbt.make_rs(trimmed, sig, uni) == rs_full
    assert cbt.make_prox(trimmed, sig) == prox_full


def test_make_prox_is_close_over_52w_high():
    dates = pd.bdate_range("2022-01-03", periods=300)
    vals = [100.0] * 299 + [80.0]           # 고점 100, 현재 80
    prox = cbt.make_prox(_closes({"000010": vals}, dates), dates[-1])
    assert abs(prox["000010"] - 0.8) < 1e-9, prox


def test_make_vol2x_matches_live_window_rule():
    """S2 = 최근 20거래일 중 '거래량 2배 초과 & 종가 상승'일 존재 (라이브 _detail과 동일)."""
    dates = pd.bdate_range("2022-01-03", periods=100)
    n = len(dates)
    closes = _closes({"000010": [100.0 + i for i in range(n)]}, dates)   # 매일 상승
    vol = [1000.0] * n
    vol[-5] = 5000.0                                    # 최근 20일 안에 급증
    assert cbt.make_vol2x(_closes({"000010": vol}, dates), closes, dates[-1])["000010"] == 1
    vol2 = [1000.0] * n
    vol2[-40] = 5000.0                                  # 20일 창 밖
    assert cbt.make_vol2x(_closes({"000010": vol2}, dates), closes, dates[-1])["000010"] == 0
    # 거래량은 터졌지만 종가가 하락한 날은 인정하지 않는다
    down = _closes({"000010": [100.0 - i for i in range(n)]}, dates)
    assert cbt.make_vol2x(_closes({"000010": vol}, dates), down, dates[-1])["000010"] == 0


def test_pit_universe_filters_match_live():
    """라이브 build()의 1차 필터와 같은 규칙 + shares = 시총/종가."""
    p = cbt._base_params()
    dump = {"cap": [
        ["005930", "삼성전자", "KOSPI", "70,000", "418,000,000,000,000"],
        ["000001", "저가주", "KOSPI", "500", "200,000,000,000"],        # 주가 미달
        ["000002", "소형주", "KOSDAQ", "5,000", "50,000,000,000"],      # 시총 미달
        ["000003", "우선주", "KOSPI", "5,000", "500,000,000,000"],      # 코드 끝 0 아님 → 제외
        ["000040", "무슨스팩1호", "KOSDAQ", "5,000", "500,000,000,000"],  # 스팩 제외
        ["000050", "코넥스주", "KONEX", "5,000", "500,000,000,000"],     # KONEX 제외
    ]}
    orig = cbt.load_krx_dumps
    cbt.load_krx_dumps = lambda *a, **k: {"20260630": dump}
    cbt._dump_key_for = lambda sig: "20260630"
    try:
        uni = cbt.pit_universe(T("2026-06-30"), p)
    finally:
        cbt.load_krx_dumps = orig
    assert [u["code"] for u in uni] == ["005930"], uni
    assert abs(uni[0]["shares"] - 418_000_000_000_000 / 70_000) < 1, uni[0]
    assert cs.score({"shares": uni[0]["shares"]})[1]["S1"] == 0     # 59.7억 주 → S1 미충족


def test_screen_uses_injected_thresholds():
    """완화 임계치 주입이 실제로 선정 결과를 바꾸는가 (백테스트의 존재 이유)."""
    rec = {"roe": 12.0, "q_ni_yoy": 12.0, "ni_growth": 12.0,
           "proximity_52w": 0.82, "shares": 3e7, "vol_2x_bo": 1, "rs_kkangto": 75}
    assert cs.score(rec)[0] == 2                                   # 원전: S1+S2만
    assert cs.score(rec, cbt.LOOSE_TH)[0] == 7                     # 완화: 전부 통과


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
