"""fetch_value 순수 계산 검증. 실행: python -m pytest engine/scanner/test_fetch_value.py -q
   (pytest 없으면: python engine/scanner/test_fetch_value.py)"""
import fetch_value as fv


def test_compute_quality_ext_basic():
    # EBIT 200, 세율후 NOPAT, 투하자본 = 자본1000 + 부채300 - 현금200 = 1100
    # ROIC = 200*(1-0.22)/1100*100 ≈ 14.18%
    out = fv._compute_quality_ext(
        rev=2000, op=200, ni=150, equity=1000, liab=300,
        ca=800, cl=400, cfo=180, capex=50, cash=200, interest=10)
    assert out["roic"] is not None and 13.5 < out["roic"] < 15.0
    assert out["fcf"] == 130          # 180 - 50
    assert out["net_cash"] == -100    # 200 - 300
    # EV/EBIT: EV = 시총 미상이므로 None (여기선 시총 인자 없음 → None)
    assert out["ev_ebit"] is None
    # 시총 미상 → FCF/매출 마진 대체: 130/2000*100 = 6.5
    assert out["fcf_margin"] == 6.5


def test_compute_quality_ext_missing():
    out = fv._compute_quality_ext(rev=None, op=None, ni=None, equity=None,
                                  liab=None, ca=None, cl=None, cfo=None,
                                  capex=None, cash=None, interest=None)
    assert out["roic"] is None
    assert out["fcf"] is None


def test_shares_empty_corp():
    # 빈 corp_code면 네트워크 접근 없이 None (오프라인 안전)
    assert fv._shares("") is None
    assert fv._shares(None) is None


if __name__ == "__main__":
    test_compute_quality_ext_basic()
    test_compute_quality_ext_missing()
    test_shares_empty_corp()
    print("OK")
