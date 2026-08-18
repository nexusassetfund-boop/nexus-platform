"""시군구별 API 진단 — 읽기 전용. 파일을 쓰지도 커밋하지도 않는다.

실행: CUSTOMS_API_KEY=... python -X utf8 scanner/trade_diagnose.py
CI:   trade-map-rebuild 워크플로를 tickers=diagnose 로 수동 실행

왜: 2026-07-01 행정구역 개편(인천·전남광주·안양)으로 기준월 202607부터
인천(sido 28) 4종목 + 광주(sido 29) 1종목이 통째로 빠졌다. 원인이
  (가) 시도코드 자체가 바뀌어 옛 코드가 0행을 돌려주는 것인지
  (나) 코드는 살아 있는데 sggNm 지명만 바뀌어 매칭이 어긋난 것인지
에 따라 고칠 곳이 다르다(전자는 sido_codes, 후자는 sgg_hint).

두 가지를 찍는다:
  A. 시도코드 스윕 — 한 HS를 개편 전월/후월로 전 코드에 물어 살아있는 코드표를 복원
  B. 실패 종목 추적 — trade_map 의 (sido, hs)로 월별 행수·지명 변화를 나열
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import customs_api as capi

logger = logging.getLogger("diagnose")

MAP_PATH = Path(__file__).resolve().parent / "data" / "trade_map.json"
OUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "data" / "trade.json"

# 개편 전(202606) / 후(202607) 비교. 개편은 2026-07-01자다.
BEFORE, AFTER = "202606", "202607"

# 스윕용 HS — 화장품 기초(330499). 전국 여러 시도에 생산 거점이 흩어져 있어
# "이 코드가 살아 있는가"를 넓게 보기에 적당하다. 코드 판별이 목적이지 금액이 아니다.
SWEEP_HS = "330499"

# 구 체계 17개 + 개편으로 생겼을 법한 빈 번호대. 코드 판별이 목적이므로 넓게 훑는다.
# 40여 콜이면 개발계정 한도(10,000/월) 대비 무시할 수준이다.
SWEEP_CODES = [f"{n:02d}" for n in list(range(11, 54))]


def _rows(hs: str, sido: str, yymm: str, key: str) -> list[dict] | str:
    """한 (시도, HS, 월) 조회. 캐시를 쓰지 않는다 — 진단은 항상 실데이터를 봐야 한다."""
    try:
        return capi.fetch_district(hs, sido, yymm, yymm, key, None)
    except capi.CustomsError as e:
        return f"ERROR {str(e)[:80]}"


def _summary(rows: list[dict] | str) -> str:
    if isinstance(rows, str):
        return rows
    if not rows:
        return "0행"
    sgg = sorted({str(r.get("sgg") or "?") for r in rows})
    # 지명은 "인천광역시 중구" 형태다. 시도명(앞 토큰)이 바뀌었는지가 핵심 단서다.
    sido_names = sorted({s.split(" ")[0] for s in sgg})
    head = ", ".join(sgg[:6]) + (f" 외 {len(sgg) - 6}" if len(sgg) > 6 else "")
    return f"{len(rows)}행 · 시도명 {'/'.join(sido_names)} · {head}"


def sweep(key: str) -> None:
    print("\n" + "=" * 78)
    print(f"A. 시도코드 스윕 — HS {SWEEP_HS}, {BEFORE}(개편 전) vs {AFTER}(개편 후)")
    print("=" * 78)
    for code in SWEEP_CODES:
        before = _rows(SWEEP_HS, code, BEFORE, key)
        after = _rows(SWEEP_HS, code, AFTER, key)
        # 둘 다 빈 응답이면 애초에 없는 코드다 — 줄만 늘리므로 접는다(오류는 남긴다).
        if isinstance(before, list) and isinstance(after, list) and not before and not after:
            continue
        mark = ""
        if before and not after:
            mark = "  ← 개편 후 사라짐"
        elif after and not before:
            mark = "  ← 개편 후 새로 생김"
        print(f"\nsidoCd {code}{mark}")
        print(f"  {BEFORE}: {_summary(before)}")
        print(f"  {AFTER}: {_summary(after)}")


def track_failures(key: str) -> None:
    print("\n" + "=" * 78)
    print("B. 실패 종목 추적 — trade_map 의 (sido, hs)로 월별 확인")
    print("=" * 78)
    tmap = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    entries = tmap.get("entries") or []

    got: set[str] = set()
    if OUT_PATH.exists():
        data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        got = {str(s.get("ticker")) for s in (data.get("stocks") or [])}

    missing = [e for e in entries if str(e.get("ticker")) not in got] if got else entries
    if not missing:
        print("현재 trade.json 에 빠진 종목이 없습니다.")
        return

    for e in missing:
        sido, hs = e.get("sido"), e.get("hs_used")
        print(f"\n{e.get('ticker')} {e.get('name')} — sido {sido} · HS {hs} · 등록지명 {e.get('sgg')}")
        for yymm in ("202604", "202605", BEFORE, AFTER):
            print(f"  {yymm}: {_summary(_rows(hs, sido, yymm, key))}")


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    key = os.environ.get("CUSTOMS_API_KEY", "").strip()
    if not key:
        print("CUSTOMS_API_KEY 미설정 — 진단할 수 없습니다.")
        sys.exit(1)
    sweep(key)
    track_failures(key)
    print(f"\n실제 네트워크 호출 {capi.call_count()}회 (한도 10,000/월)")
    print("이 스크립트는 읽기 전용입니다 — 파일을 쓰지 않았습니다.")


if __name__ == "__main__":
    main()
