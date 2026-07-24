# -*- coding: utf-8 -*-
"""consensus 순수 파서 검증. 실행: python scanner/test_consensus.py"""
import io
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import consensus


def test_parse_fixture():
    html = io.open(Path(__file__).parent / "fixtures/wise_highlight_000660.html", encoding="utf-8").read()
    out = consensus._parse_consensus(html)
    assert out is not None
    assert out["fwd_eps"] == 314787, out
    assert out["fwd_year"] == "2026/12(E)", out


def test_parse_garbage_fail_closed():
    # 구조가 깨지면 오답 대신 None (fail-closed)
    assert consensus._parse_consensus("") is None
    assert consensus._parse_consensus("<table><tr><th>주요지표</th></tr></table>") is None
    assert consensus._parse_consensus("<html>완전 다른 페이지</html>") is None


def test_parse_no_estimate_column():
    # (E) 컬럼이 없으면(컨센서스 미제공 종목) None
    html = '<table><tr><th>주요지표</th><th>2025/12(A)</th></tr><tr><th>EPS</th><td>1,000원</td></tr></table>'
    assert consensus._parse_consensus(html) is None


def test_parse_multi_fixture():
    html = io.open(Path(__file__).parent / "fixtures/wise_cf1001_000660.html", encoding="utf-8").read()
    out = consensus._parse_consensus_multi(html)
    assert len(out) == 3, out
    assert out[0] == {"year": "2026/12(E)", "eps": 314786.61}, out
    assert out[1] == {"year": "2027/12(E)", "eps": 436642.46}, out
    assert out[2]["year"] == "2028/12(E)", out


def test_parse_multi_fail_closed():
    assert consensus._parse_consensus_multi("") == []
    assert consensus._parse_consensus_multi("<html>다른 페이지</html>") == []
    # (E) 헤더는 있는데 bgE 추정 셀이 없으면 빈 리스트
    html = '<table><tr><th>2026/12(E)</th></tr><tr><th>EPS</th><td class="num">1,000</td></tr></table>'
    assert consensus._parse_consensus_multi(html) == []


if __name__ == "__main__":
    test_parse_fixture()
    test_parse_garbage_fail_closed()
    test_parse_no_estimate_column()
    test_parse_multi_fixture()
    test_parse_multi_fail_closed()
    print("OK")
