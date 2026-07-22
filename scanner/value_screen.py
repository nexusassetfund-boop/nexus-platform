"""
가치투자 자동 스크리너 — 코스피200+코스닥150 유니버스에서 저평가 우량주 후보를 자동 발굴.

파이프라인:
  1) pykrx 시장 펀더멘털(PER/PBR/EPS/BPS/DIV) 일괄 1회 호출 → 밸류 1차 필터
  2) 자체 적정가(RIM·Graham 보수값, run_scan._calc_fair_value 재사용) → 안전마진
  3) 마진 상위 후보 캔들로 기술적 위치(52주 위치·추세·RSI) + 12-1 모멘텀 계산
  4) 12-1 모멘텀 내림차순 정렬 → 상위 20 (모멘텀 미산출은 마진 순으로 뒤에)
  5) 최종 후보만 DART Piotroski F-Score — **표시 전용** (컷·정렬 미사용)
  6) first_seen 추적 → 신규/장기잔류 배지 필드
출력: docs/data/value_screen.json (프론트 '가치투자 > 자동 발굴' 섹션이 읽음)

정렬·F-Score 근거 (2026-07 5년 백테스트, reports/backtest_value.md):
  (F-Score, 마진) 정렬 CAGR 4.5% < 마진 정렬 7.3% < F-Score 제거 9.5% < 12-1 모멘텀 정렬 11.2%.
  F-Score는 정렬·컷 모두 성과를 깎았고(7점 종목 평균 -5.4%), 깊은 마진 최상위도 밸류트랩 성향.
  → 밸류는 관문으로만, 순서는 모멘텀으로, F-Score는 참고 표시로.

실행: 주 1회 GitHub Actions(value-screen.yml) 또는 수동. DART_API_KEY 없으면 F-Score 생략.
실패 정책: 펀더멘털을 아예 못 얻으면 기존 출력 파일을 보존하고 종료(빈 파일로 덮어쓰지 않음).
테스트: 환경변수 VALUE_SCREEN_LIMIT=20 으로 유니버스를 앞 20종목으로 제한 가능.
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

from data_provider import fetch_index_constituents, fetch_ohlcv, fetch_fundamental_kis, load_config
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
FSCORE_MIN = 5              # 표시 기준선 (백테스트상 컷·정렬 사용 시 성과 악화 → 참고용)
TOP_FSCORE = 40             # (구) F-Score 컷 대상 — 현재는 미사용, 백테스트 재현용 보존
TOP_OUT = 20                # 최종 출력 종목 수
TOP_TECH = 60               # 캔들 조회 대상 (마진 상위 N — API 호출 보호)
MOM_WIN = 231               # 12-1 모멘텀 룩백 (거래일 ≈ 11개월)
MOM_SKIP = 21               # 최근 1개월 제외 (단기 반전 회피) — 백테스트 검증 조합
STALE_WEEKS = 39            # 장기잔류 기준 (약 9개월 — 백테스트: 12개월+ 보유 평균 -8.4%)
STATE_PATH = ROOT / "docs" / "data" / "value_screen_state.json"



_FIN_KEYWORDS = ("금융", "지주", "홀딩스", "은행", "증권", "보험", "생명", "화재", "캐피탈", "카드")


def _fair_conf(rec: dict, roe_norm) -> str:
    """적정가 신뢰등급 A/B/C — 모델 가정 위반 신호마다 강등.
    금융·지주(회계 BPS 특성상 RIM 과대)는 즉시 C."""
    grade = 0  # 0=A, 1=B, 2+=C
    name = rec.get("name") or ""
    if any(k in name for k in _FIN_KEYWORDS):
        grade = 2
    if rec.get("roe") is not None and rec["roe"] > 25:
        grade += 1                      # ROE 캡 발동 — 피크 이익 의심
    if roe_norm is not None and rec.get("roe") is not None and abs(rec["roe"] - roe_norm) > 10:
        grade += 1                      # 단년 ROE가 정상화와 크게 괴리 — 일회성 의심
    if (rec.get("margin") or 0) > 60:
        grade += 1                      # 극단 괴리 — 모델 오류 가능성이 저평가보다 높음
    fs = (rec.get("f_score") or {}).get("score")
    if fs is not None and fs <= 4:
        grade += 1
    return "ABC"[min(grade, 2)]


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
            # 장중·개장 전이면 당일 종가가 미정산(0/None)이라 시총·관문이 전부 깨진다.
            # 유효 종가가 바닥이면 이 날짜를 버리고 직전 거래일로 백오프(look-ahead 아님).
            valid_close = sum(1 for c in cap.values() if c.get("close"))
            if valid_close < 100:
                logger.warning("기준일 %s 유효 종가 %d — 미정산(장중·개장 전) 추정, 직전일로 백오프",
                               d, valid_close)
                continue
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
    """52주 위치·추세·RSI + 12-1 모멘텀 — 후보 종목만 개별 캔들 조회."""
    try:
        df = await fetch_ohlcv(code, days=400)  # 12-1 모멘텀에 거래일 253개 필요
    except Exception:
        return {}
    if df is None or df.empty or len(df) < 30:
        return {}
    closes = df["close"].astype(float)
    mom = None
    if len(closes) >= MOM_WIN + MOM_SKIP + 1:
        a, b = float(closes.iloc[-1 - MOM_SKIP]), float(closes.iloc[-1 - MOM_SKIP - MOM_WIN])
        if b > 0:
            mom = round((a / b - 1) * 100, 1)
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
        "mom_12_1": mom,
    }


async def _kis_fundamentals(constituents) -> tuple[dict, dict]:
    """KIS 폴백 — 유니버스 종목만 개별 조회 (KRX가 클라우드 IP를 차단할 때).
    배당수익률은 KIS 응답에 없어 None (필터가 아닌 표시용이라 무해)."""
    cfg = load_config()
    sem = asyncio.Semaphore(3)  # KIS 초당 호출 제한 보호
    fund, cap = {}, {}

    async def one(code):
        async with sem:
            fb = await fetch_fundamental_kis(cfg, code)
        if fb and fb.get("price"):
            fund[code] = {k: fb.get(k) for k in ("per", "pbr", "eps", "bps", "div")}
            cap[code] = {"cap": fb.get("cap"), "close": fb.get("price")}

    await asyncio.gather(*(one(code) for code, _ in constituents))
    return fund, cap


async def build() -> dict | None:
    constituents = await fetch_index_constituents()
    limit = int(os.environ.get("VALUE_SCREEN_LIMIT", "0") or 0)
    if limit:
        constituents = constituents[:limit]
    fund, cap, base_date = _market_fundamentals()
    if not fund:
        logger.warning("pykrx 펀더멘털 실패 — KIS 폴백으로 전환 (%d종목 개별 조회)", len(constituents))
        fund, cap = await _kis_fundamentals(constituents)
        base_date = dt.datetime.now(tz=KST).date().strftime("%Y%m%d")
    if not fund:
        logger.error("펀더멘털 조회 전체 실패(pykrx·KIS) — 기존 출력 보존, 스크리닝 중단")
        return None
    # 커버리지 바닥: KIS 폴백이 일부만 반환(토큰 쿨다운·레이트리밋)하면 near-empty 후보가
    # 기존 좋은 파일을 덮어쓸 수 있으므로, 유니버스 절반 미만이면 보존하고 중단.
    # (pykrx 성공 시 fund는 전 시장 ~2700개라 이 가드에 안 걸림 — KIS 부분실패 경로만 차단)
    if len(fund) < len(constituents) // 2:
        logger.error("펀더멘털 커버리지 %d/%d 미달(부분 실패) — 기존 출력 보존, 중단",
                     len(fund), len(constituents))
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
        fair, fair_rim, fair_rim_cons = _calc_fair_value(eps, bps, roe)
        if not fair:
            continue
        margin = round((fair - price) / fair * 100, 1)
        if margin < MARGIN_MIN:
            continue
        prelim.append({
            "code": code, "name": name, "price": round(price),
            "mktcap": f"{round(c['cap'] / 1e8):,}억",
            "per": round(per, 1), "pbr": round(pbr, 2), "div": div, "eps": eps, "bps": bps, "roe": roe,
            "fair": fair, "fair_rim": fair_rim, "fair_rim_cons": fair_rim_cons, "margin": margin,
        })
    prelim.sort(key=lambda x: x["margin"], reverse=True)
    logger.info("밸류 필터 통과 %d종목 (스캔 %d)", len(prelim), len(constituents))

    # 2차: 기술적 위치 + 12-1 모멘텀 (마진 상위 TOP_TECH만 — API 보호, 동시 5개 제한)
    sem = asyncio.Semaphore(5)

    async def enrich(rec):
        async with sem:
            rec.update(await _technicals(rec["code"]) or {})
        return rec

    pool = await asyncio.gather(*(enrich(r) for r in prelim[:TOP_TECH]))

    # 3차: 12-1 모멘텀 내림차순 정렬 → 상위 20 (미산출은 마진 순으로 뒤에)
    #      백테스트 근거: (F-Score,마진) 정렬 4.5% < 모멘텀 정렬+F-Score 미사용 11.2% CAGR
    with_mom = sorted((r for r in pool if r.get("mom_12_1") is not None),
                      key=lambda r: r["mom_12_1"], reverse=True)
    without_mom = sorted((r for r in pool if r.get("mom_12_1") is None),
                         key=lambda r: r["margin"], reverse=True)
    survivors = (with_mom + without_mom)[:TOP_OUT]

    # 4차: DART F-Score — 최종 후보만, 표시 전용 (컷·정렬 미사용)
    dart_ok = bool(fetch_value.DART_KEY)
    corp_map = fetch_value._corp_map() if dart_ok else {}
    for rec in survivors:
        corp = corp_map.get(rec["code"]) if dart_ok else None
        rec["f_score"] = fetch_value._fscore(corp) if corp else None
        # 정상화 ROE(최근 3개년 평균)로 적정가·안전마진 재계산 — 단년 일회성 이익 왜곡 보정
        roe_norm = None
        if corp:
            series = fetch_value._roe_series(corp)
            if len(series) >= 2:
                roe_norm = round(sum(series[-3:]) / len(series[-3:]), 1)
        if roe_norm is not None and roe_norm > 0 and rec.get("bps"):
            fair2, rim2, cons2 = _calc_fair_value(rec.get("eps"), rec["bps"], roe_norm)
            if fair2 and rec.get("price"):
                rec["roe_norm"] = roe_norm
                rec["fair"], rec["fair_rim"], rec["fair_rim_cons"] = fair2, rim2, cons2
                rec["margin"] = round((fair2 - rec["price"]) / fair2 * 100, 1)
        elif roe_norm is not None:
            rec["roe_norm"] = roe_norm
        rec["conf"] = _fair_conf(rec, roe_norm)

    # 5차: 신규/장기잔류 배지 (first_seen 추적 — 이탈 후 재진입은 신규 취급)
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
        rec["stale"] = weeks >= STALE_WEEKS  # 장기잔류 — 밸류트랩 경고 (백테스트: 12M+ 평균 -8.4%)
    new_state = {rec["code"]: rec["first_seen"] for rec in survivors}

    return {
        "updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
        "base_date": base_date,
        "dart": dart_ok,
        "criteria": {
            "min_cap": "시총 3,000억↑", "per_max": PER_MAX, "pbr_max": PBR_MAX,
            "margin_min": MARGIN_MIN,
            "sort": "12-1 모멘텀 내림차순 (밸류는 관문, 순서는 추세)",
            "fscore_note": f"F-Score는 참고 표시 전용 ({FSCORE_MIN}점 미만 주의) — 컷·정렬 미사용",
            "fair_note": "적정가 = fade RIM(초과ROE 10년/보수 5년 소멸·Ke 9%·ROE 상한 25%) — 정상화 ROE(3개년 평균) 기준·Graham 폐지",
            "backtest": "5.0y 포인트인타임 재검증(2026-07): CAGR 6.6%·Sharpe 0.41·MDD -27.5% vs KS200 20.4% — 안전마진 단독은 선정 알파 아님(발굴 관문·가격 참고 전용). fade RIM은 구 산식 대비 +2.1%p 개선",
        },
        "scanned": len(constituents),
        "passed_value": len(prelim),
        "candidates": survivors,
        "_state": new_state,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = asyncio.run(build())
    if result is None:
        # 기존 파일 보존 + 워크플로 red 노출(조용한 stale 방지 — 주 1회 캐이던스라 실패가 묻히면 위험)
        logger.error("스크리닝 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    new_state = result.pop("_state", {})
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE_PATH.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s (후보 %d종목)", OUT_PATH, len(result["candidates"]))


if __name__ == "__main__":
    main()
