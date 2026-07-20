"""
가치투자 전용 스캐너 — value_universe.json(수동 입력) + DART 재무로 value.json 생성.

- 입력: 저장소 루트 value_universe.json (유니버스/포트폴리오, 수동 편집)
- 재무: DART OpenAPI (환경변수 DART_API_KEY) — 5년 매출/영업이익/순이익 + 퀄리티 지표
- 출력: docs/data/value.json (프론트 '가치투자' 탭이 읽음)

DART 키가 없으면 재무는 비우고 입력값만 내보낸다(로컬 문법검증 등).
"""
from __future__ import annotations
import html as _html
import io
import json
import logging
import os
import re
import sys
import urllib.request
import urllib.error
import zipfile
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("fetch_value")
KST = ZoneInfo("Asia/Seoul")

ROOT = Path(__file__).parent.parent
INPUT_PATH = ROOT / "value_universe.json"
OUT_PATH = ROOT / "docs" / "data" / "value.json"
DART_KEY = os.environ.get("DART_API_KEY", "").strip()
_DART = "https://opendart.fss.or.kr/api"

_corp_cache: dict[str, str] | None = None
_fin_cache: dict[str, dict] = {}
_company_cache: dict[str, dict] = {}
_CORP_CLS = {"Y": "코스피", "K": "코스닥", "N": "코넥스", "E": "기타"}


def _num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if not s or s in ("-",):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _corp_map() -> dict[str, str]:
    """종목코드(6자리) → DART corp_code(8자리)."""
    global _corp_cache
    if _corp_cache is not None:
        return _corp_cache
    _corp_cache = {}
    if not DART_KEY:
        return _corp_cache
    try:
        with urllib.request.urlopen(f"{_DART}/corpCode.xml?crtfc_key={DART_KEY}", timeout=40) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read()))
        root = ET.fromstring(z.read(z.namelist()[0]).decode("utf-8"))
        for e in root.iter("list"):
            sc = (e.findtext("stock_code") or "").strip()
            if sc:
                _corp_cache[sc] = (e.findtext("corp_code") or "").strip()
        logger.info("corp_code 매핑 %d건", len(_corp_cache))
    except Exception as e:
        logger.warning("corpCode 다운로드 실패: %s", e)
    return _corp_cache


def _acnt(corp: str, year: int) -> dict:
    """fnlttSinglAcnt(연간) — {account_nm: {y: amount}} 3개 연도. CFS 우선, 없으면 OFS."""
    url = f"{_DART}/fnlttSinglAcnt.json?crtfc_key={DART_KEY}&corp_code={corp}&bsns_year={year}&reprt_code=11011"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        logger.warning("재무 조회 실패 %s/%s: %s", corp, year, e)
        return {}
    if d.get("status") != "000" or not d.get("list"):
        return {}
    rows = [x for x in d["list"] if x.get("fs_div") == "CFS"] or d["list"]
    out = {}
    for x in rows:
        nm = x.get("account_nm", "")
        out.setdefault(nm, {})
        out[nm][year] = _num(x.get("thstrm_amount"))
        out[nm][year - 1] = _num(x.get("frmtrm_amount"))
        out[nm][year - 2] = _num(x.get("bfefrmtrm_amount"))
    return out


def _pick(acc: dict, names, year):
    """계정명 후보 중 해당 연도 값을 찾는다."""
    for nm in names:
        if nm in acc and acc[nm].get(year) is not None:
            return acc[nm][year]
    # 부분 일치 fallback
    for k, v in acc.items():
        if any(n in k for n in names) and v.get(year) is not None:
            return v[year]
    return None


