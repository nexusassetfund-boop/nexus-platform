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


def make(code, closes, highs=None, market="KOSPI", amount=1e10, marcap=1e12):
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


def filler(n=10):
    """RS 하한(MIN_RS)을 넘기기 위한 들러리 종목.

    RS는 그날 전 종목 수익률 백분위라, 합성 데이터에 종목이 2개뿐이면 한쪽이 50점을
    받아 게이트에 걸린다. 크게 하락하는 들러리를 깔아 실제 검증 대상이 상위에 오게 한다.
    (들러리는 gap이 커서 후보에는 들어오지 않는다)
    """
    return [make(f"9000{i:02d}", [200.0 - i - k * (100 / DAYS) for k in range(DAYS)])
            for i in range(n)]


def build(frames, tmp, pad=True):
    """OUT_CAND를 임시 경로로 돌려놓고 build_candidates 실행."""
    frames = list(frames) + (filler() if pad else [])
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


def test_entry_is_a_same_day_snapshot(tmp):
    # 편입은 '그날 gap이 후보 범위(12%)에 들어왔는가'로 판정한다.
    # 과거에 문턱에 닿았어도 오늘 멀어졌으면(끌고 가는 기간도 지났으면) 빠진다.
    old_touch = [100.0] * (DAYS - nf.WATCH_WINDOW)
    faded = [70.0] * nf.WATCH_WINDOW                 # gap = (100-70)/70 = 42.9%
    assert by_code(build([make("000050", old_touch + faded)], tmp)) == {}, "멀어진 종목이 남았다"

    near_now = [100.0] * (DAYS - 1) + [95.0]         # gap = (100-95)/95 = 5.26% → 근접
    r = by_code(build([make("000060", near_now)], tmp))["000060"]
    assert r["status"] == "near", r


def test_carry_keeps_a_stock_briefly_after_it_drifts_out(tmp):
    # 편입 다음날 gap이 상한을 넘어도 CARRY_DAYS 안이면 남긴다 (참고 화면에도 12% 초과가 섞여 있다)
    base = [100.0] * (DAYS - 2)
    drifted = base + [95.0, 88.0]                    # 어제 5.26%(편입) → 오늘 13.6%
    got = by_code(build([make("000070", drifted)], tmp))
    assert "000070" in got, "편입 직후 이탈인데 곧바로 빠졌다"
    assert got["000070"]["gap"] > nf.GAP_WATCH


def test_carry_expires(tmp):
    # 편입 후 CARRY_DAYS 를 넘겨 계속 멀어져 있으면 결국 빠진다
    tail = [88.0] * (nf.CARRY_DAYS + 3)
    closes = [100.0] * (DAYS - 1 - len(tail)) + [95.0] + tail
    assert "000080" not in by_code(build([make("000080", closes)], tmp))


def test_kosdaq_included_and_history_shape(tmp):
    closes = [100.0] * (DAYS - 1) + [99.0]
    out = build([make("000070", closes, market="KOSDAQ")], tmp)
    r = by_code(out)["000070"]
    assert r["market"] == "KOSDAQ", r
    assert len(r["spark"]) == nf.SPARK_BARS, len(r["spark"])
    hist = out["history"]["000070"]
    assert hist and len(hist[0]) == 4, hist[:1]
    assert hist[-1][0] == out["data_last_date"], hist[-1]      # 최근 항목이 마지막 거래일


def test_one_day_spike_cannot_bypass_the_average_gate(tmp):
    # 평소 거래가 없다가 오늘만 크게 터진 종목. 당일 거래대금 게이트는 넘지만 20일 평균이
    # 낮아 탈락해야 한다 — 평균에 당일을 포함하면(shift 없이) 스스로 평균을 끌어올려 통과한다.
    closes = [100.0] * (DAYS - 1) + [99.0]
    thin = make("000140", closes, amount=nf.MIN_AMOUNT_AVG20 / 5)
    thin.loc[thin.index[-1], "Amount"] = nf.MIN_AMOUNT_AVG20 * 50
    assert "000140" not in by_code(build([thin], tmp))


