"""
발굴 품목 주석 준비·병합 — "혼합기 +1,684%"를 행동 가능한 정보로 만든다.

HS 품목명은 관세 분류 용어라 그 자체로는 무엇인지 알 수 없고, 어느 회사와 관련되는지도
없다. Claude가 품목 설명(desc)과 후보 상장사(candidates)를 달아 준다. 후보는 LLM 지식
기반 추정이므로 화면에 '검증 전'으로 표시되고, 확정 매핑은 기존 검증 파이프라인을 거친다.

  prepare : trade_discover.json에서 주석 없는 품목만 추려 입력을 만든다.
            전부 주석돼 있으면 skip 마커 — Claude 호출을 아낀다.
  publish : 출력을 검증해 trade_discover.json에 병합한다. 주석은 (hs) 단위로
            annotations_cache.json에 캐시해 다음 회차에 재사용한다.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("disc_ann")

ROOT = Path(__file__).parent.parent
DISC_PATH = ROOT / "docs" / "data" / "trade_discover.json"
CACHE_PATH = Path(__file__).parent / "data" / "discover_annotations.json"
INPUT_PATH = ROOT / "discover_annotate_input.json"
OUTPUT_PATH = ROOT / "discover_annotate_output.json"


def _load(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("%s 로드 실패: %s", p.name, e)
        return default


def _apply_cache(disc: dict, cache: dict) -> int:
    """캐시된 주석을 items에 입힌다. 반환: 주석 없는 항목 수."""
    missing = 0
    for it in disc.get("items", []):
        ann = cache.get(it["hs"])
        if ann:
            it["desc"] = ann.get("desc")
            it["candidates"] = ann.get("candidates") or []
        elif not it.get("desc"):
            missing += 1
    return missing


def prepare() -> int:
    disc = _load(DISC_PATH, None)
    if not disc or not disc.get("items"):
        logger.error("trade_discover.json 없음 — 발굴 스캔이 먼저 돌아야 한다")
        return 1
    cache = _load(CACHE_PATH, {})
    missing = _apply_cache(disc, cache)
    # 캐시만으로 채워진 것도 파일에 반영해 둔다(Claude 실패해도 기존 주석은 산다)
    DISC_PATH.write_text(json.dumps(disc, ensure_ascii=False, indent=1), encoding="utf-8")
    if missing == 0:
        INPUT_PATH.write_text(json.dumps({"skip": True, "reason": "전 품목 주석 완료"},
                                         ensure_ascii=False), encoding="utf-8")
        logger.info("skip — 30개 전부 캐시에 있음")
        return 0
    todo = [{k: it.get(k) for k in ("hs", "name", "amount", "yoy", "mom")}
            for it in disc["items"] if not it.get("desc")]
    INPUT_PATH.write_text(json.dumps({"items": todo}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    logger.info("주석 필요 %d개 (캐시 적중 %d)", len(todo), len(disc["items"]) - len(todo))
    return 0


def publish() -> int:
    inp = _load(INPUT_PATH, {})
    if inp.get("skip"):
        logger.info("skip 마커 — 병합 생략")
        return 0
    out = _load(OUTPUT_PATH, None)
    if not out or not out.get("annotations"):
        logger.error("출력 없음 — 주석 생성 실패 (기존 주석은 유지)")
        return 1

    asked = {i["hs"] for i in inp.get("items", [])}
    cache = _load(CACHE_PATH, {})
    added = 0
    for a in out["annotations"]:
        hs = a.get("hs")
        if hs not in asked:          # 요청하지 않은 HS는 무시 — 창작 방지
            continue
        cands = []
        for cd in (a.get("candidates") or [])[:4]:
            if isinstance(cd, dict) and cd.get("name"):
                cands.append({"name": str(cd["name"])[:30],
                              "reason": str(cd.get("reason") or "")[:80]})
        cache[hs] = {"desc": str(a.get("desc") or "")[:300], "candidates": cands}
        added += 1
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    disc = _load(DISC_PATH, None)
    if disc:
        _apply_cache(disc, cache)
        DISC_PATH.write_text(json.dumps(disc, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("주석 %d개 병합 (캐시 총 %d)", added, len(cache))
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare":
        sys.exit(prepare())
    if cmd == "publish":
        sys.exit(publish())
    print("usage: trade_discover_annotate.py prepare|publish")
    sys.exit(2)


if __name__ == "__main__":
    main()