def _financials(corp: str) -> dict:
    """최근 ~6년 핵심 재무 + 퀄리티 지표."""
    if corp in _fin_cache:
        return _fin_cache[corp]
    this_year = dt.datetime.now(tz=KST).year
    merged = {}
    # 사업보고서는 전년도까지 확정. 최신 연도부터 시도.
    for base in (this_year - 1, this_year - 4):
        acc = _acnt(corp, base)
        for nm, yv in acc.items():
            merged.setdefault(nm, {}).update(yv)

    def series(names):
        vals = {}
        for nm in names:
            if nm in merged:
                for y, v in merged[nm].items():
                    if v is not None and y not in vals:
                        vals[y] = v
        # 부분일치 보강
        if not vals:
            for k, m in merged.items():
                if any(n in k for n in names):
                    for y, v in m.items():
                        if v is not None:
                            vals.setdefault(y, v)
        return vals

    rev = series(["매출액", "수익(매출액)", "영업수익"])
    op = series(["영업이익", "영업이익(손실)"])
    ni = series(["당기순이익", "당기순이익(손실)"])
    assets = series(["자산총계"])
    liab = series(["부채총계"])
    equity = series(["자본총계"])
    ca = series(["유동자산"])
    cl = series(["유동부채"])

    years = sorted(set(rev) | set(op) | set(ni))[-6:]
    trend = [{"year": y, "revenue": rev.get(y), "op": op.get(y), "ni": ni.get(y)} for y in years]

    # 최신 연도 지표
    metrics = {}
    yrs_full = [y for y in sorted(equity) if equity.get(y)]
    if yrs_full:
        ly = yrs_full[-1]
        py = yrs_full[-2] if len(yrs_full) >= 2 else None

        def pct(a, b):
            return round(a / b * 100, 1) if (a is not None and b) else None

        metrics = {
            "year": ly,
            "roe": pct(ni.get(ly), equity.get(ly)),
            "op_margin": pct(op.get(ly), rev.get(ly)),
            "net_margin": pct(ni.get(ly), rev.get(ly)),
            "debt_ratio": pct(liab.get(ly), equity.get(ly)),
            "current_ratio": pct(ca.get(ly), cl.get(ly)),
            "rev_growth": pct(rev.get(ly) - rev.get(py), rev.get(py)) if (py and rev.get(ly) is not None and rev.get(py)) else None,
            "ni_growth": pct(ni.get(ly) - ni.get(py), ni.get(py)) if (py and ni.get(ly) is not None and ni.get(py)) else None,
        }
    result = {"trend": trend, "metrics": metrics}
    _fin_cache[corp] = result
    return result


def _quality_score(m: dict) -> int | None:
    """간이 퀄리티 스코어 0~100 (ROE·마진·건전성·성장)."""
    if not m:
        return None
    s, w = 0.0, 0.0
    def add(v, good, weight, higher=True):
        nonlocal s, w
        if v is None:
            return
        w += weight
        ratio = (v / good) if higher else (good / v if v else 0)
        s += weight * max(0.0, min(1.0, ratio))
    add(m.get("roe"), 15, 25)          # ROE 15%면 만점
    add(m.get("op_margin"), 15, 20)    # 영업이익률 15%
    add(m.get("net_margin"), 10, 10)
    add(m.get("rev_growth"), 15, 15)
    add(m.get("ni_growth"), 15, 15)
    if m.get("debt_ratio") is not None:  # 부채비율 낮을수록 좋음(100% 기준)
        w += 15
        s += 15 * max(0.0, min(1.0, 100 / max(m["debt_ratio"], 1)))
    return round(s / w * 100) if w else None


_FS_LABELS = [
    ("ROA>0", "당기 ROA 흑자"), ("CFO>0", "영업현금흐름 흑자"), ("dROA>0", "ROA 개선"),
    ("CFO>NI", "이익의 질(현금 > 순이익)"), ("dLev<0", "부채(비유동) 감소"), ("dCurr>0", "유동비율 개선"),
    ("noNewShares", "신주 미발행"), ("dMargin>0", "매출총이익률 개선"), ("dTurn>0", "자산회전율 개선"),
]


