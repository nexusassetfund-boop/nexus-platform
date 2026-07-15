"""52주 고점대비·저점·1주·1개월 수익률 파생 — 추가 API 호출 없음(기존 OHLCV 재사용).

scanner/value_screen.py:_technicals 의 검증된 공식과 동일 규약.
시계열은 오래된→최신 순. 거래일 기준(휴장 보정 없음): 1주=5거래일, 1개월=20거래일.
low52 는 프론트 _pos52(52주 위치 바)에 필요.
"""
from __future__ import annotations


def _ret(closes, offset):
    if len(closes) <= offset:
        return None
    old = float(closes[-1 - offset])
    if old <= 0:
        return None
    return round((float(closes[-1]) / old - 1) * 100, 1)


def derive_drawdown_metrics(highs, lows, closes) -> dict:
    out = {"high52": None, "low52": None, "off_high": None, "ret_1w": None, "ret_1m": None}
    if not closes:
        return out
    price = float(closes[-1])
    tail_h = list(highs)[-250:]
    if tail_h:
        high52 = max(float(x) for x in tail_h)
        out["high52"] = round(high52)
        if high52 > 0:
            out["off_high"] = round((price / high52 - 1) * 100, 1)
    tail_l = list(lows)[-250:]
    if tail_l:
        out["low52"] = round(min(float(x) for x in tail_l))
    out["ret_1w"] = _ret(closes, 5)    # 5거래일 전 대비
    out["ret_1m"] = _ret(closes, 20)   # 20거래일 전 대비
    return out
