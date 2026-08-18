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
BANDS_PATH = ROOT / "docs" / "data" / "drawdown_bands.json"
HISTORY_PATH = ROOT / "docs" / "data" / "quality_growth_history.json"

# ── 스크리닝 기준 (백테스트 검증 파라미터) ──
MIN_CAP = 300_000_000_000   # 시총 3,000억↑ (마이크로캡 배제)
PER_MAX = 40.0              # 극단 고평가만 컷 (정렬엔 미사용)
TOP_OUT = 20                # 최종 출력
# 관문·재무를 통과한 전 종목에 모멘텀을 붙인다. 80으로 자르면 composite이 매겨지는 모집단이
# 좁아져 backtest_quality.md(관문 통과 전체로 검증)와 어긋나고, 조합 전략(combo_strategy.py)이
# 쓰는 퀄리티 풀도 같이 좁아진다. 현재 fin_ok가 ~166이라 250이면 사실상 전량.
TOP_TECH = 250              # 캔들(모멘텀) 조회 상한 (API 보호용 안전장치)
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


# drawdown_bands.json에서 이관하는 가격대(밴드) 지표 — 타이밍 렌즈, 정렬·선정엔 미사용
BAND_FIELDS = ("off_high", "per_pct", "pbr_pct", "pbr_lo", "pbr_hi",
               "rise_from_low", "above_ma20", "stabilized", "verdict",
               "low52", "high52", "sector")


def _attach_bands(survivors):
    """drawdown_bands.json 조인 — 밴드 풀 밖 종목은 필드 없음(프론트 graceful 처리)."""
    try:
        bands = json.loads(BANDS_PATH.read_text(encoding="utf-8"))
        by_code = {c["code"]: c for c in bands.get("candidates", [])}
    except Exception:
        logger.warning("drawdown_bands.json 없음/파싱 실패 — 밴드 조인 생략")
        return
    joined = 0
    for rec in survivors:
        b = by_code.get(rec["code"])
        if b:
            rec["band"] = {k: b.get(k) for k in BAND_FIELDS}
            joined += 1
    logger.info("밴드 조인 %d/%d종목", joined, len(survivors))


def _stability(corp):
    """5개년 안정성 지표 (MSCI 이익변동성·GMO 마진안정성·QMJ payout 근거, 표시·필터용 — 선정 미사용).
    fnlttSinglAcnt 2회 호출(각 3개 연도 반환)로 최대 5개년 시계열 구성.
      roe_sigma: 연도별 ROE(순이익/자본총계) 표준편차 (pp) — 이익 변동성
      opm_sigma: 연도별 영업이익률 표준편차 (pp) — 마진 안정성(해자 지속성 프록시)
      dilution : 자본금 기간 변화율 (%) — 희석 프록시 (F-Score noNewShares와 동일 계열)
      stab_years: 시계열 연도 수 (3 미만이면 σ 미산출)
    """
    import urllib.request
    yr = dt.datetime.now(tz=KST).year - 1
    by_year = {}   # year -> {rev, op, ni, eq, cap}
    ACCTS = {"매출액": "rev", "영업이익": "op", "당기순이익": "ni", "자본총계": "eq", "자본금": "cap"}
    for y in (yr, yr - 2):
        url = (f"{fetch_value._DART}/fnlttSinglAcnt.json?crtfc_key={fetch_value.DART_KEY}"
               f"&corp_code={corp}&bsns_year={y}&reprt_code=11011")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode())
        except Exception:
            continue
        rows = d.get("list") or []
        fs = "CFS" if any(x.get("fs_div") == "CFS" for x in rows) else "OFS"
        for x in rows:
            if x.get("fs_div") != fs:
                continue
            nm = (x.get("account_nm") or "").replace(" ", "").removesuffix("(손실)")
            key = ACCTS.get(nm)
            if not key:
                continue
            for per, off in (("thstrm", 0), ("frmtrm", 1), ("bfefrmtrm", 2)):
                v = fetch_value._num(x.get(f"{per}_amount"))
                if v is not None:
                    by_year.setdefault(y - off, {}).setdefault(key, v)
    def series(num, den):
        out = []
        for y in sorted(by_year):
            d_ = by_year[y]
            n, dv = d_.get(num), d_.get(den)
            if n is not None and dv not in (None, 0):
                out.append(n / dv * 100)
        return out
    roes, opms = series("ni", "eq"), series("op", "rev")
    caps = [by_year[y]["cap"] for y in sorted(by_year) if by_year[y].get("cap")]
    return {
        "roe_sigma": round(float(np.std(roes)), 2) if len(roes) >= 3 else None,
        "opm_sigma": round(float(np.std(opms)), 2) if len(opms) >= 3 else None,
        "dilution": round((caps[-1] / caps[0] - 1) * 100, 1) if len(caps) >= 2 and caps[0] > 0 else None,
        "stab_years": len(by_year),
    }


def _update_history(survivors, prev_state, today):
    """편입/제외 이력 누적 (append-only). 반환: 최근 26회 이력."""
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        history = []
    prev_codes = set(prev_state)
    if not prev_codes:                      # 최초 실행 — diff 없음
        return history[-26:]
    try:
        prev_names = {c["code"]: c["name"]
                      for c in json.loads(OUT_PATH.read_text(encoding="utf-8"))["candidates"]}
    except Exception:
        prev_names = {}
    cur = {r["code"]: r["name"] for r in survivors}
    added = [{"code": c, "name": n} for c, n in cur.items() if c not in prev_codes]
    removed = [{"code": c, "name": prev_names.get(c, c)}
               for c in sorted(prev_codes - set(cur))]
    if added or removed:
        history.append({"date": today, "added": added, "removed": removed})
        HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return history[-26:]


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

    # 6차: 안정성 지표 (최종 편입 종목만 — 종목당 DART 2콜, 표시·필터용)
    stab_sem = asyncio.Semaphore(6)

    async def _stab(rec):
        corp = corp_map.get(rec["code"])
        if corp:
            async with stab_sem:
                rec.update(await asyncio.to_thread(_stability, corp))
        return rec

    await asyncio.gather(*(_stab(r) for r in survivors))
    logger.info("안정성 지표 산출 %d/%d종목",
                sum(1 for r in survivors if r.get("roe_sigma") is not None), len(survivors))

    # 7차: 밴드 지표 조인(타이밍 렌즈) + 편입/제외 이력 누적
    _attach_bands(survivors)
    history = _update_history(survivors, state, today)

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
            "stability": "안정성(ROE σ·마진 σ·희석)은 표시·필터용 — 선정(composite)에는 미사용 (백테스트 재검증 전)",
        },
        "scanned": len(constituents),
        "passed_gate": len(prelim),
        "fin_ok": len(fetched),
        "candidates": survivors,
        # composite이 매겨진 전 종목 — 조합 전략(신고가 게이트 × 퀄리티 랭크)이 랭커로 쓴다.
        # candidates(상위 20)만으로는 교집합이 말라 검증본과 다른 물건이 된다.
        "pool": [{"code": r["code"], "name": r["name"], "composite": r["composite"],
                  "quality_z": r["quality_z"], "mom_z": r.get("mom_z")}
                 for r in sorted((x for x in pool if x.get("composite") is not None),
                                 key=lambda x: x["composite"], reverse=True)],
        "history": history,
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
