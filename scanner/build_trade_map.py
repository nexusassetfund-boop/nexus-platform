"""
수출입 매칭 테이블 빌더 — 종목 ↔ (소재지 시군구 × 품목 HS) 프록시를 만들고 상관계수로 검증.

왜 이런 우회를 하나: 기업별 수출실적은 관세법상 비공개다. DART 사업보고서의 품목별 매출
표도 실증 결과 쓸 수 없었다(삼성전자는 4행뿐, 현대모비스는 품목 분해 자체가 없고, 품목이
잘 나오는 리노공업은 명칭이 자사 고유명 "LEENO PIN"이라 HS 매칭 불가). 세종기업데이터가
공개한 방식 — "코스맥스-경기 수출_화성시" 처럼 소재지 지역의 품목 수출을 프록시로 쓰는 것 —
이 현실적으로 유일하게 성립하는 경로다.

파이프라인:
  1) sector_map.json 테마 → 소속 종목 수집 (테마명이 DRAM·HBM·MLCC 수준이라 HS 품목명과 겹침)
  2) DART corpCode + 기업개황(company.json)으로 본점 주소 → 시도코드 + 시군구명
     (API는 sidoCd 2자리로 요청하고 응답이 시군구별로 쪼개져 오므로 둘 다 필요하다)
  3) 테마명 ↔ 관세청 HS 품목명 문자열 유사도로 HS 4~6단위 후보 생성
  4) 후보 (시군구 × HS) 조합별 월별 수출액 수집 → 분기 합산
  5) DART 분기 매출과 상관계수 계산 → 최고 상관 조합 채택 (A≥0.85 / B≥0.70 / C≥0.50)
  6) 채택 조합에 대해 분기매출 ≈ α + β × 분기수출액 회귀 + 과거 오차 밴드 저장
출력: scanner/data/trade_map.json (trade_stats.py가 읽음)

한계(출력에 그대로 기록해 화면에서 표시한다):
  - 같은 지역·같은 품목에 여러 종목이 걸리면 귀속이 불확실하다 → shared 플래그
  - 해외 생산분은 한국 수출통계에 잡히지 않는다 (현대모비스 해외매출 34.4조가 대표 사례)
  - 통관 시점과 매출 인식 시점에 시차가 있다

실행: 스케줄 없음. 분기 실적 발표 후 trade-map-rebuild.yml 을 수동 실행(workflow_dispatch).
      호출량이 크므로 (조합 수 × 3년) 개발계정 10,000회 한도를 고려해 TRADE_MAP_LIMIT 로 제한 가능.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import os
import statistics
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import customs_api as capi
import fetch_value

logger = logging.getLogger("trade_map")

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = Path(__file__).parent / "data" / "trade_map.json"
CACHE_PATH = ROOT / "docs" / "data" / "trade_raw_cache.json"
SECTOR_MAP = ROOT.parent / "nexus-cloud" / "public" / "sector_map.json"
HS_MASTER = Path(__file__).parent / "data" / "hs_master.json"   # 관세청 HS부호 파일데이터 가공본

# ── 기준 ──
CORR_A, CORR_B, CORR_C = 0.85, 0.70, 0.50
MIN_QUARTERS = 8          # 상관계수 신뢰를 위한 최소 분기 수 (2년)
YEARS = 4                 # 수집 연수
SIM_CUTOFF = 0.6          # 테마명 ↔ HS 품목명 문자열 유사도 하한
MAX_HS_PER_THEME = 3      # 테마당 HS 후보 상한 (API 호출량 억제)

# 시도명 → 시군구 코드 접두. 관세청 지역코드 체계는 행정표준코드를 따른다.
# DART 주소는 "충청남도 아산시"처럼 정식 명칭으로 오므로 축약형·정식형을 모두 받는다.
# 긴 이름부터 검사해야 "전북"이 "전라북도"를 가로채지 않는다.
_SIDO_PREFIX = {
    "서울": "11", "부산": "26", "대구": "27", "인천": "28", "광주": "29",
    "대전": "30", "울산": "31", "세종": "36", "경기": "41",
    "강원": "42", "충북": "43", "충남": "44", "전북": "45", "전남": "46",
    "경북": "47", "경남": "48", "제주": "50",
    "충청북도": "43", "충청남도": "44", "전라북도": "45", "전라남도": "46",
    "경상북도": "47", "경상남도": "48", "강원특별자치도": "42",
    "제주특별자치도": "50", "전북특별자치도": "45",
}
_SIDO_ORDERED = sorted(_SIDO_PREFIX.items(), key=lambda kv: -len(kv[0]))


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("%s 로드 실패: %s", path.name, e)
        return default


def _dart_company(corp_code: str) -> dict | None:
    """DART 기업개황 — 본점 주소(adres)를 얻는다."""
    url = f"{fetch_value._DART}/company.json?crtfc_key={fetch_value.DART_KEY}&corp_code={corp_code}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if d.get("status") == "000" else None
    except Exception as e:
        logger.debug("기업개황 실패 %s: %s", corp_code, e)
        return None


def region_from_address(adres: str) -> tuple[str | None, str | None, str | None]:
    """본점 주소 → (시도코드 2자리, 시군구명, 표시명).

    API는 sidoCd(시도 2자리)로 요청하고 응답이 시군구(sggNm)별로 쪼개져 오므로,
    요청에 쓸 시도코드와 응답에서 골라낼 시군구명을 둘 다 뽑아야 한다.
    """
    if not adres:
        return None, None, None
    parts = adres.split()
    if not parts:
        return None, None, None
    sido = parts[0]
    code = next((v for k, v in _SIDO_ORDERED if sido.startswith(k)), None)
    if not code:
        return None, None, None
    # 광역시/특별시는 두 번째 토큰이 구(예: 강남구), 도는 시/군(예: 화성시)
    sgg = parts[1] if len(parts) > 1 and parts[1][-1] in "시군구" else None
    return code, sgg, (f"{sido} {sgg}" if sgg else sido)


def match_sgg(addr_sgg: str | None, available: list[str]) -> str | None:
    """주소에서 뽑은 시군구명을 응답의 sggNm 표기에 맞춘다(표기 흔들림 흡수)."""
    if not addr_sgg or not available:
        return None
    if addr_sgg in available:
        return addr_sgg
    base = addr_sgg.rstrip("시군구")
    for a in available:
        if a == base or a.startswith(base) or base and base in a:
            return a
    hit = difflib.get_close_matches(addr_sgg, available, n=1, cutoff=0.7)
    return hit[0] if hit else None


def hs_candidates(theme: str, hs_master: list[dict]) -> list[tuple[str, str]]:
    """테마명과 HS 한글 품목명의 문자열 유사도로 후보 HS를 뽑는다."""
    if not theme or not hs_master:
        return []
    scored = []
    for row in hs_master:
        name = row.get("name") or ""
        if not name:
            continue
        ratio = difflib.SequenceMatcher(None, theme, name).ratio()
        if theme in name or name in theme:
            ratio = max(ratio, 0.8)
        if ratio >= SIM_CUTOFF:
            scored.append((ratio, row.get("hs"), name))
    scored.sort(reverse=True)
    return [(hs, nm) for _, hs, nm in scored[:MAX_HS_PER_THEME] if hs]


def quarterly_revenue(corp_code: str, years: list[int]) -> dict[str, float]:
    """DART 분기 매출 — {YYYYQn: 매출액}. 누적 공시이므로 차분해 분기값을 만든다."""
    out = {}
    # 11013=1분기, 11012=반기, 11014=3분기, 11011=사업보고서(연간)
    codes = [("Q1", "11013"), ("H1", "11012"), ("Q3", "11014"), ("FY", "11011")]
    for y in years:
        cum = {}
        for label, rc in codes:
            url = (f"{fetch_value._DART}/fnlttSinglAcnt.json?crtfc_key={fetch_value.DART_KEY}"
                   f"&corp_code={corp_code}&bsns_year={y}&reprt_code={rc}")
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    d = json.loads(r.read().decode("utf-8"))
            except Exception:
                continue
            if d.get("status") != "000":
                continue
            for it in d.get("list") or []:
                if it.get("account_nm") in ("매출액", "수익(매출액)") and it.get("fs_div") == "CFS":
                    try:
                        cum[label] = float(str(it.get("thstrm_amount", "")).replace(",", ""))
                    except ValueError:
                        pass
                    break
        # 누적 → 분기 차분
        if "Q1" in cum:
            out[f"{y}Q1"] = cum["Q1"]
        if "H1" in cum and "Q1" in cum:
            out[f"{y}Q2"] = cum["H1"] - cum["Q1"]
        if "Q3" in cum and "H1" in cum:
            out[f"{y}Q3"] = cum["Q3"] - cum["H1"]
        if "FY" in cum and "Q3" in cum:
            out[f"{y}Q4"] = cum["FY"] - cum["Q3"]
    return out


def _months_between(start_yymm: str, end_yymm: str) -> list[str]:
    """[start, end] 구간의 YYYYMM 목록."""
    s = int(start_yymm[:4]) * 12 + int(start_yymm[4:]) - 1
    e = int(end_yymm[:4]) * 12 + int(end_yymm[4:]) - 1
    return [f"{t // 12:04d}{t % 12 + 1:02d}" for t in range(s, e + 1)]


def _quarter_key(yymm: str) -> str:
    y, m = int(yymm[:4]), int(yymm[4:])
    return f"{y}Q{(m - 1) // 3 + 1}"


def _corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < MIN_QUARTERS:
        return None
    try:
        return statistics.correlation(xs, ys)
    except (statistics.StatisticsError, ValueError):
        return None


def _linreg(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """단순 선형회귀 → (alpha, beta, 평균 상대오차)."""
    beta, alpha = statistics.linear_regression(xs, ys)
    errs = []
    for x, y in zip(xs, ys):
        pred = alpha + beta * x
        if y:
            errs.append(abs(pred - y) / abs(y))
    return alpha, beta, round(statistics.fmean(errs), 3) if errs else 0.0


def build() -> dict | None:
    api_key = capi.require_key()
    if not fetch_value.DART_KEY:
        logger.error("DART_API_KEY 미설정 — 상관계수 검증 불가")
        return None

    smap = _load(SECTOR_MAP, None)
    if not smap:
        logger.error("sector_map.json 없음: %s", SECTOR_MAP)
        return None
    hs_master = _load(HS_MASTER, [])
    if not hs_master:
        logger.error("HS 마스터 없음 (%s) — data.go.kr '관세청_HS부호' 파일데이터를 "
                     "[{hs, name}] 형태로 가공해 두세요.", HS_MASTER)
        return None

    # ── 테마 → 종목
    theme_of: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for sec in smap.get("sectors") or []:
        for th in sec.get("themes") or []:
            tname = th.get("name") or ""
            # stocks는 [["039570", "HDC랩스"], ...] 형태의 [코드, 이름] 쌍이다.
            for st in th.get("stocks") or []:
                if not isinstance(st, (list, tuple)) or not st:
                    continue
                code = str(st[0]).zfill(6)
                if len(code) != 6 or not code.isdigit():
                    continue
                theme_of.setdefault(code, []).append(tname)
                if len(st) > 1 and st[1]:
                    names[code] = st[1]

    limit = int(os.environ.get("TRADE_MAP_LIMIT", "0") or 0)
    tickers = sorted(theme_of)
    if limit:
        tickers = tickers[:limit]
    logger.info("대상 종목 %d개 (테마 매핑 보유)", len(tickers))

    corp_map = fetch_value._corp_map()
    today = dt.datetime.now(tz=KST).date()
    end_month = f"{today.year:04d}{today.month:02d}"
    chunks = capi.month_range(end_month, YEARS * 12)
    years = list(range(today.year - YEARS, today.year + 1))

    # 환율은 종목과 무관하게 월 단위로 공통이므로 한 번만 받아 재사용한다(호출 한도 1,000).
    all_months = sorted({m for s, e in chunks
                         for m in _months_between(s, e)})
    fx = load_fx(all_months, api_key) or None

    entries: dict[str, dict] = {}
    combo_users: dict[tuple[str, str], list[str]] = {}

    for i, code in enumerate(tickers, 1):
        corp = corp_map.get(code)
        if not corp:
            continue
        info = _dart_company(corp)
        if not info:
            continue
        sido_cd, addr_sgg, region_name = region_from_address(info.get("adres") or "")
        if not sido_cd:
            continue

        rev = quarterly_revenue(corp, years)
        if len(rev) < MIN_QUARTERS:
            logger.debug("%s 분기 매출 부족(%d) — 건너뜀", code, len(rev))
            continue

        best = None
        for theme in theme_of[code]:
            for hs, hs_name in hs_candidates(theme, hs_master):
                rows = []
                try:
                    for s, e in chunks:
                        rows.extend(capi.fetch_district(hs, sido_cd, s, e, api_key, CACHE_PATH))
                except capi.CustomsError as ex:
                    logger.debug("수집 실패 %s/%s: %s", sido_cd, hs, ex)
                    continue
                if not rows:
                    continue
                # 응답은 시도 전체 시군구를 담고 있으므로 이 기업의 시군구만 골라낸다.
                sgg = match_sgg(addr_sgg, _sgg_names(rows))
                merged = _series(rows, sgg, fx)   # USD → 원화 환산 후 매출과 비교
                if not merged:
                    continue
                # 분기 합산 후 매출과 정렬
                q_exp: dict[str, float] = {}
                for mm, amt in merged.items():
                    q_exp[_quarter_key(mm)] = q_exp.get(_quarter_key(mm), 0.0) + amt
                common = sorted(set(q_exp) & set(rev))
                if len(common) < MIN_QUARTERS:
                    continue
                xs = [q_exp[q] for q in common]
                ys = [rev[q] for q in common]
                c = _corr(xs, ys)
                if c is None:
                    continue
                if best is None or c > best["corr"]:
                    alpha, beta, err = _linreg(xs, ys)
                    best = {"corr": round(c, 3), "hs": [hs], "hs_names": [hs_name],
                            "theme": theme, "sgg": sgg, "alpha": round(alpha, 1),
                            "beta": round(beta, 4), "err_band": err, "quarters": len(common)}

        if not best or best["corr"] < CORR_C:
            continue
        grade = "A" if best["corr"] >= CORR_A else "B" if best["corr"] >= CORR_B else "C"
        entries[code] = {
            "name": names.get(code, code), "region": sido_cd, "region_name": region_name,
            "grade": grade, "note": "지역×품목 프록시", **best,
        }
        # 귀속 충돌은 시도가 아니라 (시군구 × 품목) 단위로 봐야 한다.
        combo_users.setdefault((sido_cd, best.get("sgg"), best["hs"][0]), []).append(code)
        if i % 25 == 0:
            logger.info("진행 %d/%d — 채택 %d", i, len(tickers), len(entries))

    # 같은 (지역×품목)에 복수 종목이 걸리면 귀속 불확실 → 플래그
    for combo, users in combo_users.items():
        if len(users) > 1:
            for c in users:
                entries[c]["shared"] = True

    if not entries:
        logger.error("채택된 매칭이 하나도 없음 — 기존 파일 보존")
        return None

    grades = [e["grade"] for e in entries.values()]
    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "method": "소재지 시군구 × 품목(HS) 수출액을 프록시로 사용, 분기매출 상관계수로 검증",
        "fx_adjusted": bool(fx),
        "fx_months": len(fx or {}),
        "criteria": {"corr_a": CORR_A, "corr_b": CORR_B, "corr_c": CORR_C,
                     "min_quarters": MIN_QUARTERS,
                     "note": "수출액(USD)을 관세환율로 원화 환산한 뒤 원화 매출과 상관계수를 낸다"
                             if fx else "환율 미확보 — USD 기준 상관계수(환율 변동이 섞일 수 있음)"},
        "entries": entries,
        "coverage": {
            "graded_a": grades.count("A"), "graded_b": grades.count("B"),
            "graded_c": grades.count("C"), "total_checked": len(tickers),
        },
    }


def _series(rows: list[dict], sgg: str | None = None,
            fx: dict[str, float] | None = None) -> dict[str, float]:
    """{YYYYMM: 수출금액}. sgg가 주어지면 해당 시군구 행만 (시도 요청 시 전 시군구가 함께 온다).

    fx가 주어지면 USD 금액을 그 달의 관세환율로 원화 환산한다. 수출액은 USD인데 공시 매출은
    원화라, 환율이 크게 움직인 구간에서는 환산 없이 상관계수를 내면 환율 변동이 만든 가짜
    상관(또는 상쇄)이 섞인다.
    """
    out: dict[str, float] = {}
    for r in rows:
        if sgg and str(r.get("sgg", "")).strip() != sgg:
            continue
        period = "".join(ch for ch in str(r.get("period", "")) if ch.isdigit())[:6]
        if len(period) != 6 or r.get("exp_amt") is None:
            continue
        amt = r["exp_amt"]
        if fx is not None:
            rate = fx.get(period)
            if rate is None:
                continue          # 환율 없는 달은 제외 — 섞어 쓰면 단위가 어긋난다
            amt *= rate
        out[period] = out.get(period, 0.0) + amt
    return out


def load_fx(months: list[str], api_key: str) -> dict[str, float]:
    """수집 대상 월들의 USD 관세환율. 실패한 달은 빠진 채로 반환한다."""
    fx = {}
    for m in months:
        try:
            r = capi.fetch_fx_month(m, api_key, CACHE_PATH)
        except capi.CustomsError as e:
            # 환율은 보정용 부가 정보다 — 못 받으면 USD 기준으로 진행하고 출력에 기록한다.
            logger.warning("관세환율 사용 불가(%s) — USD 기준으로 계속합니다", str(e)[:90])
            return {}
        if r:
            fx[m] = r
    if not fx:
        logger.warning("관세환율을 하나도 받지 못함 — USD 기준으로 상관계수를 계산합니다")
    else:
        logger.info("관세환율 확보 %d개월 (예: %s=%.1f원)", len(fx),
                    max(fx), fx[max(fx)])
    return fx


def _sgg_names(rows: list[dict]) -> list[str]:
    """응답에 등장하는 시군구명 목록."""
    return sorted({str(r.get("sgg", "")).strip() for r in rows if r.get("sgg")})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        data = build()
    except capi.CustomsError as e:
        logger.error("매칭 구축 실패: %s", e)
        sys.exit(1)
    if data is None:
        logger.error("매칭 구축 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    cov = data["coverage"]
    logger.info("저장: %s (A %d · B %d · C %d / 검토 %d)",
                OUT_PATH, cov["graded_a"], cov["graded_b"], cov["graded_c"], cov["total_checked"])


if __name__ == "__main__":
    main()
