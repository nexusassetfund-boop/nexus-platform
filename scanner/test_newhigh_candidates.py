"""신고가 후보 산출 단위 테스트. 실행: python scanner/test_newhigh_candidates.py

합성 일봉으로 gap 정의·상태 임계값·등재 창을 검증한다. marcap 다운로드는 타지 않는다.
"""
import json
import sys
from pathlib import Path

import pandas as pd

import newhigh_fetcher as nf
from newhigh_fetcher import add_adjusted, build_candidates, classify

DAYS = nf.HIGH52_BARS + nf.WATCH_WINDOW + 40   # 기준가(shift 포함) 확보용


def make(code, closes, highs=None, market="KOSPI", amount=1e9, marcap=1e12):
    """종가 리스트로 일봉 프레임 생성. 액면분할 없음(Changes = 전일 대비)."""
    highs = highs if highs is not None else list(closes)
    dates = pd.bdate_range("2023-01-02", periods=len(closes))
    prev = [closes[0]] + list(closes[:-1])
    return pd.DataFrame({
        "Code": code, "Name": "종목" + code, "Date": dates,
        "High": highs, "Low": [c * 0.98 for c in closes], "Close": closes,
        "Changes": [c - p for c, p in zip(closes, prev)],
        "Volume": 1000, "Amount": amount, "Marcap": marcap, "Market": market,
    })


def build(frames, tmp):
    """OUT_CAND를 임시 경로로 돌려놓고 build_candidates 실행."""
    df = add_adjusted(pd.concat(frames, ignore_index=True).sort_values(["Code", "Date"]))
    orig_out, orig_sec = nf.OUT_CAND, nf.SECTOR_CACHE
    nf.OUT_CAND, nf.SECTOR_CACHE = tmp, Path(tmp.parent / "no-such-sector.json")
    try:
        return build_candidates(df, df["Date"].max())
    finally:
        nf.OUT_CAND, nf.SECTOR_CACHE = orig_out, orig_sec


def by_code(out):
    return {r["code"]: r for r in out["candidates"]}


# ── classify: 임계값 경계 ──────────────────────────────────────────
def test_classify_thresholds():
    assert classify(-1.0, False) == "breaking"
    assert classify(0.0, False) == "breaking"          # 기준가와 동일 = 돌파로 본다
    assert classify(nf.GAP_IMMINENT, False) == "imminent"      # 3.0 포함
    assert classify(nf.GAP_IMMINENT + 0.01, False) == "near"
    assert classify(nf.GAP_NEAR, False) == "near"              # 7.0 포함
    assert classify(nf.GAP_NEAR + 0.01, False) == "watch"
    assert classify(nf.GAP_WATCH, False) == "watch"            # 15.0 포함
    assert classify(nf.GAP_WATCH + 0.01, False) == ""          # 탈락


def test_classify_touch_beats_gap_but_not_breakout():
    # 터치는 gap 구간보다 우선하지만, 종가가 이미 기준가 위면 돌파가 이긴다
    assert classify(5.0, True) == "touched_failed"
    assert classify(-2.0, True) == "breaking"


# ── build_candidates: gap 정의와 상태 ─────────────────────────────
def test_gap_is_relative_to_current_price(tmp):
    # 250일 내내 고가 100 → 기준가 100. 마지막 종가 80 → gap = (100-80)/80 = 25% (>15 탈락)
    # 마지막 종가 96 → gap = (100-96)/96 = 4.17% → 근접
    closes = [100.0] * (DAYS - nf.WATCH_WINDOW) + [99.0] * (nf.WATCH_WINDOW - 1) + [96.0]
    out = build([make("000010", closes)], tmp)
    r = by_code(out)["000010"]
    assert abs(r["gap"] - 4.17) < 0.02, r          # 분모가 현재가임을 못박는다
    assert r["status"] == "near", r
    assert r["high52"] == 100, r


def test_breaking_and_touched_failed(tmp):
    flat = [100.0] * (DAYS - 1)
    # 돌파: 마지막 종가가 기준가 위
    brk = make("000020", flat + [110.0])
    # 터치 후 밀림: 장중 고가는 기준가 초과, 종가는 아래
    tf = make("000030", flat + [98.0], highs=[100.0] * (DAYS - 1) + [105.0])
    out = build([brk, tf], tmp)
    got = by_code(out)
    assert got["000020"]["status"] == "breaking", got["000020"]
    assert got["000020"]["gap"] < 0, got["000020"]
    assert got["000030"]["status"] == "touched_failed", got["000030"]
    assert out["counts"]["breaking"] == 1 and out["counts"]["touched_failed"] == 1, out["counts"]


def test_far_from_high_is_excluded(tmp):
    # 고가 100 대비 종가 50 → gap 100% → 등재도 안 되고 후보에도 없다
    closes = [100.0] * (DAYS - 1) + [50.0]
    out = build([make("000040", closes)], tmp)
    assert by_code(out) == {}, out["candidates"]


