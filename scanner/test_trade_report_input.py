"""리포트 입력 크기 계약 테스트. 실행: python scanner/test_trade_report_input.py

배경: 8/15 회차가 max-turns 30을 소진해 실패했고, 그 결과 7월 확정치 리포트가
통째로 빠졌다. 입력 3,909줄 중 1,744줄이 imports·discover의 series 원배열이었는데
프롬프트는 이 배열을 쓰지 않는다(amount·yoy·streak 같은 집계값만 읽는다). 원배열이
들어가면 Read가 페이지네이션되고 그만큼 턴을 먹는다. 다시 새어들지 않게 고정한다.
"""
import json
import tempfile
from pathlib import Path

import trade_report as tr


def _prepare_in_tmpdir(tmp: Path) -> dict:
    """모듈 경로를 tmp로 갈아끼우고 prepare()를 돌려 입력 JSON을 돌려준다."""
    series = [{"m": "202606", "amt": 1.0}, {"m": "202607", "amt": 2.0}]
    (tmp / "trade.json").write_text(json.dumps({
        "data_month": "202607",
        "stocks": [{"ticker": "005070", "name": "코스모신소재", "amount": 1.0,
                    "flags": [], "series": series}],
        "macro": {"total": 1.0},
        "imports": [{"hs": "282520", "label": "수산화리튬", "amount": 1.0,
                     "yoy": 1.0, "series": series}],
        "report_input": {"industries": [], "movers": {}},
    }, ensure_ascii=False), encoding="utf-8")
    (tmp / "discover.json").write_text(json.dumps({
        "items": [{"hs": "8542321020", "name": "에스램", "amount": 1.0,
                   "yoy": 1.0, "series": series}],
    }, ensure_ascii=False), encoding="utf-8")

    tr.TRADE_PATH = tmp / "trade.json"
    tr.DISC_PATH = tmp / "discover.json"
    tr.REPORT_PATH = tmp / "report.json"      # 없음 → skip 안 걸림
    tr.EARN_PATH = tmp / "earnings.json"      # 없음 → 발표예정 0건
    tr.INPUT_PATH = tmp / "input.json"
    assert tr.prepare() == 0
    return json.loads(tr.INPUT_PATH.read_text(encoding="utf-8"))


def test_no_raw_series_anywhere():
    with tempfile.TemporaryDirectory() as d:
        inp = _prepare_in_tmpdir(Path(d))
    # 종목은 원래도 series를 안 실었고, imports·discover가 이번에 빠진 쪽이다.
    for key in ("stocks", "imports", "discover"):
        for row in inp[key]:
            assert "series" not in row, f"{key}에 series가 다시 실렸다"


def test_aggregates_survive():
    # 크기를 줄이려다 리포트가 실제로 쓰는 집계값까지 날리면 안 된다.
    with tempfile.TemporaryDirectory() as d:
        inp = _prepare_in_tmpdir(Path(d))
    assert inp["data_month"] == "202607"
    assert inp["imports"][0]["label"] == "수산화리튬"
    assert inp["imports"][0]["yoy"] == 1.0
    assert inp["discover"][0]["name"] == "에스램"
    assert inp["discover"][0]["yoy"] == 1.0
    assert inp["stocks"][0]["ticker"] == "005070"


def test_skip_marker_when_month_already_reported():
    # 같은 확정치로 두 번 쓰지 않는다 — 기존 동작이 깨지지 않았는지 같이 본다.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _prepare_in_tmpdir(tmp)
        tr.REPORT_PATH.write_text(json.dumps({"items": [{"month": "202607"}]}),
                                  encoding="utf-8")
        assert tr.prepare() == 0
        assert json.loads(tr.INPUT_PATH.read_text(encoding="utf-8"))["skip"] is True


def test_skip_still_publishes_existing_report():
    """생성을 건너뛴 회차도 KV 게시는 한다.

    202607이 저장소엔 있고 KV엔 7/30치가 남아 있었다. 리포트를 손으로 커밋하면
    publish를 타지 않는데, 그 뒤 실행은 전부 skip이라 영영 복구되지 않았다.
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _prepare_in_tmpdir(tmp)
        doc = {"updated": "2026-08-18 12:07", "items": [{"month": "202607"}]}
        tr.REPORT_PATH.write_text(json.dumps(doc), encoding="utf-8")
        assert tr.prepare() == 0                       # skip 마커를 남긴다
        pushed = []
        orig, tr._push_kv = tr._push_kv, pushed.append
        try:
            assert tr.publish() == 0
        finally:
            tr._push_kv = orig
        assert pushed == [doc], f"skip 회차가 게시되지 않았다: {pushed}"


def test_skip_without_report_does_not_push():
    # 리포트 자체가 없으면 밀 것도 없다 — 빈 문서를 올려 KV를 지우면 안 된다.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _prepare_in_tmpdir(tmp)
        tr.INPUT_PATH.write_text(json.dumps({"skip": True}), encoding="utf-8")
        pushed = []
        orig, tr._push_kv = tr._push_kv, pushed.append
        try:
            assert tr.publish() == 0
        finally:
            tr._push_kv = orig
        assert pushed == [], "리포트가 없는데 게시했다"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("모두 통과")