def _fscore(corp: str, year: int | None = None) -> dict | None:
    """Piotroski F-Score(9점) — DART 전체재무제표(연결)로 t vs t-1 비교.
    year 미지정 시 최근 확정 사업연도(작년). 백테스트는 포인트인타임 연도를 넘긴다."""
    if year is None:
        year = dt.datetime.now(tz=KST).year - 1
    rows = None
    for fs in ("CFS", "OFS"):
        url = (f"{_DART}/fnlttSinglAcntAll.json?crtfc_key={DART_KEY}&corp_code={corp}"
               f"&bsns_year={year}&reprt_code=11011&fs_div={fs}")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            logger.warning("F-Score 재무 실패 %s: %s", corp, e)
            return None
        if d.get("status") == "000" and d.get("list"):
            rows = d["list"]
            break
    if not rows:
        return None

    def get(nm, sj, per):
        # DART account_nm은 회사마다 공백 유무가 달라("영업활동현금흐름" vs "영업활동 현금흐름")
        # 공백을 제거해 비교한다. 그러지 않으면 CFO 등 다어절 계정이 대부분 매칭 실패한다.
        nmz = nm.replace(" ", "")
        for x in rows:
            if x.get("sj_div") != sj:
                continue
            a = (x.get("account_nm", "") or "").replace(" ", "")
            if a == nmz or a == nmz + "(손실)" or a.startswith(nmz):
                return _num(x.get(per + "_amount"))
        return None

    def pair(nm, sj):
        return get(nm, sj, "thstrm"), get(nm, sj, "frmtrm")

    A_t, A_p = pair("자산총계", "BS")
    CA_t, CA_p = pair("유동자산", "BS")
    CL_t, CL_p = pair("유동부채", "BS")
    NCL_t, NCL_p = pair("비유동부채", "BS")
    CAP_t, CAP_p = pair("자본금", "BS")
    REV_t, REV_p = pair("매출액", "CIS")
    GP_t, GP_p = pair("매출총이익", "CIS")
    NI_t, NI_p = pair("당기순이익", "CIS")
    CFO_t, CFO_p = pair("영업활동 현금흐름", "CF")
    if CFO_t is None:  # 다른 표기 변형("영업활동으로 인한 현금흐름") 폴백
        CFO_t, CFO_p = pair("영업활동으로 인한 현금흐름", "CF")
    if REV_t is None:
        REV_t, REV_p = pair("매출액", "IS")
    if GP_t is None:
        GP_t, GP_p = pair("매출총이익", "IS")
    if NI_t is None:
        NI_t, NI_p = pair("당기순이익", "IS")

    def ratio(n, d):
        return (n / d) if (n is not None and d) else None

    roa_t, roa_p = ratio(NI_t, A_t), ratio(NI_p, A_p)
    lev_t, lev_p = ratio(NCL_t, A_t), ratio(NCL_p, A_p)
    cr_t, cr_p = ratio(CA_t, CL_t), ratio(CA_p, CL_p)
    gm_t, gm_p = ratio(GP_t, REV_t), ratio(GP_p, REV_p)
    at_t, at_p = ratio(REV_t, A_t), ratio(REV_p, A_p)

    def gt(a, b):
        return a is not None and b is not None and a > b

    def lt(a, b):
        return a is not None and b is not None and a < b

    c = {
        "ROA>0": int((roa_t or 0) > 0),
        "CFO>0": int((CFO_t or 0) > 0),
        "dROA>0": int(gt(roa_t, roa_p)),
        "CFO>NI": int(gt(CFO_t, NI_t)),
        "dLev<0": int(lt(lev_t, lev_p)),
        "dCurr>0": int(gt(cr_t, cr_p)),
        "noNewShares": int(CAP_t is not None and CAP_p is not None and CAP_t <= CAP_p),
        "dMargin>0": int(gt(gm_t, gm_p)),
        "dTurn>0": int(gt(at_t, at_p)),
    }
    return {"score": sum(c.values()), "components": c, "year": year}


