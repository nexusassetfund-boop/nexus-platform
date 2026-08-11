"""수급 감지기 코어 검증 — 흠집 허용 스트릭이 요구사항대로 동작하는지."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flow_history import join_detail, metrics, reversal, side_metrics, tolerance_streak


def test_clean_streak():
    assert tolerance_streak([-5, 10, 10, 10, 10]) == (4, 0)


def test_noise_sell_does_not_reset():
    """요구사항의 그 케이스 — 4일 10만주 매수 뒤 1만주 매도. 스트릭이 살아야 한다."""
    nets = [100_000, 100_000, -10_000, 100_000, 100_000]
    streak, blemish = tolerance_streak(nets)
    assert streak == 5 and blemish == 1, (streak, blemish)
    assert tolerance_streak(nets)[0] > 2      # 무관용이면 2에서 끊긴다


def test_real_sell_breaks():
    """40만주 매도는 노이즈가 아니다 — 끊겨야 한다."""
    nets = [100_000, 100_000, -400_000, 100_000, 100_000]
    assert tolerance_streak(nets) == (2, 0)


def test_blemish_cap():
    """흠집 3번이면 세 번째에서 중단 (MAX_BLEMISH=2)."""
    nets = [50_000, -1_000, 50_000, -1_000, 50_000, -1_000, 50_000]
    streak, blemish = tolerance_streak(nets)
    assert blemish == 2 and streak == 5, (streak, blemish)


def test_negative_cumulative_rejected():
    """흠집 허용이 누적 순매도를 순매수로 둔갑시키면 안 된다."""
    assert tolerance_streak([-100]) == (0, 0)
    assert tolerance_streak([])[0] == 0


def test_side_metrics():
    rows = [{"date": f"2026080{i}", "close": 100, "volume": 1000,
             "frgn": 100, "inst": -50} for i in range(1, 6)]
    m = side_metrics(rows, "frgn", window=5)
    assert m["intensity"] == 500 / 5000
    assert m["persist"] == 1.0
    assert m["streak"] == 5
    assert side_metrics(rows, "inst", window=5)["streak"] == 0


def test_metrics_needs_full_window():
    rows = [{"date": "20260801", "close": 1, "volume": 1, "frgn": 1, "inst": 1}]
    assert metrics(rows, window=10) is None


def test_reversal_detects_flip():
    """5일 연속 매도 후 2일 순매수 전환 → (전환 2일, 직전 매도 5일)."""
    nets = [-100.0] * 5 + [50.0, 60.0]
    assert reversal(nets) == (2, 5)


def test_reversal_rejects_long_buy_run():
    """순매수 4일째면 '전환 시점'이 아니다 (REV_MAX_FLIP=3) — 스트릭 후보 영역."""
    nets = [-100.0] * 5 + [50.0] * 4
    assert reversal(nets) == (0, 0)


def test_reversal_rejects_no_prior_selling():
    """직전에 매도 스트릭이 없으면 전환이 아니다."""
    flip, sell = reversal([100.0, 100.0, 50.0])
    assert sell == 0


def test_reversal_sell_streak_tolerates_noise_buy():
    """매도 스트릭도 흠집 허용 — 노이즈 매수일 하루가 매도 연속을 끊지 않는다."""
    nets = [-100.0, -100.0, 10.0, -100.0, -100.0, 50.0]
    flip, sell = reversal(nets)
    assert flip == 1 and sell == 5, (flip, sell)


def test_join_detail():
    rows = [{"date": f"2026080{i}", "close": 100, "volume": 1000,
             "frgn": 0, "inst": 100} for i in range(1, 6)]
    detail = {f"2026080{i}": {"fin": 70, "pension": 30} for i in range(1, 6)}
    assert join_detail(rows, detail, window=5)
    assert rows[-1]["inst_xf"] == 30 and rows[-1]["pension"] == 30
    assert side_metrics(rows, "pension", window=5)["streak"] == 5
    # 창에 구멍이 있으면 병합 거부
    rows2 = [dict(r) for r in rows]
    assert not join_detail(rows2, {k: v for k, v in detail.items() if k != "20260803"}, window=5)


def test_concentration_flags_one_day_event():
    """지수 리밸런싱형: 하루에 몰린 수급은 concentr이 1에 가깝다."""
    rows = [{"date": f"2026080{i}", "close": 100, "volume": 1000,
             "frgn": (900 if i == 5 else 10), "inst": 0} for i in range(1, 6)]
    assert side_metrics(rows, "frgn", window=5)["concentr"] > 0.9


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
    print("실패" if fails else "전부 통과")
    sys.exit(1 if fails else 0)
