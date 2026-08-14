# -*- coding: utf-8 -*-
"""산업분류 마스터 로더 — 섹터의 단일 출처.

data/sector_master.json 은 build_sector_master.py 가 산업분류 엑셀에서 만든다.
    {"stocks": {"005930": ["반도체", "메모리반도체", "DRAM, NAND 등"]},
     "names":  {"005930": "삼성전자"}}

KRX 표준산업분류("그외 기타 운송장비 제조업")보다 투자 관점 분류("방산")가 쓸모 있어서
FDR 값을 이걸로 덮는다. 마스터에 없는 종목은 KRX 분류를 그대로 쓴다.

읽는 쪽이 둘(run_scan, newhigh_fetcher)이라 로더를 한 곳에 둔다 — 섹터가 조용히
빈 값이 되는 사고가 이미 한 번 있었고, 폴백 경로가 갈라지면 또 못 잡는다.
"""
import functools
import json
import logging
from pathlib import Path

PATH = Path(__file__).resolve().parent / "data" / "sector_master.json"
logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _raw() -> dict:
    try:
        return json.loads(PATH.read_text("utf-8"))
    except Exception as e:
        logger.warning("산업분류 마스터 로드 실패 — KRX 분류 사용: %s", e)
        return {}


@functools.lru_cache(maxsize=1)
def load() -> dict:
    """{code: [대분류, 중분류, 주요제품]}. 파일이 없거나 깨졌으면 빈 dict.
    스캔 루프에서 종목마다 부르므로 캐시가 필요하다."""
    d = _raw().get("stocks") or {}
    return {str(k): list(v) for k, v in d.items() if v and v[0]}


def level1() -> dict:
    """{code: 대분류} — sector 필드를 덮어쓸 때 쓴다."""
    return {c: v[0] for c, v in load().items()}


@functools.lru_cache(maxsize=1)
def names() -> dict:
    """{code: 종목명}. 엑셀은 스냅샷이라 KRX/FDR 실시간 값보다 **뒤**다.
    최후 폴백과 드리프트 감지(엑셀이 낡았는지)에만 쓴다 — 덮어쓰기 금지."""
    d = _raw().get("names") or {}
    return {str(k): str(v) for k, v in d.items() if v}


def reset() -> None:
    """캐시 전부 비우기 — PATH 를 바꾼 뒤 다시 읽게 할 때(테스트)."""
    for f in (_raw, load, names):
        f.cache_clear()
