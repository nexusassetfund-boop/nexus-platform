"""
퀄리티 성장주 자동 발굴 — 코스피200+코스닥150에서 우량 재무 + 모멘텀 상위 후보를 발굴.

전략 (5개년 백테스트 검증, reports/backtest_quality.md — mode=qm, CAGR 13.84% Sharpe 0.58, Deploy 75/100):
  퀄리티 게이트 통과 종목을 '퀄리티 Z · 모멘텀 Z 50:50 합성' 순으로 정렬.
  ※ 트레일링 성장률은 노이즈로 확인됨 → '성장' 신호는 주가 모멘텀(시장의 미래 성장 기대)이 대변.

파이프라인:
  1) pykrx 시장 펀더멘털 1회 일괄(실패 시 KIS 폴백) → 관문: 시총 3,000억↑·EPS/BPS 양수·PER≤40
  2) 관문 통과 후보 DART 연결재무 → 퀄리티 원시지표(ROE·GPA·영업이익률·부채비율·accruals)
  3) 퀄리티 Z 상위 TOP_TECH만 캔들 조회 → 12-1 모멘텀 (API 보호)
  4) 그 집합에서 퀄리티 Z·모멘텀 Z 재계산 → composite 상위 TOP_OUT 선정
  5) first_seen 추적 → 신규/장기잔류 배지
출력: docs/data/quality_growth.json (프론트 '퀄리티 성장' 탭이 읽음)

실행: 주 1회 GitHub Actions(quality-growth.yml) 또는 수동. DART_API_KEY 필수(없으면 중단).
실패 정책: 펀더멘털/DART 실패 시 기존 출력 파일 보존(빈 파일로 덮어쓰지 않음).
테스트: QUALITY_SCREEN_LIMIT=30 으로 유니버스를 앞 30종목으로 제한 가능.
"""
from __future__ import annotations
import asyncio
import sys
import datetime as dt
import json
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from data_provider import fetch_index_constituents, fetch_ohlcv
import fetch_value
import value_screen as vs   # 시장 펀더멘털·KIS 폴백·기술적 지표 재사용

logger = logging.getLogger("quality_growth")
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "quality_growth.json"
STATE_PATH = ROOT / "docs" / "data" / "quality_growth_state.json"

# ── 스크리닝 기준 (백테스트 검증 파라미터) ──
MIN_CAP = 300_000_000_000   # 시총 3,000억↑ (마이크로캡 배제)
PER_MAX = 40.0              # 극단 고평가만 컷 (정렬엔 미사용)
TOP_OUT = 20                # 최종 출력
TOP_TECH = 80               # 캔들(모멘텀) 조회 대상 = 퀄리티 Z 상위 N (API 보호)
STALE_WEEKS = 39            # 장기잔류 기준 (약 9개월)
# 퀄리티 Z 구성요소 (키, 부호). +높을수록 좋음 / −낮을수록 좋음.
Z_COMPONENTS = [("roe", +1), ("gpa", +1), ("opm", +1), ("debt", -1), ("accruals", -1)]
W_QUALITY, W_MOMENTUM = 0.5, 0.5


def _winsor_z(values, sign):
    """winsorize 5/95 후 z-score(부호 적용). 결측(None)은 None 유지(평균에서 제외)."""
    xs = [v for v in values if v is not None]
    if len(xs) < 5:
        return [None] * len(values)
    lo, hi = np.percentile(xs, 5), np.percentile(xs, 95)
    clipped = [min(max(v, lo), hi) if v is not None else None for v in values]
    present = [v for v in clipped if v is not None]
    mu, sd = float(np.mean(present)), float(np.std(present))
    if sd == 0:
        return [0.0 if v is not None else None for v in clipped]
    return [sign * (v - mu) / sd if v is not None else None for v in clipped]


def _quality_z(recs):
    """recs 각 원소에 quality_z 부여 (가용 구성요소 z 평균)."""
    zmat = {k: _winsor_z([r.get(k) for r in recs], s) for k, s in Z_COMPONENTS}
    for i, r in enumerate(recs):
        zs = [zmat[k][i] for k, _ in Z_COMPONENTS if zmat[k][i] is not None]
        r["quality_z"] = round(float(np.mean(zs)), 3) if zs else None


def _composite(recs):
    """quality_z·mom_z → composite(가중, 각 블록 표준화됨). mom_z는 recs 내 크로스섹션 표준화."""
    mz = _winsor_z([r.get("mom_12_1") for r in recs], +1)
    for i, r in enumerate(recs):
        r["mom_z"] = round(mz[i], 3) if mz[i] is not None else None
        num = den = 0.0
        for w, z in ((W_QUALITY, r["quality_z"]), (W_MOMENTUM, r["mom_z"])):
            if z is not None:
                num += w * z
                den += w
        r["composite"] = round(num / den, 3) if den > 0 else None


