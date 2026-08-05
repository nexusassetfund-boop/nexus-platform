# -*- coding: utf-8 -*-
"""코스피200 야간선물 수집 — 파싱·부호 자체 점검.

KIS는 등락폭/등락률을 부호 없이 주고 방향은 prdy_vrss_sign(1상한 2상승 3보합 4하한 5하락)에
담아 보낸다. 부호를 놓치면 야간선물 하락을 상승으로 쓰게 되므로 여기서 못박는다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefing_data as bd  # noqa: E402

# 2026-08-05 장전 실제 응답(발췌) — 상승 케이스
UP = {"hts_kor_isnm": "F 202609", "futs_prpr": "1047.40", "futs_prdy_vrss": "47.40",
      "prdy_vrss_sign": "2", "futs_prdy_clpr": "1000.00", "futs_prdy_ctrt": "4.74",
      "futs_hgpr": "1054.95", "futs_lwpr": "1045.30", "basis": "2.38",
      "hts_otst_stpl_qty": "164621", "acml_vol": "5882"}
DOWN = dict(UP, prdy_vrss_sign="5", futs_prpr="960.00", futs_prdy_vrss="40.00", futs_prdy_ctrt="4.00")


def _parse(o):
    """collect_night_futures 안의 파싱과 동일한 규칙 (KIS 호출 없이 검증)."""
    sign = -1 if str(o.get("prdy_vrss_sign") or "3") in ("4", "5") else 1
    return {"value": bd._num(o.get("futs_prpr")),
            "change": sign * abs(bd._num(o.get("futs_prdy_vrss")) or 0),
            "change_pct": sign * abs(bd._num(o.get("futs_prdy_ctrt")) or 0)}


def test_sign():
    assert _parse(UP) == {"value": 1047.4, "change": 47.4, "change_pct": 4.74}
    assert _parse(DOWN) == {"value": 960.0, "change": -40.0, "change_pct": -4.0}


def test_front_month_is_futures_row():
    code = bd._cme_front_month()
    assert code and code.startswith("A") and len(code) == 6, code  # 스프레드(D…) 아님


if __name__ == "__main__":
    test_sign()
    test_front_month_is_futures_row()
    print("ok")
