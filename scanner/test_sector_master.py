# -*- coding: utf-8 -*-
"""산업분류 마스터(엑셀 유래) — 회귀 테스트.

섹터 출처가 두 겹이다. 아래(KRX 표준산업분류, FDR→캐시 폴백) 위에 마스터를 덮는다.
검사 포인트:
  - 마스터가 KRX 분류를 덮는가 / 마스터에 없는 종목은 KRX 분류를 지키는가
  - 마스터로 덮은 값이 캐시에 새어 들어가지 않는가 (캐시가 오염되면 폴백이 망가진다)
  - 커밋된 sector_master.json 이 실제로 유니버스를 덮는가
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_sector_master  # noqa: E402
import run_scan  # noqa: E402
import sector_master  # noqa: E402


@pytest.fixture
def fake_master(monkeypatch):
    """마스터 내용을 갈아끼운다 — level1() 은 load() 위에 얹혀 있으므로 load() 만 바꾼다."""
    def _set(d):
        monkeypatch.setattr(sector_master, "load", lambda: d)
    return _set


def _tmp_cache(tmp_path, payload=None):
    p = tmp_path / "sector_cache.json"
    if payload is not None:
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    run_scan._SECTOR_CACHE = p
    return p


# ── 코드 정규화 ──
def test_norm_code_pads_numeric():
    # 엑셀이 코드를 숫자로 저장해 앞의 0이 날아간다 — 5930 → 005930
    assert build_sector_master.norm_code(5930) == "005930"
    assert build_sector_master.norm_code(660) == "000660"


def test_norm_code_keeps_alphanumeric():
    # 신규 상장분은 영숫자 혼합 — zfill 하면 안 된다
    assert build_sector_master.norm_code("0126Z0") == "0126Z0"
    assert build_sector_master.norm_code("0156t0") == "0156T0"


# ── 로더 ──
def test_level1_takes_first_column(fake_master):
    fake_master({"005930": ["반도체", "메모리반도체", "DRAM, NAND 등"]})
    assert sector_master.level1() == {"005930": "반도체"}


def test_missing_master_file_is_harmless(monkeypatch, tmp_path):
    monkeypatch.setattr(sector_master, "PATH", tmp_path / "없는파일.json")
    sector_master.reset()
    try:
        assert sector_master.load() == {}
    finally:
        sector_master.reset()


def test_corrupt_master_file_is_harmless(monkeypatch, tmp_path):
    p = tmp_path / "sector_master.json"
    p.write_text("{깨진 json", encoding="utf-8")
    monkeypatch.setattr(sector_master, "PATH", p)
    sector_master.reset()
    try:
        assert sector_master.load() == {}
    finally:
        sector_master.reset()


def test_blank_level1_rows_are_dropped(monkeypatch, tmp_path):
    # 대분류가 빈 행이 섞이면 섹터를 빈 문자열로 덮어써 KRX 분류까지 지워버린다
    p = tmp_path / "sector_master.json"
    p.write_text(json.dumps({"stocks": {"005930": ["반도체", "", ""], "000660": ["", "", ""]}},
                            ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sector_master, "PATH", p)
    sector_master.reset()
    try:
        assert set(sector_master.load()) == {"005930"}
    finally:
        sector_master.reset()


# ── run_scan 오버레이 ──
def test_master_overrides_krx_industry(monkeypatch, tmp_path, fake_master):
    _tmp_cache(tmp_path, {"sectors": {}})
    fake_master({"005930": ["반도체", "메모리반도체", "DRAM, NAND 등"]})
    monkeypatch.setattr(run_scan, "_fdr_desc_rows",
                        lambda: [("005930", "삼성전자", "통신 및 방송 장비 제조업")])
    _t, sector_map, _ = run_scan._sector_and_names()
    assert sector_map["005930"] == "반도체"


def test_uncovered_code_keeps_krx_industry(monkeypatch, tmp_path, fake_master):
    _tmp_cache(tmp_path, {"sectors": {}})
    fake_master({"005930": ["반도체", "메모리반도체", ""]})
    monkeypatch.setattr(run_scan, "_fdr_desc_rows", lambda: [
        ("005930", "삼성전자", "통신 및 방송 장비 제조업"),
        ("123456", "미커버종목", "기타 화학제품 제조업"),
    ])
    _t, sector_map, _ = run_scan._sector_and_names()
    assert sector_map["123456"] == "기타 화학제품 제조업"


def test_master_does_not_pollute_cache(monkeypatch, tmp_path, fake_master):
    """캐시는 FDR 폴백용이다 — 마스터 값이 새어 들어가면 마스터를 지워도 되돌릴 수 없다."""
    p = _tmp_cache(tmp_path, {"sectors": {}})
    fake_master({"005930": ["반도체", "메모리반도체", ""]})
    monkeypatch.setattr(run_scan, "_fdr_desc_rows",
                        lambda: [("005930", "삼성전자", "통신 및 방송 장비 제조업")])
    run_scan._sector_and_names()
    assert json.loads(p.read_text(encoding="utf-8"))["sectors"]["005930"] == "통신 및 방송 장비 제조업"


def test_master_adds_codes_missing_from_fdr(monkeypatch, tmp_path, fake_master):
    """엑셀이 FDR보다 최신인 신규 상장분(영숫자 코드)도 섹터가 붙어야 한다."""
    _tmp_cache(tmp_path, {"sectors": {}})
    fake_master({"0126Z0": ["헬스케어", "바이오시밀러", "바이오시밀러"]})
    monkeypatch.setattr(run_scan, "_fdr_desc_rows",
                        lambda: [("005930", "삼성전자", "통신 및 방송 장비 제조업")])
    _t, sector_map, _ = run_scan._sector_and_names()
    assert sector_map["0126Z0"] == "헬스케어"


# ── 종목명: 실시간이 우선, 엑셀은 폴백 + 드리프트 감지 ──
@pytest.fixture
def fake_names(monkeypatch):
    def _set(d):
        monkeypatch.setattr(sector_master, "names", lambda: d)
    return _set


def test_name_drift_never_touches_names(fake_names):
    """엑셀은 스냅샷이다 — 사명변경이 나면 옛 이름이 되살아나므로 절대 덮거나 채우지 않는다."""
    fake_names({"005930": "옛이름전자", "0126Z0": "삼성에피스홀딩스"})
    tmap = {"005930": "삼성전자"}
    run_scan._report_name_drift(tmap)
    assert tmap == {"005930": "삼성전자"}   # 덮지도, 0126Z0 을 채우지도 않았다


def test_name_drift_is_logged(fake_names, caplog):
    """이름이 어긋나면 경고로 남긴다 — 엑셀 갱신 대상 목록이다."""
    fake_names({"005930": "옛이름전자"})
    with caplog.at_level("WARNING"):
        drift = run_scan._report_name_drift({"005930": "삼성전자"})
    assert drift == [("005930", "삼성전자", "옛이름전자")]
    assert "종목명 불일치" in caplog.text
    assert "삼성전자→옛이름전자" in caplog.text


def test_unscanned_code_is_not_drift(fake_names, caplog):
    """스캔 유니버스 밖 종목(2,200여개)을 불일치로 세면 매 실행마다 가짜 경고가 뜬다."""
    fake_names({"0126Z0": "삼성에피스홀딩스"})
    with caplog.at_level("WARNING"):
        assert run_scan._report_name_drift({}) == []
    assert "종목명 불일치" not in caplog.text


def test_matching_names_are_not_drift(fake_names):
    fake_names({"005930": "삼성전자"})
    assert run_scan._report_name_drift({"005930": "삼성전자"}) == []


def test_empty_master_names_is_noop(fake_names):
    fake_names({})
    assert run_scan._report_name_drift({"005930": "삼성전자"}) == []


# ── 커밋된 실제 마스터 ──
def test_committed_master_covers_universe():
    sector_master.reset()
    stocks = sector_master.load()
    assert len(stocks) > 2000, f"마스터가 너무 적다: {len(stocks)}"
    assert stocks["005930"][0] == "반도체"
    # 대분류는 필수, 중분류·주요제품은 비어 있어도 된다
    assert all(len(v) == 3 and v[0] for v in stocks.values())
    # 대분류가 폭발하면 필터로 못 쓴다 — 엑셀 스냅샷 기준 29종
    assert len({v[0] for v in stocks.values()}) <= 40


def test_committed_master_codes_are_wellformed():
    import re
    bad = [c for c in sector_master.load() if not re.fullmatch(r"\d[0-9A-Z]{5}", c)]
    assert not bad, f"코드 형식 위반: {bad[:10]}"


def test_committed_master_has_names_for_every_stock():
    sector_master.reset()
    assert set(sector_master.names()) == set(sector_master.load())
    assert sector_master.names()["005930"] == "삼성전자"


def test_committed_master_covers_seeded_sector_cache():
    """마스터가 실제 스캔 유니버스를 얼마나 덮는지 — 커버리지가 무너지면 옛 분류로 되돌아간다."""
    cache = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "data" / "sector_cache.json")
        .read_text(encoding="utf-8"))["sectors"]
    covered = sum(1 for c in cache if c in sector_master.load())
    assert covered / len(cache) > 0.85, f"커버리지 {covered}/{len(cache)}"