async def build() -> dict | None:
    if not fetch_value.DART_KEY:
        logger.error("DART_API_KEY 없음 — 퀄리티 성장 발굴 불가(재무 필수)")
        return None

    constituents = await fetch_index_constituents()
    limit = int(os.environ.get("QUALITY_SCREEN_LIMIT", "0") or 0)
    if limit:
        constituents = constituents[:limit]

    fund, cap, base_date = vs._market_fundamentals()
    if not fund:
        logger.warning("pykrx 실패 — KIS 폴백(%d종목 개별)", len(constituents))
        fund, cap = await vs._kis_fundamentals(constituents)
        base_date = dt.datetime.now(tz=KST).date().strftime("%Y%m%d")
    if not fund or len(fund) < len(constituents) // 2:
        logger.error("펀더멘털 커버리지 미달 — 기존 출력 보존, 중단")
        return None
    exclude = vs._existing_codes()

    # 1차: 관문 — 시총·흑자·PER
    prelim = []
    for code, name in constituents:
        if code in exclude:
            continue
        f = fund.get(code)
        c = cap.get(code, {})
        if not f or not c.get("close"):
            continue
        if c.get("cap") is None or c["cap"] < MIN_CAP:
            continue
        per, pbr, eps, bps, div = f["per"], f["pbr"], f["eps"], f["bps"], f["div"]
        if not (eps and bps) or eps <= 0 or bps <= 0:
            continue
        price = c["close"]
        per = per or round(price / eps, 1)
        if per > PER_MAX:
            continue
        prelim.append({
            "code": code, "name": name, "price": round(price),
            "mktcap": f"{round(c['cap'] / 1e8):,}억",
            "per": round(per, 1), "pbr": round(pbr, 2) if pbr else None, "div": div,
            "eps": eps, "bps": bps, "roe": round(eps / bps * 100, 2),
        })
    logger.info("관문 통과 %d종목 (스캔 %d)", len(prelim), len(constituents))
    if not prelim:
        logger.error("관문 통과 0 — 기존 출력 보존, 중단")
        return None

    # 2차: DART 퀄리티 재무 (관문 통과 전 종목) — 병렬 조회 (직렬이면 200종목×수초 = 타임아웃)
    corp_map = fetch_value._corp_map()
    dart_sem = asyncio.Semaphore(6)

    async def _fin(rec):
        corp = corp_map.get(rec["code"])
        if not corp:
            return None
        async with dart_sem:
            q = await asyncio.to_thread(fetch_value._quality_metrics, corp)
        if not q:
            return None
        rec.update({k: q.get(k) for k in ("gpa", "opm", "debt", "accruals", "rev_g", "op_g")})
        rec["fy"] = q.get("year")
        return rec

    fetched = [r for r in await asyncio.gather(*(_fin(rec) for rec in prelim)) if r]
    logger.info("DART 재무 확보 %d종목", len(fetched))
    if len(fetched) < 5:
        logger.error("재무 확보 %d(<5) — Z 산출 불가, 기존 출력 보존, 중단", len(fetched))
        return None

    # 3차: 퀄리티 Z 상위 TOP_TECH만 캔들 → 12-1 모멘텀
    _quality_z(fetched)
    pool = sorted((r for r in fetched if r["quality_z"] is not None),
                  key=lambda r: r["quality_z"], reverse=True)[:TOP_TECH]
    sem = asyncio.Semaphore(5)

    async def enrich(rec):
        async with sem:
            rec.update(await vs._technicals(rec["code"]) or {})
        return rec

    pool = await asyncio.gather(*(enrich(r) for r in pool))

    # 4차: pool 내에서 퀄리티 Z·모멘텀 Z 재계산 → composite 상위 TOP_OUT
    pool = list(pool)
    _quality_z(pool)          # 동일 집합 기준 재표준화 (모멘텀 Z와 정합)
    _composite(pool)
    survivors = sorted((r for r in pool if r["composite"] is not None),
                       key=lambda r: r["composite"], reverse=True)[:TOP_OUT]
    # 표시용 비율 → % 반올림
    for r in survivors:
        for k in ("gpa", "opm", "accruals"):
            r[k] = round(r[k] * 100, 2) if r.get(k) is not None else None
        r["debt"] = round(r["debt"] * 100, 1) if r.get("debt") is not None else None
        for k in ("rev_g", "op_g"):
            r[k] = round(r[k] * 100, 1) if r.get(k) is not None else None

    # 5차: 신규/장기잔류 배지 (first_seen 추적)
    today = dt.datetime.now(tz=KST).date().isoformat()
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    for rec in survivors:
        first = state.get(rec["code"], today)
        weeks = round((dt.date.fromisoformat(today) - dt.date.fromisoformat(first)).days / 7)
        rec["first_seen"] = first
        rec["is_new"] = first == today
        rec["weeks_listed"] = weeks
        rec["stale"] = weeks >= STALE_WEEKS
    new_state = {rec["code"]: rec["first_seen"] for rec in survivors}

    return {
        "updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
        "base_date": base_date,
        "dart": True,
        "criteria": {
            "min_cap": "시총 3,000억↑", "per_max": PER_MAX,
            "quality_z": "ROE·GPA(매출총이익/자산)·영업이익률·−부채비율·−accruals (winsorize 5/95 표준화 평균)",
            "sort": "composite = 0.5·퀄리티Z + 0.5·모멘텀Z(12-1)",
            "backtest": "5개년 CAGR 13.84%·Sharpe 0.58·Deploy 75/100 (reports/backtest_quality.md)",
            "note": "트레일링 성장률은 노이즈로 확인 → 성장 신호는 주가 모멘텀이 대변",
        },
        "scanned": len(constituents),
        "passed_gate": len(prelim),
        "fin_ok": len(fetched),
        "candidates": survivors,
        "_state": new_state,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = asyncio.run(build())
    if result is None:
        logger.error("스크리닝 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    new_state = result.pop("_state", {})
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE_PATH.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s (후보 %d종목)", OUT_PATH, len(result["candidates"]))


if __name__ == "__main__":
    main()
