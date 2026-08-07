# -*- coding: utf-8 -*-
"""섹터 캐시 폴백 — 회귀 테스트.

버그(2026-08-07): 섹터의 유일한 출처인 FDR KRX-DESC가 GitHub Actions 러너에서만
HTTP 404를 내기 시작하자 scan.json 350종목의 sector가 전부 빈 문자열이 됐다.
경고 한 줄만 남고 스캔은 '성공'으로 끝나 프론트에 '-'가 뜨기 전까지 아무도 몰랐다.
(같은 시각 국내 IP에서는 동일 호출이 정상 — 라이브러리 버전 문제가 아니었다.)

유니버스 캐시와 같은 방식으로 마지막 정상값을 재사용하는지 검사한다.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_scan  # noqa: E402


def _use_tmp_cache(tmp_path, payload=None):
    """_SECTOR_CACHE 를 임시 파일로 돌려놓고 경로를 돌려준다."""
    p = tmp_path / "sector_cache.json"
    if payload is not None:
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    run_scan._SECTOR_CACHE = p
    return p


def test_save_then_load_roundtrip(tmp_path):
    _use_tmp_cache(tmp_path)
    run_scan._save_sector_cache({"005930": "반도체", "000810": "보험업"})
    assert run_scan._load_sector_cache() == {"005930": "반도체", "000810": "보험업"}


def test_load_missing_file_is_empty(tmp_path):
    run_scan._SECTOR_CACHE = tmp_path / "없는파일.json"
    assert run_scan._load_sector_cache() == {}


def test_load_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "sector_cache.json"
    p.write_text("{깨진 json", encoding="utf-8")
    run_scan._SECTOR_CACHE = p
    assert run_scan._load_sector_cache() == {}


def test_blank_sectors_are_dropped(tmp_path):
    # 빈 값이 캐시에 섞여 들어가도 로드 때 걸러야 한다 — 빈 섹터를 되살리면 폴백이 무의미
    _use_tmp_cache(tmp_path, {"sectors": {"005930": "반도체", "000810": ""}})
    assert run_scan._load_sector_cache() == {"005930": "반도체"}


def test_fdr_failure_falls_back_to_cache(monkeypatch, tmp_path):
    """FDR이 404를 내도 캐시가 있으면 섹터가 채워진다 — 이 버그의 핵심."""
    _use_tmp_cache(tmp_path, {"sectors": {"005930": "반도체", "000810": "보험업"}})
    monkeypatch.setattr(run_scan, "_fdr_desc_rows", lambda: (_ for _ in ()).throw(
        Exception("HTTP Error 404: Not Found")))
    _tmap, sector_map, _ = run_scan._sector_and_names()
    assert sector_map == {"005930": "반도체", "000810": "보험업"}


def test_fresh_fetch_refreshes_cache(monkeypatch, tmp_path):
    """조회에 성공하면 캐시를 갱신한다 — 다음 실패 때 쓸 값이 최신이어야 한다."""
    p = _use_tmp_cache(tmp_path, {"sectors": {"005930": "옛섹터"}})
    monkeypatch.setattr(run_scan, "_fdr_desc_rows",
                        lambda: [("005930", "삼성전자", "반도체")])
    _tmap, sector_map, _ = run_scan._sector_and_names()
    assert sector_map["005930"] == "반도체"
    assert json.loads(p.read_text(encoding="utf-8"))["sectors"]["005930"] == "반도체"


def test_partial_fetch_is_topped_up_from_cache(monkeypatch, tmp_path):
    """일부만 온 경우 빈 곳만 캐시로 보충하고, 새로 온 값이 우선한다."""
    _use_tmp_cache(tmp_path, {"sectors": {"005930": "옛섹터", "000810": "보험업"}})
    monkeypatch.setattr(run_scan, "_fdr_desc_rows",
                        lambda: [("005930", "삼성전자", "반도체")])
    _tmap, sector_map, _ = run_scan._sector_and_names()
    assert sector_map["005930"] == "반도체"      # 새 값 우선
    assert sector_map["000810"] == "보험업"      # 빠진 건 캐시로 보충


def test_seeded_cache_covers_scan_universe():
    """저장소에 커밋된 시드 캐시가 실제로 쓸 만한지 — 비면 폴백이 무의미하다."""
    real = Path(__file__).resolve().parents[1] / "docs" / "data" / "sector_cache.json"
    assert real.exists(), "sector_cache.json 시드가 커밋돼 있어야 한다"
    sectors = json.loads(real.read_text(encoding="utf-8"))["sectors"]
    assert len(sectors) > 2000, f"시드가 너무 적다: {len(sectors)}"
    assert sectors.get("005930"), "삼성전자 섹터가 있어야 한다"
