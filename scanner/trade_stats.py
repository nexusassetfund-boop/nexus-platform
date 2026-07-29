"""
수출입 데이터 스캐너 — 품목별 수출입 동향과 그 품목을 다루는 종목을 함께 낸다.

방향: 키움증권 빅데이터 메뉴와 BeOn 채널이 하는 방식이다. 매출 상관이나 실적 추정을
주장하지 않는다. "인공호흡기 수출이 늘었고, 이걸 수출하는 종목은 한컴라이프케어·
씨유메디칼이다" 까지만 말한다. 좁은 품목일수록 수출사가 적어 귀속이 깨끗하다.

수입도 함께 본다. 원재료·소재는 수입 쪽이 선행 신호다 — MR-MUF(언더필) 수입이 늘면
HBM 생산이 앞서 늘고, 펄프 수입은 제지주 가동률을 앞선다. 특수 소재는 수입사가 적어
귀속이 수출보다 오히려 깨끗한 경우가 많다.

파이프라인:
  1) trade_map.json(품목 ↔ 종목 인덱스)에서 대상 HS 수집
  2) 품목별 API로 월별 수출입 금액·중량 수집 (디스크 캐시, 1년 단위 분할)
  3) 지표: 전월비 / 전년동월비 / 분기평균 / 누적 / 연속개월 / 수출단가 / 물량 / 수입
  4) 관세환율로 원화 기준 증감률 병기 — USD 기준과의 차이가 환율이 만든 착시분
출력: docs/data/trade.json (프론트 '전략실 > 수출입' 탭이 읽음)

실행: 매일 07:00 KST trade-stats.yml (월 단위 데이터라 대부분 캐시 히트로 끝난다).
실패 정책: 인덱스 부재 또는 수집 성공 품목이 절반 미만이면 기존 출력 보존 후 exit 1.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import customs_api as capi

logger = logging.getLogger("trade")

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "trade.json"
HISTORY_PATH = ROOT / "docs" / "data" / "trade_history.json"
CACHE_PATH = ROOT / "docs" / "data" / "trade_raw_cache.json"
MAP_PATH = Path(__file__).parent / "data" / "trade_map.json"
SCAN_PATH = ROOT / "docs" / "data" / "scan.json"

# ── 기준 (완화하려면 여기만 수정) ──
MOM_MIN = 10.0            # '주목 품목' 전월비 하한 (%) — 키움도 전월비를 전면에 쓴다
AMOUNT_MIN = 100_000.0    # 월 수출입액 하한. 품목별 API의 금액 단위는 **USD**다.
STREAK_YOY = 10.0         # '연속 성장' 판정 기준 YoY (%)
BASE_EFFECT_Q = 0.25      # 전년동월이 자기 시계열 하위 25% 미만이면 기저효과 플래그
MONTHS = 25               # 수집 개월 수 (YoY·분기평균에 최소 24 필요)
MAX_ITEMS = 400           # 처리 품목 상한 (호출 한도 보호)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("%s 로드 실패: %s", path.name, e)
        return default


def _pct(cur, prev):
    """증감률(%). 기준값이 없거나 0이면 None — 0으로 나눠 무한대를 만들지 않는다."""
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _series_to_map(rows: list[dict], sgg: str | None = None) -> dict[str, dict]:
    """API 응답 → {YYYYMM: {amt, qty, imp}}. period가 '2026.06'으로 와도 숫자만 남긴다.

    sgg가 주어지면 해당 시군구 행만 남긴다(시군구별 API를 시도 단위로 부를 때).
    qty는 중량이 있으면 중량, 없으면 건수 — 시군구별 응답에는 중량이 없다.
    """
    out: dict[str, dict] = {}
    for r in rows:
        if sgg and str(r.get("sgg", "")).strip() != sgg:
            continue
        period = "".join(ch for ch in str(r.get("period", "")) if ch.isdigit())[:6]
        if len(period) != 6:
            continue
        amt, imp = r.get("exp_amt"), r.get("imp_amt")
        if amt is None and imp is None:
            continue
        qty = r.get("exp_wgt")
        if qty is None:
            qty = r.get("exp_cnt")
        cur = out.setdefault(period, {"amt": 0.0, "qty": 0.0, "imp": 0.0})
        cur["amt"] += amt or 0.0
        cur["qty"] += qty or 0.0
        cur["imp"] += imp or 0.0
    return out


def _prev_yymm(yymm: str, back: int) -> str:
    y, m = int(yymm[:4]), int(yymm[4:])
    total = y * 12 + (m - 1) - back
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def _quarter_months(yymm: str) -> list[str]:
    y, m = int(yymm[:4]), int(yymm[4:])
    q_start = ((m - 1) // 3) * 3 + 1
    return [f"{y:04d}{q_start + i:02d}" for i in range(3)]


def compute_metrics(series: dict[str, dict], month: str) -> dict | None:
    """월별 시계열에서 지표 일괄 계산. 해당 월 데이터가 없으면 None."""
    cur = series.get(month)
    if not cur:
        return None
    amt = cur.get("amt") or 0.0
    imp_cur = cur.get("imp") or None
    if not amt and not imp_cur:
        return None

    prev_y = (series.get(_prev_yymm(month, 12)) or {}).get("amt")
    prev_m = (series.get(_prev_yymm(month, 1)) or {}).get("amt")

    # 분기평균 YoY — 분기 내 각 월 YoY의 평균 (3개월 평활 역할)
    q_yoys = []
    for qm in _quarter_months(month):
        if qm > month:
            continue
        v = _pct((series.get(qm) or {}).get("amt"),
                 (series.get(_prev_yymm(qm, 12)) or {}).get("amt"))
        if v is not None:
            q_yoys.append(v)
    q_avg = round(statistics.fmean(q_yoys), 1) if q_yoys else None

    # 누적 YoY — 연초~당월 누계 대비
    year = month[:4]
    ytd = sum(v.get("amt", 0.0) for k, v in series.items() if k[:4] == year and k <= month)
    ytd_prev = sum(v.get("amt", 0.0) for k, v in series.items()
                   if k[:4] == str(int(year) - 1) and k[4:] <= month[4:])
    cum_yoy = _pct(ytd, ytd_prev)

    # 연속 성장 개월 수 — 현재 월부터 과거로 YoY > STREAK_YOY 가 이어지는 길이
    streak = 0
    for back in range(0, 24):
        mm = _prev_yymm(month, back)
        v = _pct((series.get(mm) or {}).get("amt"),
                 (series.get(_prev_yymm(mm, 12)) or {}).get("amt"))
        if v is None or v <= STREAK_YOY:
            break
        streak += 1

    # 단가 P = 금액 ÷ 물량. 품목별 API는 중량이 있어 실제 수출단가가 된다.
    def _unit(rec):
        if not rec or not rec.get("qty"):
            return None
        return (rec.get("amt") or 0.0) / rec["qty"]

    p_cur, p_prev = _unit(cur), _unit(series.get(_prev_yymm(month, 12)))
    q_cur = cur.get("qty") or None
    q_prev = (series.get(_prev_yymm(month, 12)) or {}).get("qty") or None

    # P/Q 기여도 분해 — 금액 증가분 중 가격 기여 vs 물량 기여
    contrib_p = contrib_q = None
    if None not in (p_cur, p_prev, q_cur, q_prev):
        contrib_p = round((p_cur - p_prev) * q_prev, 1)
        contrib_q = round((q_cur - q_prev) * p_prev, 1)

    # 기저효과 — 전년동월이 자기 시계열 하위 분위면 증감률이 과장된다
    flags = []
    amts = sorted(v.get("amt", 0.0) for v in series.values() if v.get("amt"))
    if prev_y is not None and amts:
        idx = int(len(amts) * BASE_EFFECT_Q)
        if prev_y <= amts[max(0, idx)]:
            flags.append("base_effect")

    imp_py = (series.get(_prev_yymm(month, 12)) or {}).get("imp") or None
    imp_pm = (series.get(_prev_yymm(month, 1)) or {}).get("imp") or None

    return {
        "amount": round(amt, 1),
        "import_amount": round(imp_cur, 1) if imp_cur else None,
        "import_yoy": _pct(imp_cur, imp_py), "import_mom": _pct(imp_cur, imp_pm),
        "yoy": _pct(amt, prev_y), "mom": _pct(amt, prev_m),
        "q_avg_yoy": q_avg, "cum_yoy": cum_yoy, "streak": streak,
        "price": round(p_cur, 4) if p_cur else None,
        "price_yoy": _pct(p_cur, p_prev), "qty_yoy": _pct(q_cur, q_prev),
        "contrib_p": contrib_p, "contrib_q": contrib_q,
        "flags": flags,
    }


def latest_confirmed_month(today: dt.date) -> str:
    """확정치 기준월. 매월 15일경 전월분이 현행화되므로 15일 전에는 2개월 전을 본다."""
    back = 1 if today.day >= 16 else 2
    return _prev_yymm(f"{today.year:04d}{today.month:02d}", back)


def build() -> dict | None:
    tmap = _load_json(MAP_PATH, None)
    if not tmap or not tmap.get("items"):
        logger.error("품목 인덱스 없음 (%s) — build_trade_map.py를 먼저 실행하세요.", MAP_PATH)
        return None

    api_key = capi.require_key()
    today = dt.datetime.now(tz=KST).date()
    month = latest_confirmed_month(today)
    chunks = capi.month_range(month, MONTHS)

    # 환율 — 수출액은 USD라 원화 약세면 원화 기준 증가율이 더 커진다.
    fx = {}
    for m in {month, _prev_yymm(month, 12), _prev_yymm(month, 1)}:
        try:
            r = capi.fetch_fx_month(m, api_key, CACHE_PATH)
        except capi.CustomsError as e:
            logger.warning("관세환율 사용 불가(%s) — 원화 기준은 생략합니다", str(e)[:80])
            fx = {}
            break
        if r:
            fx[m] = r

    scan = _load_json(SCAN_PATH, {})
    prices = {str(r.get("ticker")).zfill(6): r for r in (scan.get("results") or [])}

    src_items = tmap["items"][:MAX_ITEMS]
    out_items, failed = [], 0
    for it in src_items:
        hs = it.get("hs")
        if not hs:
            failed += 1
            continue
        rows: list[dict] = []
        try:
            for s, e in chunks:
                rows.extend(capi.fetch_item(hs, s, e, api_key, CACHE_PATH))
        except capi.CustomsError as ex:
            logger.warning("수집 실패 hs=%s: %s", hs, ex)
            failed += 1
            continue
        series = _series_to_map(rows)
        m = compute_metrics(series, month)
        if not m:
            failed += 1
            continue

        # 원화 기준 YoY — 같은 두 달을 각자의 환율로 환산해 비교
        prev_m = _prev_yymm(month, 12)
        yoy_krw = None
        if fx.get(month) and fx.get(prev_m):
            cur_krw = (series.get(month) or {}).get("amt", 0.0) * fx[month]
            prv_krw = (series.get(prev_m) or {}).get("amt", 0.0) * fx[prev_m]
            yoy_krw = _pct(cur_krw, prv_krw)

        stocks = []
        for s in it.get("stocks") or []:
            sc = prices.get(str(s.get("ticker")).zfill(6), {})
            stocks.append({**s,
                           "price": sc.get("current_price"),
                           "change_pct": sc.get("change_pct"),
                           "pct_12m": sc.get("pct_12m")})

        out_items.append({
            "hs": hs, "name": it.get("name"), "industry": it.get("industry"),
            "stocks": stocks, "yoy_krw": yoy_krw,
            "series": [{"m": k, "amt": round(v.get("amt", 0.0), 1),
                        "imp": round(v.get("imp", 0.0), 1)}
                       for k, v in sorted(series.items())][-24:],
            **m,
        })

    if src_items and len(out_items) < len(src_items) / 2:
        logger.error("수집 성공 품목이 절반 미만 (%d/%d) — 기존 파일 보존",
                     len(out_items), len(src_items))
        return None

    # ── 주목 품목: 전월비가 크게 뛴 것 (키움도 전월비를 전면에 쓴다)
    def notable(x):
        return (x.get("mom") is not None and x["mom"] >= MOM_MIN
                and (x.get("amount") or 0) >= AMOUNT_MIN)

    risers = sorted([x for x in out_items if notable(x)],
                    key=lambda x: -(x.get("mom") or 0))

    # ── 종목 역인덱스 (내 종목 탭)
    by_ticker: dict[str, list[dict]] = {}
    for it in out_items:
        for s in it.get("stocks") or []:
            by_ticker.setdefault(s["ticker"], []).append(
                {"hs": it["hs"], "name": it["name"], "industry": it["industry"],
                 "mom": it.get("mom"), "yoy": it.get("yoy"),
                 "import_mom": it.get("import_mom")})

    history = _load_json(HISTORY_PATH, [])
    if not history or history[-1].get("month") != month:
        history.append({"month": month, "date": today.isoformat(),
                        "items": [x["hs"] for x in risers[:30]]})
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(history[-24:], ensure_ascii=False, indent=1),
                                encoding="utf-8")

    industries = sorted({x["industry"] for x in out_items if x.get("industry")})
    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "data_month": month,
        "industries": industries,
        "items": sorted(out_items, key=lambda x: -(x.get("amount") or 0)),
        "risers": [x["hs"] for x in risers[:50]],
        "by_ticker": by_ticker,
        "history": history[-12:],
        "fx": {"usd_krw": fx.get(month), "months": len(fx)},
        "thresholds": {
            "mom_min": MOM_MIN, "amount_min": AMOUNT_MIN, "streak_yoy": STREAK_YOY,
            "note": "해당 품목을 수출·수입하는 것으로 보이는 종목을 제시할 뿐, 수출입액이 그 종목의 "
                    "실적이라는 뜻은 아닙니다. 해외 생산분은 한국 통계에 잡히지 않고, 비상장사 물량도 "
                    "같은 품목에 섞여 있습니다.",
        },
        "coverage": {"items": len(out_items), "requested": len(src_items),
                     "failed": failed, "tickers": len(by_ticker)},
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        data = build()
    except capi.CustomsError as e:
        logger.error("수출입 스캔 실패: %s", e)
        sys.exit(1)
    if data is None:
        logger.error("수출입 스캔 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    cov = data["coverage"]
    logger.info("저장: %s (기준월 %s · 품목 %d/%d · 종목 %d · 주목 %d)",
                OUT_PATH, data["data_month"], cov["items"], cov["requested"],
                cov["tickers"], len(data["risers"]))


if __name__ == "__main__":
    main()
