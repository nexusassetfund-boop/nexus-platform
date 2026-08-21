"""
역방향 발굴 — 전 품목(HS10)을 매달 스캔해 급가속 품목을 먼저 찾는다.

트래커는 아는 종목만 본다. 이 스캐너는 반대로 **품목에서 출발**한다: 관세청 품목별
전수(HS10 약 9,600건)에서 최근 급가속한 품목을 랭킹해, 몰랐던 기회를 수면 위로
올린다. 키움 "오늘의 발견"과 같은 발상.

품목→종목 연결은 자동화하지 않는다 — 문자열 매칭은 이미 실패했고(여행사가 화장품에
붙었다), 이 목록은 발굴 재료다. 종목 후보는 리포트가 '후보' 수준으로만 언급하고,
확정 매핑은 기존 검증 파이프라인(probe → stock_trade_map → verify)을 거친다.

호출량: fetch_all_items는 월당 1콜. 25개월 백필 = 25콜, 이후 회차당 최근 7개월
재수집(최근 6개월은 캐시 제외 정책) — 한도(10,000/월)에 무해.

선별 기준(백테스트 반영): 레벨이 아니라 **변화**. 월 수출 $2M 이상 중에서
YoY·MoM이 함께 뛰고 12개월 신고인 품목을 앞세운다. 백테스트에서 가속 신호 단독은
무효(6M 중앙 -11%)였으므로 이 목록은 매수 신호가 아니라 **조사 출발점**이다 —
출력 note에 명시한다.

출력: docs/data/trade_discover.json
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import customs_api as capi
from trade_stats import _pct, _prev_yymm, latest_confirmed_month

logger = logging.getLogger("discover")

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "trade_discover.json"
CACHE_PATH = ROOT / "docs" / "data" / "trade_raw_cache.json"
MAP_PATH = Path(__file__).parent / "data" / "trade_map.json"

MONTHS = 25               # YoY 계산에 최소 13, 12개월 신고 판정에 여유
MIN_AMT_USD = 2_000_000   # 월 수출 하한 (품목별 API 단위 = USD)
MIN_YOY = 40.0            # 급가속 후보 하한
MIN_MOM = 15.0
TOP_N = 30
BAD_NAMES = ("기타", "기타의 것", "그 밖의 것", "총계")


def build() -> dict | None:
    api_key = capi.require_key()
    capi.preflight(api_key)          # 경로가 막혔으면 20초 안에 죽는다
    today = dt.datetime.now(tz=KST).date()
    month = latest_confirmed_month(today)

    # 월별 전수 수집 — 월당 1콜
    series: dict[str, dict[str, float]] = {}   # {hs10: {yymm: exp_usd}}
    names: dict[str, str] = {}
    got_months = []
    for back in range(MONTHS):
        m = _prev_yymm(month, back)
        try:
            rows = capi.fetch_all_items(m, api_key, CACHE_PATH)
        except capi.CustomsError as e:
            logger.warning("%s 수집 실패: %s", m, e)
            continue
        if not rows:
            continue
        got_months.append(m)
        for r in rows:
            hs = str(r.get("hs") or "")
            nm = (r.get("name") or "").strip()
            if len(hs) != 10 or not nm or nm in BAD_NAMES:
                continue
            series.setdefault(hs, {})[m] = r.get("exp_amt") or 0.0
            names[hs] = nm

    if not got_months:
        logger.error("수집된 월이 없음 — 기존 파일 보존")
        return None
    newest = max(got_months)
    if newest != month:
        logger.info("기준월 %s → %s (미공개로 물러남)", month, newest)
        month = newest
    if len(got_months) < 14:
        logger.error("확보 개월 %d < 14 — YoY 계산 불가, 기존 파일 보존", len(got_months))
        return None

    # 이미 추적 중인 HS6 (트래커와의 중복 표시용)
    tracked6 = {e.get("hs_used", "")[:6]
                for e in (json.loads(MAP_PATH.read_text(encoding="utf-8")).get("entries") or [])
                if e.get("hs_used")} if MAP_PATH.exists() else set()

    out = []
    for hs, ser in series.items():
        amt = ser.get(month)
        if amt is None or amt < MIN_AMT_USD:
            continue
        yoy = _pct(amt, ser.get(_prev_yymm(month, 12)), MIN_AMT_USD * 0.25)
        mom = _pct(amt, ser.get(_prev_yymm(month, 1)), MIN_AMT_USD * 0.25)
        if yoy is None or mom is None or yoy < MIN_YOY or mom < MIN_MOM:
            continue
        recent12 = [ser.get(_prev_yymm(month, k)) for k in range(12)]
        recent12 = [v for v in recent12 if v is not None]
        high_12m = bool(recent12) and amt >= max(recent12)
        out.append({
            "hs": hs, "name": names.get(hs, hs),
            "amount": round(amt / 1000.0, 1),        # 천USD로 통일(화면 단위)
            "yoy": yoy, "mom": mom, "high_12m": high_12m,
            "tracked": hs[:6] in tracked6,
            "series": [{"m": k, "amt": round(ser[k] / 1000.0, 1)}
                       for k in sorted(ser)][-24:],
        })

    # 정렬: 12개월 신고 우선, 그다음 YoY. 극단 % 는 이미 하한(_pct base)으로 절제됨.
    out.sort(key=lambda x: (not x["high_12m"], -(x["yoy"] or 0)))
    out = out[:TOP_N]

    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "data_month": month,
        "months": len(got_months),
        "scanned": len(series),
        "note": "급가속 품목 목록은 조사 출발점이지 매수 신호가 아니다 — 백테스트에서 "
                "가속 신호 단독 매수는 6개월 중앙 -11%로 무효였다. 품목→종목 연결은 "
                "검증 파이프라인을 거쳐야 한다.",
        "items": out,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        data = build()
    except capi.CustomsError as e:
        logger.error("발굴 스캔 실패: %s", e)
        sys.exit(1)
    if data is None:
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("저장: %s (기준월 %s · 전수 %d품목 → 급가속 %d)",
                OUT_PATH, data["data_month"], data["scanned"], len(data["items"]))


if __name__ == "__main__":
    main()
