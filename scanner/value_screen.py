"""
가치투자 자동 스크리너 — 코스피200+코스닥150 유니버스에서 저평가 우량주 후보를 자동 발굴.

파이프라인:
  1) pykrx 시장 펀더멘털(PER/PBR/EPS/BPS/DIV) 일괄 1회 호출 → 밸류 1차 필터
  2) 자체 적정가(RIM·Graham 보수값, run_scan._calc_fair_value 재사용) → 안전마진
  3) 안전마진 상위 후보만 DART Piotroski F-Score (fetch_value._fscore 재사용) → 퀄리티 필터
  4) 후보 종목 캔들로 기술적 위치(52주 위치·추세·RSI) 계산
출력: docs/data/value_screen.json (프론트 '가치투자 > 자동 발굴' 섹션이 읽음)

실행: 주 1회 GitHub Actions(value-screen.yml) 또는 수동. DART_API_KEY 없으면 F-Score 생략.
실패 정책: 펀더멘털을 아예 못 얻으면 기존 출력 파일을 보존하고 종료(빈 파일로 덮어쓰지 않음).
테스트: 환경변수 VALUE_SCREEN_LIMIT=20 으로 유니버스를 앞 20종목으로 제한 가능.
"""
from __future__ import annotations
import asyncio
import datetime as dt
import json
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from data_provider import fetch_index_constituents, fetch_ohlcv
from run_scan import _calc_fair_value, _fnum
import fetch_value

logger = logging.getLogger("value_screen")
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "value_screen.json"
UNIVERSE_PATH = ROOT / "value_universe.json"

# ── 스크리닝 기준 (보수적 딥밸류 — 완화하려면 여기만 수정) ──
MIN_CAP = 300_000_000_000   # 시총 3,000억 이상 (마이크로캡 밸류트랩 배제)
PER_MAX = 15.0
PBR_MAX = 1.5
MARGIN_MIN = 15.0           # 자체 적정가 대비 안전마진 15% 이상
FSCORE_MIN = 5              # Piotroski 5점 이상 (재무 훼손 배제)
TOP_FSCORE = 40             # DART F-Score 계산 대상 (밸류 상위 N — API 호출 절약)
TOP_OUT = 20                # 최종 출력 종목 수


def _market_fundamentals():
    """전 종목 펀더멘털·시총 (pykrx, 최근 영업일). 반환: (fund_map, cap_map, 기준일) 실패 시 ({}, {}, None)."""
    try:
        from pykrx import stock as _pykrx
    except ImportError:
        logger.error("pykrx 미설치")
        return {}, {}, None
    def _markets(fn, d):
        """market='ALL' 우선, 미지원/실패 시 KOSPI+KOSDAQ 병합. 유효 컬럼 없으면 예외."""
        try:
            df = fn(d, market="ALL")
            if df is not None and len(df) and ({"PER", "시가총액"} & set(getattr(df, "columns", []))):
                return df
        except Exception:
            pass
        import pandas as pd
        parts = []
        for mk in ("KOSPI", "KOSDAQ"):
            p = fn(d, market=mk)
            if p is not None and len(p):
                parts.append(p)
        if not parts:
            raise RuntimeError("전 시장 조회 실패")
        return pd.concat(parts)

    for back in range(0, 7):
        d = (dt.datetime.now(tz=KST).date() - dt.timedelta(days=back)).strftime("%Y%m%d")
        try:
            fdf = _markets(_pykrx.get_market_fundamental_by_ticker, d)
            if fdf is None or not len(fdf) or "PER" not in fdf.columns:
                continue
            fund = {}
            for t in fdf.index:
                fund[str(t).zfill(6)] = {
                    "per": _fnum(fdf.loc[t, "PER"]),
                    "pbr": _fnum(fdf.loc[t, "PBR"]),
                    "eps": _fnum(fdf.loc[t, "EPS"]),
                    "bps": _fnum(fdf.loc[t, "BPS"]),
                    "div": _fnum(fdf.loc[t, "DIV"], allow_zero=True),
                }
            cap = {}
            try:
                cdf = _markets(_pykrx.get_market_cap_by_ticker, d)
                for t in cdf.index:
                    cap[str(t).zfill(6)] = {
                        "cap": _fnum(cdf.loc[t, "시가총액"]),
                        "close": _fnum(cdf.loc[t, "종가"]),
                    }
            except Exception as e:
                logger.warning("시총 조회 실패(무시): %s", e)
            logger.info("펀더멘털 %d종목 (기준일 %s)", len(fund), d)
            return fund, cap, d
        except Exception as e:
            logger.warning("pykrx 펀더멘털 실패 %s: %s", d, e)
    return {}, {}, None


def _existing_codes() -> set:
    """이미 유니버스/포트폴리오에 있는 종목 — 후보에서 제외."""
    try:
        v = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        return {r.get("code") for r in v.get("universe", []) + v.get("portfolio", []) if r.get("code")}
    except Exception:
        return set()