def test_entry_requires_touch_within_window(tmp):
    # 창(60영업일) '밖'에서만 문턱에 닿고, 창 안에서는 계속 gap 10% 근처 → 등재 안 됨
    old_touch = [100.0] * (DAYS - nf.WATCH_WINDOW - 10) + [100.0] * 10
    recent = [91.0] * nf.WATCH_WINDOW           # gap = (100-91)/91 = 9.9% → 관찰 구간이지만 미등재
    out = build([make("000050", old_touch + recent)], tmp)
    assert by_code(out) == {}, "창 밖 터치만으로 등재되면 안 된다"

    # 창 '안'에서 문턱(gap<=3)에 닿았다가 밀린 경우 → 등재되고 관찰로 남는다
    recent2 = [98.0] * 5 + [91.0] * (nf.WATCH_WINDOW - 5)
    out2 = build([make("000060", old_touch + recent2)], tmp)
    r = by_code(out2)["000060"]
    assert r["status"] == "watch", r
    assert r["seen_days"] >= nf.WATCH_WINDOW - 5, r


def test_kosdaq_included_and_history_shape(tmp):
    closes = [100.0] * (DAYS - 1) + [99.0]
    out = build([make("000070", closes, market="KOSDAQ")], tmp)
    r = by_code(out)["000070"]
    assert r["market"] == "KOSDAQ", r
    assert len(r["spark"]) == nf.SPARK_BARS, len(r["spark"])
    hist = out["history"]["000070"]
    assert hist and len(hist[0]) == 4, hist[:1]
    assert hist[-1][0] == out["data_last_date"], hist[-1]      # 최근 항목이 마지막 거래일


def test_liquidity_floor_uses_20day_average_not_today(tmp):
    closes = [100.0] * (DAYS - 1) + [99.0]
    # 평소 거래가 없다가 오늘만 크게 터진 종목 — 20일 평균이 낮아 탈락해야 한다
    thin = make("000140", closes, amount=nf.MIN_AMOUNT_AVG20 / 5)
    thin.loc[thin.index[-1], "Amount"] = nf.MIN_AMOUNT_AVG20 * 50
    # 평소 거래가 충분한데 오늘만 조용한 종목 — 통과해야 한다
    liquid = make("000150", closes, amount=nf.MIN_AMOUNT_AVG20 * 3)
    liquid.loc[liquid.index[-1], "Amount"] = nf.MIN_AMOUNT_AVG20 / 10
    got = by_code(build([thin, liquid], tmp))
    assert "000140" not in got, "당일만 터진 종목이 통과했다 — 당일 거래대금으로 걸렸다는 뜻"
    assert "000150" in got, "평소 거래가 충분한데 오늘 조용하다고 탈락했다"


def test_liquidity_floor_boundary(tmp):
    closes = [100.0] * (DAYS - 1) + [99.0]
    ok = make("000160", closes, amount=nf.MIN_AMOUNT_AVG20)          # 하한과 동일 = 통과
    no = make("000170", closes, amount=nf.MIN_AMOUNT_AVG20 * 0.9)    # 미달 = 탈락
    got = by_code(build([ok, no], tmp))
    assert "000160" in got and "000170" not in got, sorted(got)


def test_market_cap_floor(tmp):
    closes = [100.0] * (DAYS - 1) + [99.0]           # 신고가 문턱 — 시총만 다르게 준다
    big = make("000110", closes, marcap=nf.MIN_MARCAP)          # 하한 정확히 = 통과
    small = make("000120", closes, marcap=nf.MIN_MARCAP - 1)    # 1원 미달 = 탈락
    got = by_code(build([big, small], tmp))
    assert "000110" in got, "시총 하한과 같으면 통과해야 한다"
    assert "000120" not in got, "시총 하한 미달인데 남았다"
    assert got["000110"]["marcap_eok"] == round(nf.MIN_MARCAP / 1e8), got["000110"]


def test_unknown_market_cap_is_excluded(tmp):
    # 시총이 비면 하한을 우회한다 — 조용히 통과시키면 필터가 무의미해진다
    closes = [100.0] * (DAYS - 1) + [99.0]
    frame = make("000130", closes)
    frame.loc[frame.index[-1], "Marcap"] = float("nan")
    assert by_code(build([frame], tmp)) == {}, "시총 미상 종목이 통과했다"


def test_small_trading_value_keeps_a_decimal(tmp):
    # 유동성 하한은 직전 20일 평균에 걸리므로, 평소엔 거래가 있어도 당일만 조용할 수 있다
    # (실데이터: 코람코더원리츠 평균 18억 / 당일 7.3억). 그 값이 0억으로 뭉개지면 누락처럼 보인다.
    closes = [100.0] * (DAYS - 1) + [99.0]
    quiet = make("000090", closes, amount=3e10)
    quiet.loc[quiet.index[-1], "Amount"] = 6e7            # 당일만 0.6억
    assert by_code(build([quiet], tmp))["000090"]["amount_eok"] == 0.6

    out2 = build([make("000100", closes, amount=3e10)], tmp)   # 300억 → 정수
    assert by_code(out2)["000100"]["amount_eok"] == 300, by_code(out2)["000100"]


def test_output_is_json_serialisable(tmp):
    out = build([make("000080", [100.0] * (DAYS - 1) + [99.0])], tmp)
    json.loads(tmp.read_text(encoding="utf-8"))                 # 파일이 실제로 파싱되는지
    assert set(out) >= {"updated_at", "counts", "candidates", "history", "params"}, set(out)


if __name__ == "__main__":
    import tempfile
    fails = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "cand.json"
        for name, fn in sorted(globals().items()):
            if not (name.startswith("test_") and callable(fn)):
                continue
            args = (tmp,) if fn.__code__.co_argcount else ()
            try:
                fn(*args)
                print("PASS", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, "->", e)
    print("ALL PASS" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
