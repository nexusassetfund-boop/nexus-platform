"""
낙폭·밸류밴드 스크리너 — "빠졌는데 싸진 종목"과 "이익도 같이 망가진 종목"을 기계적으로 구분.

파이프라인:
  1) docs/data/scan.json 결과에서 52주 고점 대비 낙폭 큰 종목 추출 (off_high ≤ -25%)
  2) pykrx 시총 필터 (3,000억+) → 낙폭 깊은 순 최대 100종목 (API 보호)
  3) 종목별 최근 1년 일별 PER/PBR/EPS (pykrx) → 현재 멀티플의 1년 밴드 백분위 계산
  4) 판정:
     - derating       : PBR이 1년 밴드 하위 20% + EPS 유지 확인(적자 제외) → 가격만 빠짐(싸짐) — 매수 후보
     - earnings_driven: EPS 1년 -15% 이하 훼손 → 이익 동반 하락(안 싸짐) — 함정 주의
     - neutral        : 그 외 (밴드 중상단 등)
출력: docs/data/drawdown_bands.json (프론트 '이벤트드리븐 > 낙폭·밸류밴드'가 읽음)

주의: 판정 기준은 백테스트 미검증 — 발굴 보조용. 실행: 주 1회 value-screen.yml 또는 수동.
실패 정책: 밴드 확보가 대상의 절반 미만이면 기존 출력 파일 보존 후 exit 1 (value_screen 패턴).
테스트: 환경변수 DRAWDOWN_LIMIT=10 으로 대상 종목 수 제한 가능.
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from run_scan import _fnum
from value_screen import _market_fundamentals

logger = logging.getLogger("drawdown_bands")
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
SCAN_PATH = ROOT / "docs" / "data" / "scan.json"
OUT_PATH = ROOT / "docs" / "data" / "drawdown_bands.json"

# ── 기준 (완화하려면 여기만 수정) ──
OFF_HIGH_MAX = -25.0        # 52주 고점 대비 -25% 이하만
MIN_CAP = 300_000_000_000   # 시총 3,000억 이상 (마이크로캡 배제 — value_screen과 동일)
MAX_TICKERS = 500           # 일별 멀티플 조회 상한 (안전장치 — 유니버스 전체가 걸려도 여유)
BAND_DAYS = 372             # 밴드 룩백 (달력일 ≈ 1년)
DERATE_PBR_PCT = 20.0       # 디레이팅 게이트: PBR 1년 밴드 하위 20% (자산가치 하한)
DERATE_PER_PCT = 30.0       # 디레이팅 확인: PER 1년 밴드 하위 30% (이익 기준도 싸야 함)
TRAP_PER_PCT = 50.0         # 함정: PBR 낮은데 PER 밴드 상위 → 이익이 가격보다 더 빠짐
EPS_DROP_MIN = -15.0        # 이익 훼손 판정: EPS 1년 변화 -15% 이하
PYKRX_SLEEP = 0.25          # KRX 조회 간격


def _pct_rank(vals: list[float], cur: float) -> float | None:
    """cur가 vals 분포에서 차지하는 백분위 (0=최저, 100=최고)."""
    if cur is None or len(vals) < 20:  # 표본 부족 시 밴드 신뢰 불가
        return None
    below = sum(1 for v in vals if v < cur)
    return round(below / len(vals) * 100, 1)


def _band(code: str, start: str, end: str):
    """1년 일별 PER/PBR/EPS → (pbr, pbr_pct, pbr_lo, pbr_hi, per, per_pct, eps_chg_1y)."""
    from pykrx import stock as _pykrx
    df = _pykrx.get_market_fundamental(start, end, code)
    if df is None or df.empty:
        return None
    pbrs = [float(v) for v in df.get("PBR", []) if _fnum(v)]
    pers = [float(v) for v in df.get("PER", []) if _fnum(v)]  # 적자(0/NaN) 제외
    # EPS는 0(적자)도 유효값 — 흑자전환/적자전환을 구분해야 하므로 걸러내지 않는다
    epss = [float(v) for v in df.get("EPS", []) if _fnum(v, allow_zero=True) is not None]
    if not pbrs:
        return None
    pbr = pbrs[-1]
    per = pers[-1] if pers and _fnum(df["PER"].iloc[-1]) else None  # 현재 적자면 PER 없음
    eps_chg, eps_note = None, None
    if len(epss) >= 2:
        if epss[0] > 0 and epss[-1] > 0:
            eps_chg = round((epss[-1] - epss[0]) / epss[0] * 100, 1)
        elif epss[0] <= 0 < epss[-1]:
            eps_note = "흑자전환"
        elif epss[-1] <= 0 < epss[0]:
            eps_note = "적자전환"
        # 둘 다 0 이하 → 적자 지속(eps_chg/note 없음)
    return {
        "pbr": round(pbr, 2), "pbr_pct": _pct_rank(pbrs, pbr),
        "pbr_lo": round(min(pbrs), 2), "pbr_hi": round(max(pbrs), 2),
        "per": round(per, 1) if per else None,
        "per_pct": _pct_rank(pers, per) if per else None,
        "eps_chg_1y": eps_chg, "eps_note": eps_note,
    }


def _verdict(rec: dict) -> str:
    eps, note = rec.get("eps_chg_1y"), rec.get("eps_note")
    if note == "적자전환" or (eps is not None and eps <= EPS_DROP_MIN):
        return "earnings_driven"
    # EPS 판별 불가(적자 지속)면 "이익 유지" 증거가 없음 — 디레이팅 자격 없음.
    # (적자 고PBR 테마주가 밴드 하위라는 이유만으로 디레이팅 상위에 오르는 것 방지)
    if eps is None and note != "흑자전환":
        return "neutral"
    # PBR 게이트: 자산가치 대비 밴드 하위여야 디레이팅 후보
    if rec.get("pbr_pct") is None or rec["pbr_pct"] > DERATE_PBR_PCT:
        return "neutral"
    # PER로 확인/반증: 이익 기준으로도 싸야 진짜 디레이팅, PER 밴드 높으면 함정
    per_pct = rec.get("per_pct")
    if per_pct is None:
        return "derating"          # PER 데이터 없음(표본 부족) → PBR 게이트만으로 판정
    if per_pct > TRAP_PER_PCT:
        return "earnings_driven"   # 자산은 싸 보여도 이익 기준 안 쌈 = 함정 (예: 삼성생명)
    if per_pct <= DERATE_PER_PCT:
        return "derating"          # PBR·PER 둘 다 하위 → 확인된 디레이팅
    return "neutral"               # 중간대(30~50%) → 애매, 중립


def build() -> dict | None:
    scan = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    results = scan.get("results", [])
    if not results:
        logger.error("scan.json 결과 없음")
        return None

    fund, cap, base_d = _market_fundamentals()
    if not cap:
        logger.error("pykrx 시총 확보 실패")
        return None

    pool = []
    for r in results:
        code = str(r.get("ticker", "")).zfill(6)
        oh = r.get("off_high")
        if oh is None or oh > OFF_HIGH_MAX:
            continue
        c = (cap.get(code) or {}).get("cap")
        if c is None or c < MIN_CAP:
            continue
        pool.append((code, r))
    # 신형코드(0009K0 등) 신규상장 종목은 스캐너가 이름을 못 받아옴 — pykrx로 보완
    for code, r in pool:
        if not r.get("name") or r.get("name") == r.get("ticker"):
            try:
                from pykrx import stock as _pykrx
                nm = _pykrx.get_market_ticker_name(code)
                if isinstance(nm, str) and nm and nm != code:
                    r["name"] = nm
            except Exception:
                pass

    pool.sort(key=lambda x: x[1].get("off_high") or 0)  # 낙폭 깊은 순
    dropped = max(0, len(pool) - MAX_TICKERS)
    if dropped:
        logger.info("대상 %d종목 중 낙폭 상위 %d만 조회 (%d 생략)", len(pool), MAX_TICKERS, dropped)
    pool = pool[:MAX_TICKERS]
    limit = int(os.environ.get("DRAWDOWN_LIMIT", "0"))
    if limit:
        pool = pool[:limit]

    end = dt.datetime.now(tz=KST).date()
    start_s = (end - dt.timedelta(days=BAND_DAYS)).strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    out, fails = [], 0
    for code, r in pool:
        try:
            band = _band(code, start_s, end_s)
        except Exception as e:
            logger.warning("%s 밴드 실패: %s", code, e)
            band = None
        time.sleep(PYKRX_SLEEP)
        f = fund.get(code) or {}
        # 시계열 무결성 검증 — KRX가 (특히 해외 IP에) 낡은 시계열을 주는 사례 발견:
        # 시계열 마지막 PBR이 전종목 스냅샷(신뢰 가능)과 15% 이상 어긋나면 불량으로 탈락.
        snap_pbr = f.get("pbr")
        if band and snap_pbr and band.get("pbr"):
            if abs(band["pbr"] - snap_pbr) / snap_pbr > 0.15:
                logger.warning("%s 시계열 불일치: 밴드 PBR %.2f vs 스냅샷 %.2f — 탈락",
                               code, band["pbr"], snap_pbr)
                band = None
        if not band:
            fails += 1
            continue
        rec = {
            "code": code, "name": r.get("name"), "sector": r.get("sector"),
            "price": r.get("current_price"), "off_high": r.get("off_high"),
            "high52": r.get("high52"), "low52": r.get("low52"),
            "ret_1m": r.get("ret_1m"), "rs_rank": r.get("rs_rank"),
            "div": f.get("div"), "cap_100m": round((cap[code]["cap"]) / 1e8),
            **band,
        }
        rec["verdict"] = _verdict(rec)
        out.append(rec)

    if pool and len(out) < len(pool) / 2:
        logger.error("밴드 확보 %d/%d — 일시 장애 의심, 기존 파일 보존", len(out), len(pool))
        return None

    # 디레이팅 먼저(PBR 밴드 낮은 순), 그 뒤 중립·이익동반
    order = {"derating": 0, "neutral": 1, "earnings_driven": 2}
    out.sort(key=lambda x: (order[x["verdict"]], x["pbr_pct"] if x["pbr_pct"] is not None else 100))

    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "base_date": base_d,
        "criteria": {
            "off_high_max": OFF_HIGH_MAX, "min_cap_100m": MIN_CAP // 100_000_000,
            "derate_pbr_pct": DERATE_PBR_PCT, "derate_per_pct": DERATE_PER_PCT,
            "trap_per_pct": TRAP_PER_PCT, "eps_drop_min": EPS_DROP_MIN,
            "band_days": BAND_DAYS,
            "note": "판정은 백테스트 미검증 — 발굴 보조용. PBR 밴드=최근 1년 일별 분포 백분위.",
        },
        "scanned": len(pool), "candidates": out,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = build()
    if data is None:
        logger.error("낙폭·밸류밴드 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(1 for c in data["candidates"] if c["verdict"] == "derating")
    logger.info("저장: %s (%d종목, 디레이팅 %d)", OUT_PATH, len(data["candidates"]), n)


if __name__ == "__main__":
    main()