def _quality_metrics(corp: str, year: int | None = None) -> dict | None:
    """퀄리티 성장주 발굴용 원시 재무지표 (DART 전체재무제표, 연결 우선).
    year 미지정 시 최근 확정 사업연도. 반환: gpa·opm·debt·accruals·rev_g·op_g (비율=소수) 또는 None.
      gpa=매출총이익/자산총계, opm=영업이익/매출, debt=부채총계/자본총계,
      accruals=(순이익−영업활동현금흐름)/자산총계, rev_g·op_g=매출·영업이익 2년 CAGR(전전기 대비).
    quality_backtest._quality_fin과 동일 정의 — 라이브·백테스트 정합."""
    if year is None:
        year = dt.datetime.now(tz=KST).year - 1
    rows = None
    for fs in ("CFS", "OFS"):
        url = (f"{_DART}/fnlttSinglAcntAll.json?crtfc_key={DART_KEY}&corp_code={corp}"
               f"&bsns_year={year}&reprt_code=11011&fs_div={fs}")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            logger.warning("퀄리티 재무 실패 %s: %s", corp, e)
            return None
        if d.get("status") == "000" and d.get("list"):
            rows = d["list"]
            break
    if not rows:
        return None

    def get(nm, sj, per="thstrm"):
        nmz = nm.replace(" ", "")
        for x in rows:
            if x.get("sj_div") != sj:
                continue
            a = (x.get("account_nm", "") or "").replace(" ", "")
            if a == nmz or a == nmz + "(손실)" or a.startswith(nmz):
                return _num(x.get(per + "_amount"))
        return None

    def acct(nm, sjs, per="thstrm"):
        for sj in sjs:
            v = get(nm, sj, per)
            if v is not None:
                return v
        return None

    assets = get("자산총계", "BS")
    liab = get("부채총계", "BS")
    equity = get("자본총계", "BS")
    def rev_of(per="thstrm"):   # 서비스·플랫폼기업은 '영업수익' 표기 → 폴백
        return (acct("매출액", ("CIS", "IS"), per) or acct("수익(매출액)", ("CIS", "IS"), per)
                or acct("영업수익", ("CIS", "IS"), per))
    rev = rev_of()
    gp = acct("매출총이익", ("CIS", "IS"))
    op = acct("영업이익", ("CIS", "IS"))
    ni = acct("당기순이익", ("CIS", "IS"))
    cfo = get("영업활동 현금흐름", "CF") or get("영업활동으로 인한 현금흐름", "CF")
    rev_p2 = rev_of("bfefrmtrm")
    op_p2 = acct("영업이익", ("CIS", "IS"), "bfefrmtrm")

    def ratio(n, d):
        return (n / d) if (n is not None and d not in (None, 0)) else None

    def cagr2(now, past):
        if now is None or past is None or past <= 0 or now <= 0:
            return None
        return (now / past) ** (1 / 2) - 1

    gpa = ratio(gp, assets)
    opm = ratio(op, rev)
    if gpa is None and opm is None:   # 금융·지주 등 매출/매출총이익 미보고 → 무효
        return None
    return {
        "gpa": gpa, "opm": opm, "debt": ratio(liab, equity),
        "accruals": ratio((ni - cfo) if (ni is not None and cfo is not None) else None, assets),
        "rev_g": cagr2(rev, rev_p2), "op_g": cagr2(op, op_p2), "year": year,
    }


