"""canslim_i_backtest 판정 로직 검증 — KRX·DART 없이 합성 데이터로 돌린다.

목적: 스프레드가 0으로 나왔을 때 "신호가 없어서"인지 "집계가 틀려서"인지 구분할 수 있게
심는 최소 체크. 알파를 심은 합성 표본에서 스프레드·IC가 잡히는지, 무알파 표본에서
0 근처로 나오는지만 본다.
"""
import numpy as np
import pandas as pd

import canslim_i_backtest as ib


def _obs(n_months, alpha, seed=0):
    """flow_cap 순위가 fwd에 alpha만큼 반영된 합성 관측치."""
    rng = np.random.default_rng(seed)
    out = []
    for m in range(n_months):
        rows = []
        for i in range(40):
            f = rng.normal()
            rows.append({"code": f"{i:06d}", "rs": 90, "prox": 0.9, "cap": 1e12,
                         "flow": f, "flow_cap": f,
                         "fwd": alpha * f + rng.normal() * 0.05})
        out.append({"sig": pd.Timestamp("2024-01-31"), "ex": pd.Timestamp("2024-02-01"),
                    "rows": rows})
    return out


def test_alpha_detected():
    r = ib.analyze(_obs(60, alpha=0.02, seed=1))
    assert r["spread_pct"] > 1.0, r          # 심은 알파가 스프레드로 잡혀야 한다
    assert r["spread_t"] > 3, r
    assert r["ic_mean"] > 0.2, r
    assert r["quintile_top_minus_bottom_pct"] > 1.0, r
    q = r["quintile_fwd_pct"]
    assert q == sorted(q), q                 # 단조 증가


def test_no_alpha_is_flat():
    r = ib.analyze(_obs(60, alpha=0.0, seed=2))
    assert abs(r["spread_pct"]) < 0.5, r
    assert abs(r["spread_t"]) < 2, r
    assert abs(r["ic_mean"]) < 0.05, r


def test_fwd_returns_basic():
    idx = pd.to_datetime(["2024-02-01", "2024-03-01"])
    opens = pd.DataFrame({"A": [100.0, 110.0], "B": [50.0, np.nan]}, index=idx)
    fwd = ib._fwd_returns(opens, idx[0], idx[1])
    assert round(fwd["A"], 4) == 0.1, fwd
    assert "B" not in fwd, fwd               # 결측은 제외
    assert ib._fwd_returns(opens, idx[0], pd.Timestamp("2024-04-01")) == {}


def test_t_stat_edges():
    assert np.isnan(ib._t_stat([1.0, 2.0]))          # 표본 부족
    assert np.isnan(ib._t_stat([1.0, 1.0, 1.0]))     # 분산 0
    assert ib._t_stat([1.0, 1.1, 0.9, 1.0]) > 5


if __name__ == "__main__":
    test_alpha_detected()
    test_no_alpha_is_flat()
    test_fwd_returns_basic()
    test_t_stat_edges()
    print("ok")
