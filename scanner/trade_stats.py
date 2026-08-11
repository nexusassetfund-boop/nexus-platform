"""
종목별 수출 추적기 — 공장 소재 시군구의 품목 수출을 그 기업의 매출 프록시로 본다.

설계 배경: 처음에는 품목 163개를 증감률 순으로 나열했는데 "참고할 게 없다"는 평가를
받았다. 어디를 봐야 하는지가 없고 종목으로 좁혀지지 않았다. BeOn 채널을 역설계한 결과
알파는 **시군구 × HS품목**에 있었다 — "파마리서치 : 리쥬란 (강원 강릉시)" 처럼 공장이
하나뿐인 지역의 품목 수출은 사실상 그 회사의 월매출이고, **분기 실적 발표 2~7주 전에**
관측된다.

핵심은 자동 매칭이 아니라 큐레이션이다. DART 본점 주소는 등기상 본사(기아=서초구)라
쓰면 안 되고, 생산 거점을 사람이 지정해야 한다. stock_trade_map.json에 손으로 적고
verify_trade_map.py로 시도 내 점유율·순위를 확인해 다듬는다.

파이프라인:
  1) trade_map.json(검증 통과한 종목↔지역×HS)에서 대상 수집
  2) 시군구별 API로 60개월 시계열 — "역대 대비 어디인가"를 보려면 장기간이 필요하다
  3) 지표: 전월비 / 전년동월비 / 분기평균 / 누적 / 연속개월 / 역대최고 / 12개월최고
  4) 관세청 환율로 원화 기준 증감률 병기 — USD 기준과의 차이가 환율이 만든 착시분
  5) 10대 품목 순별 잠정치를 매크로 띠로 부착(21일 공개 — 확정치보다 40일 빠름)
출력: docs/data/trade.json (프론트 '전략실 > 수출입' 탭)

주의: 두 API의 금액 단위가 다르다. **시군구별 = 천USD, 품목별 = USD.**
      증감률 하한을 단위에 맞춰 넘기지 않으면 지표가 통째로 None이 된다.

해석 문구를 자동 생성하지 않는다. 수치와 시계열만 제시하고 판단은 사용자 몫이다.
실행: 매일 07:00 KST trade-stats.yml. 실패 시 exit 1로 기존 산출물 보존.
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
SRC_MAP_PATH = Path(__file__).parent / "data" / "stock_trade_map.json"  # import_watch 큐레이션
SCAN_PATH = ROOT / "docs" / "data" / "scan.json"

# ── 기준 ──
# 시군구 API 단위는 **천USD**. 증감률 기준월이 이보다 작으면 분모가 0에 가까워
# 증감률이 폭주하므로 산출하지 않는다($50k 상당).
BASE_MIN = 50.0
STREAK_YOY = 10.0         # '연속 성장' 판정 기준 YoY (%)
# 전년동월이 자기 시계열의 양 극단이면 증감률이 과장된다. 낮은 쪽만 보면 안 된다 —
# 효성중공업 변압기는 전년동월(2025-06)이 24개월 중 최대값이라 -88%가 찍혔지만
# 12개월 이동합계로는 횡보였다. 높은 기준월도 똑같이 경고해야 한다.
BASE_EFFECT_Q = 0.25      # 하위 25% 미만 → base_effect (증가율 과장)
BASE_SPIKE_Q = 0.90       # 상위 10% 초과 → base_spike (감소율 과장)
# 월 편차가 큰 품목(대형 수주품)은 월 단위 증감률이 노이즈다. 최대/최소 배수가
# 이 값을 넘으면 lumpy로 표시하고 12개월 이동합계를 함께 본다.
LUMPY_RATIO = 10.0
# 월 수출 하한(천USD). 미만이면 small_scale로 표시한다 — 한국타이어처럼 연매출 9조원대
# 회사가 월 $165K로 잡히면 그건 매출 프록시가 아니라 잔여 물량이다.
SMALL_SCALE = 1000.0
MONTHS = 60               # 수집 개월 수 — 역대 대비 위치를 보려면 장기간이 필요


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("%s 로드 실패: %s", path.name, e)
        return default


def _pct(cur, prev, base_min: float | None = None):
    """증감률(%). 기준값이 없거나 0이거나 너무 작으면 None.

    base_min을 기본 인자로 묶지 않는다 — 기본 인자는 정의 시점에 값이 고정돼
    모듈 상수를 바꿔도 반영되지 않는다. 단위가 API마다 다르므로 호출 시점에
    결정해야 한다. 0을 넘기면 하한을 끄는 뜻(단가·물량 등 금액이 아닌 지표).
    """
    if cur is None or prev is None or prev == 0:
        return None
    floor = BASE_MIN if base_min is None else base_min
    if floor and abs(prev) < floor:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _prev_yymm(yymm: str, back: int) -> str:
    y, m = int(yymm[:4]), int(yymm[4:])
    total = y * 12 + (m - 1) - back
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def _quarter_months(yymm: str) -> list[str]:
    y, m = int(yymm[:4]), int(yymm[4:])
    q = ((m - 1) // 3) * 3 + 1
    return [f"{y:04d}{q + i:02d}" for i in range(3)]


def series_for(rows: list[dict], sgg: str) -> dict[str, dict]:
    """시도 응답에서 해당 시군구 행만 골라 {YYYYMM: {amt, qty}}.

    시군구 API는 시도 단위로 요청하면 그 안의 모든 시군구가 함께 온다. 골라내지
    않으면 같은 도의 타 지역이 섞여 프록시가 무너진다. 중량이 없어 qty는 건수다.
    """
    out: dict[str, dict] = {}
    for r in rows:
        if str(r.get("sgg", "")).strip() != sgg:
            continue
        p = "".join(c for c in str(r.get("period", "")) if c.isdigit())[:6]
        if len(p) != 6:
            continue
        cur = out.setdefault(p, {"amt": 0.0, "qty": 0.0})
        cur["amt"] += r.get("exp_amt") or 0.0
        cur["qty"] += r.get("exp_cnt") or 0.0
    return out


def compute_metrics(s: dict[str, dict], month: str) -> dict | None:
    cur = s.get(month)
    # 0은 유효한 값이다. falsy로 걸러내면 통관이 0이 된 종목 — 가장 강한 경고 신호 —
    # 이 목록에서 통째로 사라져 사용자가 나쁜 소식을 못 본다. None(결측)만 걸러낸다.
    if not cur or cur.get("amt") is None:
        return None
    amt = cur["amt"]
    prev_y = (s.get(_prev_yymm(month, 12)) or {}).get("amt")
    prev_m = (s.get(_prev_yymm(month, 1)) or {}).get("amt")

    q_yoys = []
    for qm in _quarter_months(month):
        if qm > month:
            continue
        v = _pct((s.get(qm) or {}).get("amt"), (s.get(_prev_yymm(qm, 12)) or {}).get("amt"))
        if v is not None:
            q_yoys.append(v)

    year = month[:4]
    ytd = sum(v.get("amt", 0.0) for k, v in s.items() if k[:4] == year and k <= month)
    ytd_prev = sum(v.get("amt", 0.0) for k, v in s.items()
                   if k[:4] == str(int(year) - 1) and k[4:] <= month[4:])

    streak = 0
    for back in range(24):
        mm = _prev_yymm(month, back)
        v = _pct((s.get(mm) or {}).get("amt"), (s.get(_prev_yymm(mm, 12)) or {}).get("amt"))
        if v is None or v <= STREAK_YOY:
            break
        streak += 1

    # 역대/12개월 최고 — Y/Y·M/M만 보면 기저효과와 구분이 안 되는데 이 플래그가 보정한다.
    # 창은 기준월을 기준으로 잡는다. 계열 끝을 쓰면 API가 기준월 이후의 부분집계월을
    # 돌려줄 때 창이 어긋난다.
    def _vals(n: int | None = None) -> list[float]:
        keys = sorted(s) if n is None else [_prev_yymm(month, k) for k in range(n)]
        return [s[k]["amt"] for k in keys
                if k in s and k <= month and s[k].get("amt") is not None]

    ordered = _vals()
    ath = bool(ordered) and amt >= max(ordered)
    recent = _vals(12)
    high_12m = bool(recent) and amt >= max(recent)

    flags = []
    if amt == 0:
        flags.append("zero_month")   # 통관 없음 — 사라뜨리지 말고 드러낸다

    # 분포는 0인 달도 포함해야 한다. 0을 빼면 간헐적으로 멈추는 품목의 분위수가 왜곡된다.
    amts = sorted(v["amt"] for v in s.values() if v.get("amt") is not None)
    if prev_y is not None and amts:
        if prev_y <= amts[max(0, int(len(amts) * BASE_EFFECT_Q))]:
            flags.append("base_effect")
        elif prev_y >= amts[min(len(amts) - 1, int(len(amts) * BASE_SPIKE_Q))]:
            flags.append("base_spike")

    # ── 12개월 이동합계. **달력 기준**으로 창을 잡는다.
    # 존재하는 행의 위치 인덱스로 자르면 결측월이 있을 때 창이 늘어난다 — 한국타이어는
    # 60개월 중 25개월이 결측이라 '12개월 누적'이 실제로는 16~17개월 합이었다.
    # 결측이 하나라도 있으면 값을 내지 않고 ttm_partial로 표시한다.
    def _window_sum(end_month: str, n: int = 12):
        keys = [_prev_yymm(end_month, k) for k in range(n)]
        vals = [s[k]["amt"] for k in keys if k in s and s[k].get("amt") is not None]
        return (sum(vals), len(vals) == n)

    ttm, ttm_full = _window_sum(month)
    ttm_prev, prev_full = _window_sum(_prev_yymm(month, 12))
    ttm_partial = not (ttm_full and prev_full)
    if ttm_partial:
        flags.append("ttm_partial")
        ttm = ttm_prev = None

    # 변동성 판정도 0인 달을 포함한다. 0을 빼면 20/24개월이 0인 완전 간헐성 품목이
    # 오히려 lumpy로 안 잡힌다(0이 아닌 최솟값끼리 비교하게 되므로).
    recent24 = _vals(24)
    nz = [v for v in recent24 if v > 0]
    lumpy = bool(nz) and (max(recent24) / max(1e-9, min(recent24)) >= LUMPY_RATIO
                          or len(nz) < len(recent24) * 0.75)
    if lumpy:
        flags.append("lumpy")

    # 분기 합계 대 합계 — 실적은 분기 합계로 발표되므로 컨센서스 대조에는 이쪽이 맞다.
    # q_avg_yoy(월별 증감률의 단순 평균)는 금액 가중이 아니라 값이 다르게 나온다.
    q_ms = [m for m in _quarter_months(month) if m <= month]
    q_sum = sum((s.get(m) or {}).get("amt") or 0.0 for m in q_ms)
    q_sum_prev = sum((s.get(_prev_yymm(m, 12)) or {}).get("amt") or 0.0 for m in q_ms)

    return {
        "amount": round(amt, 1),
        "yoy": _pct(amt, prev_y), "mom": _pct(amt, prev_m),
        "q_avg_yoy": round(statistics.fmean(q_yoys), 1) if q_yoys else None,
        "q_sum_yoy": _pct(q_sum, q_sum_prev),
        "q_progress": f"{len(q_ms)}/3",
        "q_sum": round(q_sum, 1),
        "cum_yoy": _pct(ytd, ytd_prev), "streak": streak,
        # 12개월 이동합계와 그 전년비 — 월 편차가 큰 품목의 진짜 추세
        "ttm": round(ttm, 1) if ttm is not None else None,
        "ttm_yoy": _pct(ttm, ttm_prev),
        "ttm_partial": ttm_partial,
        "ath": ath, "high_12m": high_12m, "flags": flags,
    }


def latest_confirmed_month(today: dt.date) -> str:
    """확정치 기준월 후보(최신부터). 매월 15일경 전월분이 현행화된다.

    day>=16에서만 M-1을 보면 **15일 실행이 11일 실행과 같은 결과**를 낸다 —
    확정치가 그날 나오는데 6일을 버린다. M-1을 먼저 시도하고 데이터가 없으면
    호출부에서 M-2로 물러난다.
    """
    return _prev_yymm(f"{today.year:04d}{today.month:02d}", 1)


def latest_month_in(series: dict, cap: str) -> str | None:
    """계열에 실제로 들어온 달 중 cap 이하 최신월. 없으면 None.

    YYYYMM 문자열은 사전순 비교가 곧 시간순이라 그대로 비교한다. cap을 두는 이유는
    월중 부분 집계를 걸러내기 위해서다 — 상한이 없으면 아직 안 끝난 달이 완결월처럼
    최신값이 돼 가짜 급감으로 보인다.
    """
    return max((m for m in series if m <= cap), default=None)


# 순별 잠정치 응답은 itemUsdAmt00~10 열로 오고 품목명이 없다. 순서는 실측으로 검증했다
# (반도체 221억달러·+180.6%, 컴퓨터주변기기 +231.9%, 승용차 -10.6%가 관세청 보도자료와 일치).
_PROV_EXP = ["전체", "반도체", "철강제품", "승용차", "석유제품", "무선통신기기",
             "선박", "자동차부품", "컴퓨터주변기기", "정밀기기", "가전제품"]
_PROV_IMP = ["전체", "반도체", "원유", "기계류", "가스", "반도체 제조용장비",
             "정밀기기", "석유제품", "무선통신기기", "승용차", "석탄"]


def fetch_macro(api_key: str, yymm: str) -> dict | None:
    """10대 품목 순별 잠정치 + 전년동기간 대비.

    1~20일치가 21일에 공개돼 월별 확정치(익월 15일)보다 40일 빠르다. 품목이 10개로
    고정이라 종목 스크리닝에는 못 쓰고 섹터 방향 파악용이다.
    전년비는 반드시 **같은 기간끼리** 비교한다(1~20일 vs 전년 1~20일).
    미승인(403)이면 조용히 건너뛴다.
    """
    prev_yymm = f"{int(yymm[:4]) - 1}{yymm[4:]}"
    out = {"items": [], "import_items": []}
    for imex, names, key in (("1", _PROV_EXP, "items"), ("2", _PROV_IMP, "import_items")):
        try:
            cur_rows = capi.fetch_provisional(yymm, imex, api_key, CACHE_PATH)
            prv_rows = capi.fetch_provisional(prev_yymm, imex, api_key, CACHE_PATH)
        except capi.CustomsError as e:
            logger.warning("순별 잠정치 사용 불가(%s) — 매크로 띠는 생략합니다", str(e)[:70])
            return None
        if not cur_rows:
            return None
        # 가장 긴 기간(1~말일 > 1~20 > 1~10)을 최신으로 본다
        cur = sorted(cur_rows, key=lambda r: len(str(r.get("priodDt") or "")))[-1]
        period = str(cur.get("priodDt") or "")
        prv = next((r for r in prv_rows if str(r.get("priodDt")) == period), None)
        for i, nm in enumerate(names):
            if i == 0:
                continue          # 전체는 헤더에서 따로 쓴다
            a = cur.get(f"itemUsdAmt{i:02d}")
            b = prv.get(f"itemUsdAmt{i:02d}") if prv else None
            if a is None:
                continue
            out[key].append({"name": nm, "amt": a, "yoy": _pct(a, b, 0)})
        out["period"] = f"{yymm[:4]}.{yymm[4:]} {period}"
        # 수출·수입 총액을 각각 담는다 — 하나의 키에 쓰면 뒤에 도는 수입이 덮어쓴다.
        pfx = "" if imex == "1" else "import_"
        out[f"{pfx}total"] = cur.get("itemUsdAmt00")
        out[f"{pfx}total_yoy"] = _pct(cur.get("itemUsdAmt00"),
                                      prv.get("itemUsdAmt00") if prv else None, 0)
    # 증감률이 큰 순으로 — 눈에 띄어야 할 것이 앞에 오도록
    out["items"].sort(key=lambda x: (x["yoy"] is None, -(x["yoy"] or 0)))
    out["import_items"].sort(key=lambda x: (x["yoy"] is None, -(x["yoy"] or 0)))
    return out


def build() -> dict | None:
    tmap = _load_json(MAP_PATH, None)
    if not tmap or not tmap.get("entries"):
        logger.error("검증된 매핑 없음 (%s) — verify_trade_map.py를 먼저 실행하세요.", MAP_PATH)
        return None

    api_key = capi.require_key()
    today = dt.datetime.now(tz=KST).date()
    month = latest_confirmed_month(today)
    # month는 아래에서 시군구 응답에 맞춰 뒤로 물러날 수 있다. 수입 워치는 다른 API를
    # 쓰므로 그 후퇴에 끌려가면 안 된다 — 물러나기 전의 상한을 따로 붙잡아 둔다.
    confirmed_cap = month
    chunks = capi.month_range(month, MONTHS)

    scan = _load_json(SCAN_PATH, {})
    prices = {str(r.get("ticker")).zfill(6): r for r in (scan.get("results") or [])}

    # 같은 (시도, HS)를 쓰는 종목이 여럿이면 호출을 한 번만 한다.
    raw: dict[tuple[str, str], list[dict]] = {}
    entries = tmap["entries"]
    for e in entries:
        key = (e.get("sido"), e.get("hs_used"))
        if not all(key) or key in raw:
            continue
        rows: list[dict] = []
        try:
            for s, en in chunks:
                rows.extend(capi.fetch_district(key[1], key[0], s, en, api_key, CACHE_PATH))
        except capi.CustomsError as ex:
            logger.warning("수집 실패 sido=%s hs=%s: %s", key[0], key[1], ex)
            continue
        raw[key] = rows

    # 확정치 공개 직후에는 M-1이 아직 비어 있을 수 있다. 실제로 데이터가 들어온
    # 최신월을 기준월로 삼는다 — 이렇게 해야 15일 실행이 새 확정치를 바로 집는다.
    seen_months = {
        "".join(c for c in str(r.get("period", "")) if c.isdigit())[:6]
        for rows in raw.values() for r in rows
    }
    seen_months = {m for m in seen_months if len(m) == 6 and m <= month}
    if seen_months:
        newest = max(seen_months)
        if newest != month:
            logger.info("기준월 %s → %s (확정치 미공개로 한 달 물러남)", month, newest)
            month = newest
    else:
        logger.error("수집 데이터에 유효한 기간이 없음 — 기존 파일 보존")
        return None

    fx: dict[str, float] = {}
    for m in {month, _prev_yymm(month, 12)}:
        try:
            r = capi.fetch_fx_month(m, api_key, CACHE_PATH)
        except capi.CustomsError as e:
            logger.warning("관세환율 사용 불가(%s) — 원화 기준은 생략합니다", str(e)[:70])
            fx = {}
            break
        if r:
            fx[m] = r

    out, failed = [], 0
    for e in entries:
        rows = raw.get((e.get("sido"), e.get("hs_used")))
        if not rows:
            failed += 1
            continue
        s = series_for(rows, e.get("sgg") or "")
        m = compute_metrics(s, month)
        if not m:
            failed += 1
            continue

        prev = _prev_yymm(month, 12)
        yoy_krw = None
        if fx.get(month) and fx.get(prev):
            # 하한도 함께 환산해야 한다. 기본값(천USD 기준)을 그대로 두면 원화 금액에는
            # 실효 하한이 환율배(약 1,500배) 느슨해져 USD 기준은 None인데 원화만 숫자가
            # 찍히는 조합이 생긴다.
            yoy_krw = _pct((s.get(month) or {}).get("amt", 0.0) * fx[month],
                           (s.get(prev) or {}).get("amt", 0.0) * fx[prev],
                           BASE_MIN * fx[prev])

        # 회사 규모 대비 통관액이 미미하면 프록시 전제가 깨진 것이다(해외 생산 비중이
        # 크거나 매핑이 어긋난 경우). 자동으로 빼지 않고 표시만 한다 — 규모 판단에는
        # 회사 지식이 필요하고, 티씨케이처럼 매핑은 정확한데 회사가 작은 경우도 있다.
        if (m.get("amount") or 0) < SMALL_SCALE:
            m.setdefault("flags", []).append("small_scale")

        # 급감 경고 — 백테스트에서 가장 신뢰도 높은 신호. 분기 -30% 진입 후 6개월
        # 초과수익이 중앙 -16.1%(승률 31%)였다. 유니버스 선택 편향은 이 결과를
        # 좋게 만드는 방향인데도 뚜렷이 나빴다.
        if m.get("q_sum_yoy") is not None and m["q_sum_yoy"] <= -30:
            m.setdefault("flags", []).append("q_drop")

        sc = prices.get(str(e.get("ticker")).zfill(6), {})
        out.append({
            "ticker": e.get("ticker"), "name": e.get("name"),
            "label": e.get("label"), "hs": e.get("hs_used"),
            "region": e.get("sgg"), "share": e.get("share"),
            "rank": e.get("rank"), "of": e.get("of"),
            "grade": e.get("grade"), "shared": bool(e.get("shared")),
            "note": e.get("note"), "yoy_krw": yoy_krw,
            "price": sc.get("current_price"), "change_pct": sc.get("change_pct"),
            "pct_12m": sc.get("pct_12m"),
            "series": [{"m": k, "amt": round(s[k]["amt"], 1)} for k in sorted(s)],
            **m,
        })

    if entries and len(out) < len(entries) / 2:
        logger.error("수집 성공이 절반 미만 (%d/%d) — 기존 파일 보존", len(out), len(entries))
        return None

    # ── 리포트 재료. LLM에게 카드 52장을 통째로 주지 않고 요리된 재료를 준다.
    #    세종의 선별 기준(원문 공개)은 레벨이 아니라 **추세 변화**다 —
    #    "최근 2~3개월 추이를 확인하고 추세 변화가 뚜렷한 기업을 고른다".
    IND_GROUPS = {
        "양극재·2차전지": ["양극재", "리튬염", "전해질", "축전지"],
        "화장품·미용": ["기초화장", "메이크업", "미용기기", "마스크"],
        "바이오·의료": ["독소", "면역", "의료기기", "치과", "올리고", "조제식료품", "진단"],
        "반도체": ["집적회로", "반도체", "리드프레임", "검사장비", "제조용 장비", "내화물"],
        "조선": ["탱커"],
        "철강·금속": ["철강관", "알루미늄", "베어링"],
        "기계·전력": ["개폐", "연료전지", "철도", "온수기", "레이저"],
    }

    def _q(stk, cur_q=True):
        ser = {p["m"]: p["amt"] for p in stk["series"]}
        ms = [m for m in _quarter_months(month) if m <= month]
        if not cur_q:
            ms = [_prev_yymm(m, 12) for m in ms]
        return sum(ser.get(m, 0.0) for m in ms)

    industries = []
    grouped: set[str] = set()
    for gname, kws in IND_GROUPS.items():
        members = [x for x in out if any(k in (x.get("label") or "") for k in kws)]
        if len(members) < 2:
            continue
        rows_g = []
        for x in members:
            grouped.add(x["ticker"])
            a, b = _q(x), _q(x, False)
            rows_g.append({"ticker": x["ticker"], "name": x["name"],
                           "q_sum": round(a, 1), "q_sum_yoy": _pct(a, b),
                           "ath": x.get("ath"), "flags": x.get("flags")})
        rows_g.sort(key=lambda r: (r["q_sum_yoy"] is None, -(r["q_sum_yoy"] or 0)))
        ga = sum(r["q_sum"] for r in rows_g)
        gb = sum(_q(x, False) for x in members)
        industries.append({"group": gname, "q_sum": round(ga, 1),
                           "q_sum_yoy": _pct(ga, gb), "stocks": rows_g})

    # 추세 변화 movers: 신규 ATH / 분기 상·하위 / 부호 전환(최근 3개월 vs 이전 3개월)
    history = _load_json(HISTORY_PATH, [])
    prev_rec = next((h for h in reversed(history) if h.get("month") != month), {})
    prev_ath = set(prev_rec.get("ath") or [])
    prev_picks = set(prev_rec.get("picks") or [])

    def _turn(stk):
        ser = {p["m"]: p["amt"] for p in stk["series"]}
        recent = sum(ser.get(_prev_yymm(month, k), 0.0) for k in range(3))
        before = sum(ser.get(_prev_yymm(month, k), 0.0) for k in range(3, 6))
        return _pct(recent, before, BASE_MIN * 3)

    movers = {
        "new_ath": [x["ticker"] for x in out
                    if x.get("ath") and x["ticker"] not in prev_ath],
        "q_top": [x["ticker"] for x in sorted(
            (x for x in out if x.get("q_sum_yoy") is not None),
            key=lambda x: -x["q_sum_yoy"])[:10]],
        "q_bottom": [x["ticker"] for x in sorted(
            (x for x in out if x.get("q_sum_yoy") is not None),
            key=lambda x: x["q_sum_yoy"])[:5]],
        "turning": {x["ticker"]: _turn(x) for x in out
                    if _turn(x) is not None and abs(_turn(x)) >= 30},
        "prev_picks": sorted(prev_picks),
    }

    report_input = {
        "industries": industries,
        "movers": movers,
        "ungrouped": [x["ticker"] for x in out if x["ticker"] not in grouped],
    }

    if not history or history[-1].get("month") != month:
        history.append({"month": month, "date": today.isoformat(),
                        "ath": [x["ticker"] for x in out if x.get("ath")]})
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(history[-24:], ensure_ascii=False, indent=1),
                                encoding="utf-8")

    # ── 수입 선행 워치 — 원재료·장비 수입은 생산 확대를 앞선다(BeOn: MR-MUF→하이닉스,
    #    펄프→제지). 전국 품목별(Itemtrade)의 imp를 쓴다. **단위가 USD**라(시군구는 천USD)
    #    시리즈를 천USD로 환산해 화면 단위를 통일한다.
    imports = []
    watch = (_load_json(SRC_MAP_PATH, {}) or {}).get("import_watch") or []
    for w in watch:
        hs = w.get("hs")
        if not hs:
            continue
        rows: list[dict] = []
        try:
            for s0, e0 in chunks:
                rows.extend(capi.fetch_item(hs, s0, e0, api_key, CACHE_PATH))
        except capi.CustomsError as ex:
            logger.warning("수입 워치 실패 hs=%s: %s", hs, ex)
            continue
        ser: dict[str, dict] = {}
        for r in rows:
            p = "".join(c for c in str(r.get("period", "")) if c.isdigit())[:6]
            if len(p) != 6 or r.get("imp_amt") is None:
                continue
            cur = ser.setdefault(p, {"amt": 0.0, "qty": 0.0})
            cur["amt"] += r["imp_amt"] / 1000.0          # USD → 천USD
            cur["qty"] += (r.get("imp_wgt") or 0.0)
        # 기준월을 종목 카드(시군구별)와 공유하지 않는다. 품목별 통계는 시군구별보다
        # 먼저 갱신돼, 시군구가 한 달 물러난 기간(매월 1~14일)에는 이미 받아둔 최신월을
        # 통째로 버리게 된다 — 8/11 실행에서 202607이 시리즈에 있는데도 202606으로
        # 표시됐다. 원재료·장비 수입은 생산 확대를 앞서 보는 선행 지표라 시의성이 핵심이다.
        # 품목마다 갱신이 어긋날 수 있으므로 각자의 최신월을 쓴다. 서로 비교하는 카드가
        # 아니라 각 품목을 자기 이력과 대조하는 카드라 기준월이 달라도 읽는 데 문제없다.
        # 상한(confirmed_cap)은 둔다 — 없으면 월중 부분 집계가 완결월처럼 찍혀 가짜 급감이 된다.
        imp_month = latest_month_in(ser, confirmed_cap)
        met = compute_metrics(ser, imp_month) if imp_month else None
        if not met:
            continue
        imports.append({
            "hs": hs, "label": w.get("label"), "note": w.get("note"),
            "month": imp_month,
            "stocks": w.get("stocks") or [],
            "series": [{"m": k, "amt": round(ser[k]["amt"], 1)} for k in sorted(ser)][-24:],
            **met,
        })
    imports.sort(key=lambda x: (x.get("yoy") is None, -(x.get("yoy") or -1e9)))

    # 월초에는 당월 잠정치가 아직 없다(1~10일치는 11일 공개) — 전월로 물러난다.
    this_month = f"{today.year:04d}{today.month:02d}"
    macro = fetch_macro(api_key, this_month)
    if not macro or not macro.get("items"):
        macro = fetch_macro(api_key, _prev_yymm(this_month, 1))
    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "data_month": month,
        "stocks": sorted(out, key=lambda x: (x.get("yoy") is None, -(x.get("yoy") or -1e9))),
        "macro": macro,
        "imports": imports,
        "report_input": report_input,
        "fx": {"usd_krw": fx.get(month), "months": len(fx)},
        "thresholds": {
            "streak_yoy": STREAK_YOY, "base_min": BASE_MIN,
            "note": "공장 소재 시군구의 품목 수출액입니다. 그 기업의 확정 매출이 아니며, "
                    "같은 지역에 동종 업체가 있으면 섞입니다. 해외 생산분은 한국 통계에 "
                    "잡히지 않고, 통관 시점과 매출 인식 시점에 시차가 있습니다. "
                    "금액 단위는 천USD입니다.",
        },
        "coverage": {"stocks": len(out), "mapped": len(entries), "failed": failed},
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        data = build()
    except capi.CustomsError as e:
        logger.error("수출 추적 실패: %s", e)
        sys.exit(1)
    if data is None:
        logger.error("수출 추적 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    cov = data["coverage"]
    ath = len([x for x in data["stocks"] if x.get("ath")])
    logger.info("저장: %s (기준월 %s · 종목 %d/%d · 역대최고 %d · 매크로 %s)",
                OUT_PATH, data["data_month"], cov["stocks"], cov["mapped"], ath,
                "있음" if data.get("macro") else "없음")


if __name__ == "__main__":
    main()