def _company(corp: str) -> dict:
    """DART 기업개요 — 대표·설립·시장·홈페이지·주소."""
    if corp in _company_cache:
        return _company_cache[corp]
    info = {}
    try:
        with urllib.request.urlopen(f"{_DART}/company.json?crtfc_key={DART_KEY}&corp_code={corp}", timeout=20) as r:
            d = json.loads(r.read().decode())
        if d.get("status") == "000":
            est = (d.get("est_dt") or "").strip()
            info = {
                "ceo": (d.get("ceo_nm") or "").strip() or None,
                "est": f"{est[:4]}-{est[4:6]}-{est[6:]}" if len(est) == 8 else None,
                "market": _CORP_CLS.get(d.get("corp_cls"), None),
                "homepage": (d.get("hm_url") or "").strip() or None,
                "address": (d.get("adres") or "").strip() or None,
            }
    except Exception as e:
        logger.warning("기업개요 실패 %s: %s", corp, e)
    _company_cache[corp] = info
    return info


def _fnguide_business(code: str) -> dict:
    """FnGuide Snapshot의 'Business Summary'(사업 설명 + 최근 실적) 추출."""
    url = f"https://wcomp.fnguide.com/CompanyInfo/Snapshot?c_id=AA&menu_type=01&cmp_cd={code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception as e:
        logger.warning("FnGuide 조회 실패 %s: %s", code, e)
        return {}
    m = re.search(r'id="bizSummaryDate">\[([^\]]*)\](.*?)(?:업종\s*비교|단위\s*:)', page, re.S)
    if not m:
        return {}
    body = re.sub(r"<!--.*?-->", " ", m.group(2), flags=re.S)  # HTML 주석 제거
    txt = re.sub(r"<[^>]+>", " ", body)
    txt = _html.unescape(txt)
    txt = txt.replace("<", " ").replace(">", " ")  # 엔티티 복원으로 생긴 잔여 부등호 제거
    txt = re.sub(r"\s+", " ", txt).strip()
    txt = re.sub(r"\s*[!<>\-]{2,}\s*$", "", txt).strip()  # 꼬리 주석 잔재 제거
    return {"date": m.group(1).strip(), "text": txt} if txt else {}


def _enrich(rec: dict) -> dict:
    code = rec.get("code", "")
    corp = _corp_map().get(code)
    fin = _financials(corp) if corp else {"trend": [], "metrics": {}}
    out = dict(rec)
    out["financials"] = fin.get("trend", [])
    out["metrics"] = fin.get("metrics", {})
    out["quality_score"] = _quality_score(fin.get("metrics", {}))
    out["f_score"] = _fscore(corp) if corp else None  # Piotroski F-Score(9점)
    out["company"] = _company(corp) if corp else {}
    out["biz_summary"] = _fnguide_business(code)  # FnGuide Business Summary
    return out


def build() -> dict | None:
    data = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    universe = [_enrich(r) for r in data.get("universe", [])]
    portfolio = [_enrich(r) for r in data.get("portfolio", [])]
    # DART 장애 감지: 키가 있고 종목도 있는데 재무가 절반도 안 붙으면 일시 장애로 보고
    # 공백본으로 기존 value.json을 덮어쓰지 않는다(value_screen과 동일한 보존 정책).
    entries = universe + portfolio
    if DART_KEY and entries:
        enriched = sum(1 for e in entries if e.get("f_score") or e.get("financials"))
        if enriched < max(1, len(entries) // 2):
            logger.error("DART 재무 확보 %d/%d — 일시 장애 의심, 기존 value.json 보존", enriched, len(entries))
            return None
    return {
        "updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
        "dart": bool(DART_KEY),
        "universe": universe,
        "portfolio": portfolio,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not DART_KEY:
        logger.warning("DART_API_KEY 없음 — 재무 지표 없이 입력값만 출력")
    d = build()
    if d is None:
        # 기존 파일 보존 + 워크플로에 실패 노출(조용한 stale 방지). 후속 KV push 스텝도 스킵됨.
        logger.error("재무 확보 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s (유니버스 %d, 포트폴리오 %d)", OUT_PATH, len(d["universe"]), len(d["portfolio"]))


if __name__ == "__main__":
    main()