def test_liquidity_floors_are_two_separate_gates(tmp):
    # 평소 유동성(20일 평균)과 당일 거래대금을 각각 본다 — 둘 다 넘어야 통과한다.
    closes = [100.0] * (DAYS - 1) + [99.0]
    ok = make("000160", closes, amount=nf.MIN_AMOUNT_TODAY)          # 둘 다 하한 이상
    thin_avg = make("000170", closes, amount=nf.MIN_AMOUNT_AVG20 * 0.9)   # 평균 미달
    thin_today = make("000175", closes, amount=nf.MIN_AMOUNT_TODAY * 2)
    thin_today.loc[thin_today.index[-1], "Amount"] = nf.MIN_AMOUNT_TODAY * 0.9  # 당일만 미달
    got = by_code(build([ok, thin_avg, thin_today], tmp))
    assert "000160" in got, "두 하한을 다 넘었는데 빠졌다"
    assert "000170" not in got, "20일 평균 미달이 통과했다"
    assert "000175" not in got, "당일 거래대금 미달이 통과했다"


def test_emits_avg20_denominator_for_intraday(tmp):
    # 워커가 장중 배수를 다시 계산하려면 분모(직전 20일 평균)가 산출물에 있어야 한다
    closes = [100.0] * (DAYS - 1) + [99.0]
    r = by_code(build([make("000180", closes, amount=3e10)], tmp))["000180"]
    assert r["amount_avg20_eok"] == 300.0, r
    assert abs(r["amount_eok"] / r["amount_avg20_eok"] - r["amt_vs20"]) < 0.1, r


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


def test_trading_value_is_an_integer_after_the_gate(tmp):
    # 당일 거래대금 게이트(50억)를 통과한 종목만 오므로 억 단위 정수로 충분하다.
    # 장중 소수 표기(누적 6천만원 → 0.6억)는 워커 쪽 책임이라 여기서 다루지 않는다.
    closes = [100.0] * (DAYS - 1) + [99.0]
    r = by_code(build([make("000100", closes, amount=3e10)], tmp))["000100"]
    assert r["amount_eok"] == 300 and isinstance(r["amount_eok"], int), r


def test_output_is_json_serialisable(tmp):
    out = build([make("000080", [100.0] * (DAYS - 1) + [99.0])], tmp)
    json.loads(tmp.read_text(encoding="utf-8"))                 # 파일이 실제로 파싱되는지
    assert set(out) >= {"updated_at", "counts", "candidates", "history", "params"}, set(out)


def test_daily_accumulates_past_lists(tmp):
    """일자별 명단은 그날 기준으로 다시 계산된다 — 오늘 명단에서 빠진 종목도 그날엔 남아야 한다."""
    # A: 계속 문턱 근처 → 매일 후보
    a = [100.0] * (DAYS - 1) + [96.0]
    # B: 과거엔 문턱 근처였다가 최근 크게 밀림 → 오늘 명단엔 없지만 과거 일자엔 있어야 한다
    faded = [70.0] * 20
    b = [100.0] * (DAYS - 1 - len(faded)) + [96.0] + faded
    out = build([make("000210", a), make("000220", b)], tmp)

    today = {r["code"] for r in out["candidates"]}
    assert "000210" in today and "000220" not in today, sorted(today)

    daily = out["daily"]
    assert daily, "일자별 명단이 비었다"
    assert out["data_last_date"] not in daily, "오늘은 candidates 가 담당한다 — daily 에 중복 저장하면 안 된다"
    ci = out["daily_cols"].index("code")
    seen_b = [dt for dt, v in daily.items() if any(x[ci] == "000220" for x in v["items"])]
    assert seen_b, "오늘 빠진 종목이 과거 일자에도 없다 — 누적이 안 되고 있다"

    # 과거 항목은 배열 + meta 로 싣는다 (dict로 담으면 키 이름만 수백 KB)
    one = next(iter(daily.values()))["items"][0]
    assert isinstance(one, list) and len(one) == len(out["daily_cols"]), one
    assert out["meta"]["000220"]["name"], out["meta"].get("000220")


def test_daily_counts_match_items(tmp):
    closes = [100.0] * (DAYS - 1) + [96.0]
    out = build([make("000230", closes)], tmp)
    for dt, v in out["daily"].items():
        n = sum(v["counts"][s] for s in ("breaking", "imminent", "near", "watch", "touched_failed"))
        assert n == len(v["items"]), f"{dt}: counts {v['counts']} vs items {len(v['items'])}"


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
