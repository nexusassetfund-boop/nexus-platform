"""수급 감지기 편입/편출 이력(_track) 검증.

홈 '연동 전략 편입·편출' 카드가 이 history를 그대로 읽으므로, 여기서 잘못 확정하면
사이트에 없던 편입·편출이 뜬다. 확정 게이트·급감 가드·멱등을 전부 검사한다.
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import flow_screener as fs


def _cand(code, grade="A", name=None, rank=1):
    return {"ticker": code, "name": name or f"종목{code}", "grade": grade, "rank": rank}


def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "STATE_PATH", tmp_path / "flow_state.json")
    monkeypatch.setattr(fs, "HIST_PATH", tmp_path / "flow_members.json")


def _today():
    return dt.datetime.now(tz=fs.KST).strftime("%Y-%m-%d")


def test_first_run_seeds_state_without_history(tmp_path, monkeypatch):
    """최초 실행은 전원이 '편입'으로 쏟아지면 안 된다 — 시드만."""
    _paths(tmp_path, monkeypatch)
    hist = fs._track([_cand("005930"), _cand("000660")], _today())
    assert hist == []
    state = json.loads((tmp_path / "flow_state.json").read_text(encoding="utf-8"))
    assert set(state) == {"005930", "000660"}
    assert not (tmp_path / "flow_members.json").exists()


def test_added_and_removed_recorded(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    today = _today()
    fs._track([_cand("005930"), _cand("000660")], today)
    hist = fs._track([_cand("005930"), _cand("035420", grade="S")], today)
    assert len(hist) == 1
    assert [a["code"] for a in hist[-1]["added"]] == ["035420"]
    assert [r["code"] for r in hist[-1]["removed"]] == ["000660"]
    assert hist[-1]["removed"][0]["last_grade"] == "A"
    assert hist[-1]["date"] == today


def test_only_s_and_a_are_members(tmp_path, monkeypatch):
    """C(기관 단독)·P(연기금)는 백테스트상 역신호 — 편입으로 내보내지 않는다."""
    _paths(tmp_path, monkeypatch)
    today = _today()
    fs._track([_cand("005930")], today)
    hist = fs._track([_cand("005930"), _cand("000660", grade="C"),
                      _cand("035420", grade="P")], today)
    assert hist == []                       # C·P는 편입이 아니므로 이력 없음
    state = json.loads((tmp_path / "flow_state.json").read_text(encoding="utf-8"))
    assert set(state) == {"005930"}


def test_stale_snapshot_freezes(tmp_path, monkeypatch):
    """기준일이 오늘이 아니면(네이버 확정치 미게시) 어제 명단으로 확정하지 않는다."""
    _paths(tmp_path, monkeypatch)
    today = _today()
    fs._track([_cand("005930")], today)
    stale = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    hist = fs._track([_cand("035420")], stale)
    assert hist == []
    state = json.loads((tmp_path / "flow_state.json").read_text(encoding="utf-8"))
    assert set(state) == {"005930"}         # 상태도 동결


def test_collapse_guard_holds_mass_exit(tmp_path, monkeypatch):
    """부분 장애로 명단이 쪼그라들면 대량 편출을 확정하지 않는다."""
    _paths(tmp_path, monkeypatch)
    today = _today()
    many = [_cand(f"{i:06d}") for i in range(10)]
    fs._track(many, today)
    hist = fs._track(many[:2], today)        # 10 → 2 (40% 미만)
    assert hist == []
    state = json.loads((tmp_path / "flow_state.json").read_text(encoding="utf-8"))
    assert len(state) == 10                  # 상태 보존 — 다음 실행이 다시 판단


def test_same_day_rerun_is_idempotent(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    today = _today()
    fs._track([_cand("005930"), _cand("000660")], today)
    fs._track([_cand("005930"), _cand("035420")], today)
    hist = fs._track([_cand("005930"), _cand("035420")], today)
    assert len(hist) == 1                    # 같은 날 줄이 늘지 않는다
    assert [a["code"] for a in hist[-1]["added"]] == ["035420"]
    assert [r["code"] for r in hist[-1]["removed"]] == ["000660"]


def test_history_capped_at_30(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    (tmp_path / "flow_members.json").write_text(
        json.dumps([{"date": f"2026-01-{i:02d}", "added": [], "removed": []}
                    for i in range(1, 32)]), encoding="utf-8")
    assert len(fs._track([_cand("005930")], _today())) == 30