async def _technicals(code: str) -> dict:
    """52주 위치·추세·RSI — 후보 종목만 개별 캔들 조회."""
    try:
        df = await fetch_ohlcv(code, days=280)
    except Exception:
        return {}
    if df is None or df.empty or len(df) < 30:
        return {}
    closes = df["close"].astype(float)
    price = float(closes.iloc[-1])
    tail = df.tail(250)
    high52, low52 = float(tail["high"].max()), float(tail["low"].min())
    pos52 = round((price - low52) / ((high52 - low52) or 1) * 100) if high52 > low52 else None
    ma60 = float(closes.tail(60).mean()) if len(closes) >= 60 else None
    ma60_prev = float(closes.iloc[-70:-10].mean()) if len(closes) >= 70 else None
    trend = "flat"
    if ma60 and ma60_prev:
        if price > ma60 and ma60 > ma60_prev:
            trend = "up"
        elif price < ma60 and ma60 < ma60_prev:
            trend = "down"
    rsi14 = None
    if len(closes) >= 15:
        delta = closes.diff().tail(14)
        up_avg = float(delta.clip(lower=0).mean())
        dn_avg = float((-delta.clip(upper=0)).mean())
        rsi14 = round(100 - 100 / (1 + up_avg / dn_avg), 1) if dn_avg else 100.0
    return {
        "price": round(price), "high52": round(high52), "low52": round(low52),
        "pos52": pos52, "trend": trend, "rsi14": rsi14,
        "off_high": round((price / high52 - 1) * 100, 1) if high52 else None,
    }


async def build() -> dict | None:
    constituents = await fetch_index_constituents()
    limit = int(os.environ.get("VALUE_SCREEN_LIMIT", "0") or 0)
    if limit:
        constituents = constituents[:limit]
    fund, cap, base_date = _market_fundamentals()
    if not fund:
        logger.error("펀더멘털 조회 전체 실패 — 기존 출력 보존, 스크리닝 중단")
        return None
    exclude = _existing_codes()

    # 1차: 밸류 필터 + 자체 적정가/안전마진
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
        if not (eps and bps):          # 적자·자본잠식·데이터 결측 배제
            continue
        price = c["close"]
        per = per or round(price / eps, 1)
        pbr = pbr or round(price / bps, 2)
        if per > PER_MAX or pbr > PBR_MAX:
            continue
        roe = round(eps / bps * 100, 1)
        fair, fair_rim, fair_graham = _calc_fair_value(eps, bps, roe)
        if not fair:
            continue
        margin = round((fair - price) / fair * 100, 1)
        if margin < MARGIN_MIN:
            continue
        prelim.append({
            "code": code, "name": name, "price": round(price),
            "mktcap": f"{round(c['cap'] / 1e8):,}억",
            "per": round(per, 1), "pbr": round(pbr, 2), "div": div, "eps": eps, "bps": bps, "roe": roe,
            "fair": fair, "fair_rim": fair_rim, "fair_graham": fair_graham, "margin": margin,
        })
    prelim.sort(key=lambda x: x["margin"], reverse=True)
    logger.info("밸류 필터 통과 %d종목 (스캔 %d)", len(prelim), len(constituents))

    # 2차: DART F-Score (상위 후보만 — API 호출 절약)
    dart_ok = bool(fetch_value.DART_KEY)
    corp_map = fetch_value._corp_map() if dart_ok else {}
    survivors = []
    for rec in prelim[:TOP_FSCORE]:
        if dart_ok:
            corp = corp_map.get(rec["code"])
            fs = fetch_value._fscore(corp) if corp else None
            rec["f_score"] = fs
            if fs and fs["score"] < FSCORE_MIN:
                continue        # 명백한 재무 훼손만 탈락 (F-Score 미산출은 통과)
        else:
            rec["f_score"] = None
        survivors.append(rec)

    # 3차: 기술적 위치 (동시 5개 제한)
    sem = asyncio.Semaphore(5)

    async def enrich(rec):
        async with sem:
            rec.update(await _technicals(rec["code"]) or {})
        return rec

    survivors = await asyncio.gather(*(enrich(r) for r in survivors))
    survivors = sorted(survivors, key=lambda r: ((r.get("f_score") or {}).get("score", -1), r["margin"]), reverse=True)[:TOP_OUT]

    return {
        "updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
        "base_date": base_date,
        "dart": dart_ok,
        "criteria": {
            "min_cap": "시총 3,000억↑", "per_max": PER_MAX, "pbr_max": PBR_MAX,
            "margin_min": MARGIN_MIN, "fscore_min": FSCORE_MIN,
            "fair_note": "적정가 = RIM(Ke 9%·ROE 상한 25%)·Graham 중 보수값",
        },
        "scanned": len(constituents),
        "passed_value": len(prelim),
        "candidates": survivors,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = asyncio.run(build())
    if result is None:
        return  # 기존 파일 보존
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s (후보 %d종목)", OUT_PATH, len(result["candidates"]))


if __name__ == "__main__":
    main()
