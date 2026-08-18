"""주간 복기 입력 계약 테스트. 실행: python scanner/test_review_data.py

배경: run_scan._update_track_nav가 7/28 리셋 때 track_nav를 {date, nav}(기준가 1000)
에서 {date, cum}(누적수익률 %, 기점 0%)으로 바꿨는데 review_data가 따라가지 않아
7/31·8/7·8/14 복기가 3주 연속 KeyError: 'nav'로 죽었다. 아무도 안 봐서 3주가 갔다.
스키마가 또 갈리면 금요일 크론이 아니라 여기서 먼저 깨지게 고정한다.
"""
import json
import sys
import tempfile
from pathlib import Path

import review_data as rd


def _run(tmp: Path, date: str) -> dict:
    """track_nav 곡선과 원장을 심어 두고 review_data.main()을 돌린다."""
    (tmp / "tracking.json").write_text(json.dumps({
        "nav_base": "2026-07-28",
        # 기점 0% → 주 시작 전 +10% → 주중 +21%(= 주간 +10%)
        "track_nav": {"1": [{"date": "2026-07-28", "cum": 0.0},
                            {"date": "2026-08-07", "cum": 10.0},
                            {"date": "2026-08-14", "cum": 21.0}]},
        "holdings": [{"ticker": "000001", "entry_date": "2026-08-11"},
                     {"ticker": "000002", "entry_date": "2026-08-17"}],  # 다음 주 진입
        "exited": [{"ticker": "000003", "exit_date": "2026-08-12", "return_pct": -5.0},
                   {"ticker": "000004", "exit_date": "2026-08-17"}],     # 다음 주 청산
        "stats": {},
    }, ensure_ascii=False), encoding="utf-8")
    (tmp / "scan.json").write_text(json.dumps({"results": []}), encoding="utf-8")

    rd.DATA = tmp
    rd.OUT = tmp / "review_input.json"
    rd._index_weekly = lambda *a, **k: None      # 네트워크 차단
    sys.argv = ["review_data.py", "--date", date]
    rd.main()
    return json.loads(rd.OUT.read_text(encoding="utf-8"))


def test_reads_cum_schema():
    with tempfile.TemporaryDirectory() as d:
        out = _run(Path(d), "2026-08-14")
    t = out["tracks"]["1"]
    # 누적은 곡선 값 그대로, 주간은 복리 비율 — 뺄셈(11.0)이 아니라 (1.21/1.10-1)=10.0
    assert t["since_inception_pct"] == 21.0, t
    assert t["weekly_pct"] == 10.0, t
    assert t["week_series"] == [{"date": "2026-08-14", "cum": 21.0}], t


def test_week_window_is_closed_at_both_ends():
    # --date로 지난 주를 복구할 때 그 뒤 매매가 딸려오면 안 된다.
    with tempfile.TemporaryDirectory() as d:
        out = _run(Path(d), "2026-08-14")
    assert [h["ticker"] for h in out["new_entries_this_week"]] == ["000001"], out
    assert [e["ticker"] for e in out["exits_this_week"]] == ["000003"], out


def test_skip_when_no_points_in_week():
    # 휴장 주간은 빈 리포트를 쓰지 않고 skip 마커를 남긴다(기존 동작).
    with tempfile.TemporaryDirectory() as d:
        out = _run(Path(d), "2026-07-24")
    assert out.get("skip") is True, out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("모두 통과")
