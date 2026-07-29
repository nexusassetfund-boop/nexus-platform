"""
수출입 데이터 스크리너 — 관세청 통계로 수출이 먼저 움직인 종목을 발굴.

근거: 기업별 수출실적은 관세법상 비공개다. 따라서 "소재지 시군구 × 품목(HS)" 조합을
      해당 종목의 수출 프록시로 쓰고, 분기 수출액 vs 공시 분기매출 상관계수로 자기검증한다.
      (세종기업데이터가 공개한 매칭 방식과 동일 — "코스맥스-경기 수출_화성시")
      매칭 테이블은 build_trade_map.py가 만들고 여기서는 읽기만 한다.

파이프라인:
  1) trade_map.json 로드 → 등급 A/B 종목의 (시군구, HS) 조합 수집
  2) 관세청 시군구별 품목별 API로 조합별 월별 수출액·중량 수집 (디스크 캐시, 1년 단위 분할)
  3) 지표 계산: YoY / MoM / 분기평균 YoY / 누적 YoY / 연속개월 / 단가 / 물량
     주의: 시군구별 API에는 중량이 없어 물량 = 수출건수, 단가 = 건당 금액이다.
     진짜 수출단가(금액÷중량)는 중량이 있는 품목별 API를 쓰는 품목 레벨에서만 유효하다.
  4) 스크리닝: 월 YoY 하한 AND 분기평균 YoY>0 AND 절대금액 하한 (일회성·소액 노이즈 배제)
  5) 회귀계수(alpha/beta)로 분기 매출 추정 + 주가 미반영 스코어 산출
  6) 관세환율로 원화 기준 YoY 병기 — USD 기준과 벌어진 폭이 환율이 만든 착시분이다
출력: docs/data/trade.json (프론트 '전략실 > 수출입' 탭이 읽음)

주의: 영업이익은 산출하지 않는다 — 매출→이익은 마진 가정이 필요하고 그건 사람 몫이다.
      해외 생산분은 한국 수출통계에 잡히지 않으므로 추정매출은 하한 성격을 갖는다.
실행: 매일 07:00 KST trade-stats.yml (월 단위 데이터라 대부분 캐시 히트로 끝난다).
실패 정책: 매칭 테이블 부재 또는 수집 조합이 절반 미만이면 기존 출력 보존 후 exit 1.
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
HISTORY_PATH = ROOT / "docs" / "data" / "trade_history.json"   # append-only 편입/편출
CACHE_PATH = ROOT / "docs" / "data" / "trade_raw_cache.json"   # 관세청 월별 응답 캐시
MAP_PATH = Path(__file__).parent / "data" / "trade_map.json"
SCAN_PATH = ROOT / "docs" / "data" / "scan.json"               # 종목명·주가 재활용

# ── 스크리닝 기준 (완화하려면 여기만 수정) ──
YOY_MIN = 20.0            # 해당 월 수출 YoY 하한 (%)
Q_AVG_MIN = 0.0           # 분기평균 YoY 하한 — 일회성 급증 배제
AMOUNT_MIN = 1_000.0      # 월 수출액 하한 (천 USD) — 소액 종목 변동성 노이즈 배제
STREAK_YOY = 10.0         # "연속 성장" 판정 기준 YoY (%)
BASE_EFFECT_Q = 0.25      # 전년동월 금액이 자기 시계열 하위 25% 미만이면 기저효과 플래그
GRADES_SHOWN = ("A", "B")  # 본 목록에 노출할 신뢰등급
MONTHS = 36               # 수집 개월 수 (YoY·분기평균 계산에 최소 24 필요)


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
    """API 응답 → {YYYYMM: {amt, qty}}. period가 '2026.06' 등으로 와도 숫자만 남긴다.

    시군구별 API는 시도 단위로 요청하면 그 안의 모든 시군구 행이 함께 오므로,
    sgg가 주어지면 해당 시군구 행만 남긴다(sgg=None이면 시도 전체 합산).

    qty(물량)는 중량이 있으면 중량, 없으면 건수를 쓴다 — 시군구별 응답에는 중량이 없다.
    따라서 종목 레벨의 '단가'는 엄밀히는 건당 금액이며, 화면에서도 그렇게 표기한다.
    """
    out = {}
    for r in rows:
        if sgg and str(r.get("sgg", "")).strip() != sgg:
            continue
        period = "".join(ch for ch in str(r.get("period", "")) if ch.isdigit())[:6]
        if len(period) != 6:
            continue
        amt = r.get("exp_amt")
        if amt is None:
            continue
        qty = r.get("exp_wgt")
        if qty is None:
            qty = r.get("exp_cnt")
        cur = out.setdefault(period, {"amt": 0.0, "qty": 0.0})
        cur["amt"] += amt
        cur["qty"] += qty or 0.0
    return out


def _prev_yymm(yymm: str, back: int) -> str:
    y, m = int(yymm[:4]), int(yymm[4:])
    total = y * 12 + (m - 1) - back
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def _quarter_months(yymm: str) -> list[str]:
    """해당 월이 속한 분기의 3개월."""
    y, m = int(yymm[:4]), int(yymm[4:])
    q_start = ((m - 1) // 3) * 3 + 1
    return [f"{y:04d}{q_start + i:02d}" for i in range(3)]


def compute_metrics(series: dict[str, dict], month: str) -> dict | None:
    """월별 시계열에서 지표 일괄 계산. 해당 월 데이터가 없으면 None."""
    cur = series.get(month)
    if not cur or not cur.get("amt"):
        return None

    amt = cur["amt"]
    prev_y = series.get(_prev_yymm(month, 12), {}).get("amt")
    prev_m = series.get(_prev_yymm(month, 1), {}).get("amt")

    # 분기평균 YoY — 분기 내 각 월 YoY의 평균 (3개월 평활 역할)
    q_yoys = []
    for qm in _quarter_months(month):
        if qm > month:
            continue
        v = _pct(series.get(qm, {}).get("amt"), series.get(_prev_yymm(qm, 12), {}).get("amt"))
        if v is not None:
            q_yoys.append(v)
    q_avg = round(statistics.fmean(q_yoys), 1) if q_yoys else None

    # 누적 YoY — 연초~당월 누계 대비
    year = month[:4]
    ytd = sum(v["amt"] for k, v in series.items() if k[:4] == year and k <= month)
    ytd_prev = sum(v["amt"] for k, v in series.items()
                   if k[:4] == str(int(year) - 1) and k[4:] <= month[4:])
    cum_yoy = _pct(ytd, ytd_prev)

    # 연속 성장 개월 수 — 현재 월부터 과거로 YoY > STREAK_YOY 가 이어지는 길이
    streak = 0
    for back in range(0, 24):
        mm = _prev_yymm(month, back)
        v = _pct(series.get(mm, {}).get("amt"), series.get(_prev_yymm(mm, 12), {}).get("amt"))
        if v is None or v <= STREAK_YOY:
            break
        streak += 1

    # 단가 P = 금액 ÷ 물량, 물량 Q. 물량이 0이면 단가는 무의미.
    # 시군구별 데이터에는 중량이 없어 물량이 '건수'다 → P는 건당 금액 성격임에 주의.
    def _unit(rec):
        if not rec or not rec.get("qty"):
            return None
        return rec["amt"] / rec["qty"]

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
    amts = sorted(v["amt"] for v in series.values() if v.get("amt"))
    if prev_y is not None and amts:
        idx = int(len(amts) * BASE_EFFECT_Q)
        if prev_y <= amts[max(0, idx)]:
            flags.append("base_effect")

    return {
        "amount": round(amt, 1),
        "yoy": _pct(amt, prev_y), "mom": _pct(amt, prev_m),
        "q_avg_yoy": q_avg, "cum_yoy": cum_yoy, "streak": streak,
        "price": round(p_cur, 4) if p_cur else None,
        "price_yoy": _pct(p_cur, p_prev), "qty_yoy": _pct(q_cur, q_prev),
        "contrib_p": contrib_p, "contrib_q": contrib_q,
        "flags": flags,
    }


def _percentile_rank(values: list[float], v: float) -> int:
    """v가 values 안에서 차지하는 백분위(0~100)."""
    if not values:
        return 0
    below = sum(1 for x in values if x < v)
    return round(below / len(values) * 100)


def latest_confirmed_month(today: dt.date) -> str:
    """관세청 확정치 기준월. 매월 15일경 전월분이 현행화되므로 15일 전에는 2개월 전을 본다."""
    back = 1 if today.day >= 16 else 2
    return _prev_yymm(f"{today.year:04d}{today.month:02d}", back)


def build() -> dict | None:
    tmap = _load_json(MAP_PATH, None)
    if not tmap or not tmap.get("entries"):
        logger.error("매칭 테이블 없음 (%s) — build_trade_map.py를 먼저 실행하세요.", MAP_PATH)
        return None

    api_key = capi.require_key()
    today = dt.datetime.now(tz=KST).date()
    month = latest_confirmed_month(today)
    chunks = capi.month_range(month, MONTHS)

    entries = {t: e for t, e in tmap["entries"].items() if e.get("grade") in ("A", "B", "C")}

    # 환율 — 수출액은 USD라 원화가 약해지면 원화 기준 증가율이 더 커진다.
    # USD 기준과 원화 기준을 함께 보여줘야 "성장이 진짜인지 환율 효과인지" 구분된다.
    fx = {}
    for m in {month, _prev_yymm(month, 12), _prev_yymm(month, 1)}:
        try:
            r = capi.fetch_fx_month(m, api_key, CACHE_PATH)
        except capi.CustomsError as e:
            # 환율은 부가 지표다 — 못 받아도 스캔 자체는 계속한다(원화 기준만 생략).
            logger.warning("관세환율 사용 불가(%s) — 원화 기준 증감률은 생략합니다", str(e)[:90])
            fx = {}
            break
        if r:
            fx[m] = r
    if len(fx) < 2:
        logger.warning("환율 확보 부족(%d개월) — 원화 기준 증감률은 생략합니다", len(fx))
    scan = _load_json(SCAN_PATH, {})
    prices = {str(r.get("ticker")).zfill(6): r for r in (scan.get("results") or [])}

    # ── (시군구, HS) 조합별 시계열 수집 — 여러 종목이 같은 조합을 공유하면 1회만 호출
    # scope='전국'인 항목은 지역 없이 품목별 API를 쓴다(매칭 단계에서 전국이 더
    # 잘 맞은 종목 — 본점 주소와 공장 위치가 다른 경우가 많다). region을 None으로 둔다.
    combos: dict[tuple[str | None, str], dict] = {}
    for e in entries.values():
        region = None if e.get("scope") == "전국" else e.get("region")
        for hs in e.get("hs") or []:
            combos.setdefault((region, hs), {})

    # (시도, HS) 1회 호출로 그 도의 모든 시군구가 함께 오므로 원시 응답을 먼저 모은다.
    raw_by_combo: dict[tuple[str, str], list[dict]] = {}
    failed = 0
    for (region, hs) in combos:
        if not hs:
            failed += 1
            continue
        rows: list[dict] = []
        try:
            for s, e in chunks:
                if region is None:
                    # 전국 품목별 — 중량이 있어 실제 수출단가를 낼 수 있다
                    rows.extend(capi.fetch_item(hs, s, e, api_key, CACHE_PATH))
                else:
                    rows.extend(capi.fetch_district(hs, region, s, e, api_key, CACHE_PATH))
        except capi.CustomsError as ex:
            logger.warning("수집 실패 %s hs=%s: %s", region or "전국", hs, ex)
            failed += 1
            continue
        if rows:
            raw_by_combo[(region, hs)] = rows

    series_by_combo = {k: _series_to_map(v) for k, v in raw_by_combo.items()}

    total = len(combos)
    if total and len(series_by_combo) < total / 2:
        logger.error("수집 성공 조합이 절반 미만 (%d/%d) — 기존 파일 보존", len(series_by_combo), total)
        return None

    # ── 종목별 지표 계산
    rows = []
    for ticker, e in entries.items():
        # 지역 스코프면 자기 시군구 행만 봐야 한다 — 같은 도의 타 지역이 섞이면 프록시가 무너진다.
        national = e.get("scope") == "전국"
        sgg = None if national else e.get("sgg")
        region = None if national else e.get("region")
        merged: dict[str, dict] = {}
        for hs in e.get("hs") or []:
            raw = raw_by_combo.get((region, hs))
            if not raw:
                continue
            for k, v in _series_to_map(raw, sgg).items():
                cur = merged.setdefault(k, {"amt": 0.0, "qty": 0.0})
                cur["amt"] += v["amt"]
                cur["qty"] += v["qty"]
        m = compute_metrics(merged, month)
        if not m:
            continue

        # 원화 기준 YoY — 같은 두 달을 각자의 환율로 환산해 비교.
        # USD 기준과 벌어지는 폭이 곧 환율이 만든 착시분이다.
        prev_m = _prev_yymm(month, 12)
        yoy_krw = None
        if fx.get(month) and fx.get(prev_m):
            cur_krw = (merged.get(month) or {}).get("amt", 0.0) * fx[month]
            prv_krw = (merged.get(prev_m) or {}).get("amt", 0.0) * fx[prev_m]
            yoy_krw = _pct(cur_krw, prv_krw)

        # 분기 매출 추정 — alpha + beta × 분기수출액 (build_trade_map이 회귀로 구한 계수)
        est_rev = est_band = None
        q_amt = sum(merged.get(qm, {}).get("amt", 0.0) for qm in _quarter_months(month))
        if e.get("beta") is not None and q_amt:
            est = (e.get("alpha") or 0.0) + e["beta"] * q_amt
            est_rev = round(est, 1)
            band = e.get("err_band")
            if band:
                est_band = [round(est * (1 - band), 1), round(est * (1 + band), 1)]

        sc = prices.get(str(ticker).zfill(6), {})
        rows.append({
            "ticker": str(ticker).zfill(6),
            "name": e.get("name") or sc.get("name") or ticker,
            "grade": e.get("grade"), "corr": e.get("corr"),
            "region_name": e.get("region_name"), "hs": e.get("hs"),
            "scope": e.get("scope"),
            # 시군구별에는 중량이 없어 물량=건수·단가=건당 금액이다.
            # 전국(품목별)은 중량이 있어 실제 수출단가가 나온다.
            "qty_is_count": not national,
            "yoy_krw": yoy_krw,     # 원화 환산 기준 — USD 기준과의 차이가 환율 효과
            "shared": bool(e.get("shared")),
            "est_rev": est_rev, "est_band": est_band,
            "_price_ret": sc.get("pct_12m"),
            **m,
        })

    # ── 주가 미반영 스코어 = 수출 YoY 백분위 − 주가 수익률 백분위
    yoys = [r["yoy"] for r in rows if r.get("yoy") is not None]
    rets = [r["_price_ret"] for r in rows if r.get("_price_ret") is not None]
    for r in rows:
        if r.get("yoy") is None:
            r["unreflected"] = None
        else:
            pr = _percentile_rank(rets, r["_price_ret"]) if r.get("_price_ret") is not None else 50
            r["unreflected"] = max(0, _percentile_rank(yoys, r["yoy"]) - pr)
        r["price_ret_12m"] = r.pop("_price_ret", None)

    # ── 스크리닝
    def passes(r):
        return (r.get("grade") in GRADES_SHOWN
                and r.get("yoy") is not None and r["yoy"] >= YOY_MIN
                and (r.get("q_avg_yoy") is None or r["q_avg_yoy"] > Q_AVG_MIN)
                and (r.get("amount") or 0) >= AMOUNT_MIN)

    cands = sorted([r for r in rows if passes(r)],
                   key=lambda r: (r.get("unreflected") or 0), reverse=True)

    # ── 편입/편출 이력 (전월 언급 종목 팔로업용)
    history = _load_json(HISTORY_PATH, [])
    prev_set = set()
    for h in reversed(history):
        if h.get("month") and h["month"] != month:
            prev_set = set(h.get("tickers") or [])
            break
    cur_set = [r["ticker"] for r in cands]
    if not history or history[-1].get("month") != month:
        history.append({"month": month, "date": today.isoformat(), "tickers": cur_set})
        HISTORY_PATH.write_text(json.dumps(history[-24:], ensure_ascii=False, indent=1), encoding="utf-8")
    for r in cands:
        r["is_new"] = 0 if r["ticker"] in prev_set else 1

    # ── 품목 단위 집계 (품목 탭)
    items = []
    hs_names = {hs: n for e in entries.values() for hs, n in
                zip(e.get("hs") or [], e.get("hs_names") or [])}
    for hs in {hs for e in entries.values() for hs in (e.get("hs") or [])}:
        merged: dict[str, dict] = {}
        for (region, h), s in series_by_combo.items():
            if h != hs:
                continue
            for k, v in s.items():
                cur = merged.setdefault(k, {"amt": 0.0, "qty": 0.0})
                cur["amt"] += v["amt"]
                cur["qty"] += v["qty"]
        m = compute_metrics(merged, month)
        if not m:
            continue
        items.append({
            "hs": hs, "name": hs_names.get(hs, hs),
            "tickers": [t for t, e in entries.items() if hs in (e.get("hs") or [])],
            "series": [{"m": k, "amt": round(v["amt"], 1)} for k, v in sorted(merged.items())][-24:],
            **m,
        })

    graded = [e.get("grade") for e in entries.values()]
    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "data_month": month,
        "candidates": cands,
        "items": sorted(items, key=lambda x: (x.get("yoy") is None, -(x.get("yoy") or 0))),
        "history": history[-12:],
        "thresholds": {
            "yoy_min": YOY_MIN, "q_avg_min": Q_AVG_MIN, "amount_min": AMOUNT_MIN,
            "streak_yoy": STREAK_YOY, "grades_shown": list(GRADES_SHOWN),
            "note": "기업별 수출실적은 비공개 — 소재지 시군구 × 품목 조합을 프록시로 사용하고 "
                    "분기매출 상관계수로 검증(A≥0.85/B≥0.70). 해외 생산분 미반영, 통관·매출인식 시차 존재. "
                    "임계값은 백테스트 미검증 — 발굴 보조용.",
        },
        "fx": {"usd_krw": fx.get(month), "months": len(fx)},
        "coverage": {
            "graded_a": graded.count("A"), "graded_b": graded.count("B"), "graded_c": graded.count("C"),
            "combos_ok": len(series_by_combo), "combos_total": total, "failed": failed,
        },
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
    logger.info("저장: %s (기준월 %s, 후보 %d, 품목 %d, 조합 %d/%d)",
                OUT_PATH, data["data_month"], len(data["candidates"]),
                len(data["items"]), cov["combos_ok"], cov["combos_total"])


if __name__ == "__main__":
    main()
