"""
가치투자 전용 스캐너 — value_universe.json(수동 입력) + DART 재무로 value.json 생성.

- 입력: 저장소 루트 value_universe.json (유니버스/포트폴리오, 수동 편집)
- 재무: DART OpenAPI (환경변수 DART_API_KEY) — 5년 매출/영업이익/순이익 + 퀄리티 지표
- 출력: docs/data/value.json (프론트 '가치투자' 탭이 읽음)

DART 키가 없으면 재무는 비우고 입력값만 내보낸다(로컬 문법검증 등).
"""
from __future__ import annotations
import io
import json
import logging
import os
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


def _enrich(rec: dict) -> dict:
    code = rec.get("code", "")
    corp = _corp_map().get(code)
    fin = _financials(corp) if corp else {"trend": [], "metrics": {}}
    out = dict(rec)
    out["financials"] = fin.get("trend", [])
    out["metrics"] = fin.get("metrics", {})
    out["quality_score"] = _quality_score(fin.get("metrics", {}))
    # 목표가 대비 상승여력(현재가는 추후 시세 연동 시 계산)
    return out


def build() -> dict:
    data = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    universe = [_enrich(r) for r in data.get("universe", [])]
    portfolio = [_enrich(r) for r in data.get("portfolio", [])]
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
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s (유니버스 %d, 포트폴리오 %d)", OUT_PATH, len(d["universe"]), len(d["portfolio"]))


if __name__ == "__main__":
    main()
