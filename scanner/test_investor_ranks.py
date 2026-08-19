# -*- coding: utf-8 -*-
"""장전 브리핑 '외국인·기관 순매매 상위' — 회귀 테스트.

지키는 것:
- ETF·ETN 제외. 안 빼면 기관 상위 15행이 KODEX 레버리지·KODEX 200으로만 채워진다.
- 순매수는 큰 값부터, 순매도는 가장 마이너스인 것부터. 순매도 표에 (+)가 섞이면 안 된다.
- 원 → 억원 환산.
- KRX가 막히면 조용히 0을 싣지 말고 error만 남긴다(프롬프트가 섹션을 통째로 생략).
"""
import sys
from pathlib import Path

import pandas as pd
import pykrx.stock as ps

sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefing_data as bd  # noqa: E402

BN = 100_000_000

# 티커: (종목명, 시장, 등락률, 외국인 순매수원, 기관 순매수원)
FIXTURE = {
    "005930": ("삼성전자", "KOSPI", -2.19, 1778 * BN, -2572 * BN),
    "000660": ("SK하이닉스", "KOSPI", 1.03, 7904 * BN, -1237 * BN),
    "009150": ("삼성전기", "KOSPI", -7.57, -4159 * BN, -1686 * BN),
    "005380": ("현대차", "KOSPI", -3.97, -816 * BN, 12 * BN),
    "122630": ("KODEX 레버리지", "KOSPI", -4.30, 50 * BN, 9299 * BN),   # ETF — 빠져야 한다
    "196170": ("알테오젠", "KOSDAQ", -5.10, 208 * BN, -370 * BN),
    "247540": ("에코프로비엠", "KOSDAQ", -6.51, -4 * BN, -153 * BN),
    "580011": ("삼성 레버리지 WTI ETN", "KOSDAQ", 0.5, 1 * BN, 300 * BN),  # ETN — 빠져야 한다
}


def _install_fake_pykrx():
    bd._OHLCV_CACHE.clear()
    ps.get_nearest_business_day_in_a_week = lambda *a, **k: "20260818"
    ps.get_etf_ticker_list = lambda d: ["122630"]
    ps.get_etn_ticker_list = lambda d: ["580011"]

    def ohlcv(date, market="KOSPI"):
        rows = {t: {"등락률": v[2]} for t, v in FIXTURE.items() if v[1] == market}
        return pd.DataFrame.from_dict(rows, orient="index")

    def net(fromdate, todate, market, investor):
        col = 3 if investor == "외국인" else 4
        rows = {t: {"종목명": v[0], "순매수거래대금": v[col]}
                for t, v in FIXTURE.items() if v[1] == market}
        return pd.DataFrame.from_dict(rows, orient="index")

    ps.get_market_ohlcv_by_ticker = ohlcv
    ps.get_market_net_purchases_of_equities = net


def test_ranks():
    _install_fake_pykrx()
    out = bd.collect_investor_ranks("2026-08-19", n=15)

    assert "error" not in out, out.get("error")
    assert out["base_date"] == "2026-08-18"

    names = {r["name"] for k in ("foreign_buy", "foreign_sell", "inst_buy", "inst_sell")
             for r in out[k]}
    assert "KODEX 레버리지" not in names, "ETF가 안 걸러졌다 — 기관 상위가 LP 물량으로 덮인다"
    assert "삼성 레버리지 WTI ETN" not in names, "ETN이 안 걸러졌다"

    # 외국인 순매수: SK하이닉스(7,904억) > 삼성전자(1,778억) > 알테오젠(208억)
    assert [r["name"] for r in out["foreign_buy"]] == ["SK하이닉스", "삼성전자", "알테오젠"]
    assert out["foreign_buy"][0]["net_bn"] == 7904.0, "원 → 억원 환산이 틀렸다"
    assert out["foreign_buy"][0]["market"] == "KOSPI"
    assert out["foreign_buy"][0]["change_pct"] == 1.03, "등락률이 전일 시세와 안 붙었다"

    # 외국인 순매도: 가장 마이너스인 것부터, 전부 음수
    assert [r["name"] for r in out["foreign_sell"]] == ["삼성전기", "현대차", "에코프로비엠"]
    assert all(r["net_bn"] < 0 for r in out["foreign_sell"])
    assert out["foreign_sell"][0]["net_bn"] == -4159.0

    # 기관도 같은 규약 — 코스피·코스닥이 한 순위에 섞인다
    assert [r["name"] for r in out["inst_sell"][:2]] == ["삼성전자", "삼성전기"]
    assert [r["name"] for r in out["inst_buy"]] == ["현대차"]
    assert out["inst_buy"][0]["market"] == "KOSPI"
    assert out["foreign_buy"][2]["market"] == "KOSDAQ"


def test_n_caps_rows():
    _install_fake_pykrx()
    out = bd.collect_investor_ranks("2026-08-19", n=2)
    assert len(out["foreign_buy"]) == 2 and len(out["foreign_sell"]) == 2


def test_krx_blocked_leaves_error_not_zeros():
    bd._OHLCV_CACHE.clear()
    ps.get_nearest_business_day_in_a_week = lambda *a, **k: "20260818"

    def boom(*a, **k):
        raise RuntimeError("KRX 로그인 실패")

    ps.get_etf_ticker_list = boom
    out = bd.collect_investor_ranks("2026-08-19")
    assert "error" in out and "KRX" in out["error"]
    # 빈 표를 만들어 싣느니 키 자체를 안 만든다 — 프롬프트가 섹션을 생략한다
    assert "foreign_buy" not in out and "inst_buy" not in out


if __name__ == "__main__":
    test_ranks()
    test_n_caps_rows()
    test_krx_blocked_leaves_error_not_zeros()
    print("ok")
