"""
실적 캘린더 수집기 — DART 공시 + 네이버 컨센서스로 earnings_calendar.json 생성.

- 종목: 포트폴리오 보유 + 유니버스(value_universe.json/value.json) + 관심종목(config.json watchlist)
- DART: list.json(최근 ~120일)에서 "영업(잠정)실적" 공시 + 분기/반기/사업보고서 → 발표일·rcept_no
  실제치는 정기보고서 제출 후 fnlttSinglAcnt.json (원 → 억원 환산)
- 네이버: m.stock.naver.com/api/stock/{code}/finance/quarter
  (financeInfo.trTitleList의 isConsensus="Y" 분기가 컨센서스, "N"은 실적 — 단위 이미 억원)
- 출력: docs/data/earnings_calendar.json (프론트 '실적 캘린더' 하위 탭이 읽음)

단독 실행: python earnings_calendar.py [--codes 005930,000660] [--limit 5]
DART 키가 없으면 네이버 컨센서스 + 예상일 휴리스틱만으로 생성한다.
"""
from __future__ import annotations
import argparse
import io
import json
import logging
import os
import time
import urllib.request
import zipfile
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("earnings_calendar")
KST = ZoneInfo("Asia/Seoul")

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.json"
PORTFOLIO_PATH = ROOT / "docs" / "data" / "portfolio.json"
VALUE_PATH = ROOT / "docs" / "data" / "value.json"
VALUE_UNIVERSE_PATH = ROOT / "value_universe.json"  # fetch_value.py 입력 — 수동 편집 유니버스
OUT_PATH = ROOT / "docs" / "data" / "earnings_calendar.json"

DART_KEY = os.environ.get("DART_API_KEY", "").strip()
_DART = "https://opendart.fss.or.kr/api"
_DART_SLEEP = 0.3
_NAVER_SLEEP = 0.2
_NAVER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
}
# 분기별 정기보고서 코드 (fnlttSinglAcnt reprt_code)
_REPRT = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}

_corp_cache: dict[str, str] | None = None


# ── 유틸 ──────────────────────────────────────────────────────────

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


def _eok(won):
    """원 → 억원 (반올림)."""
    return round(won / 1e8) if won is not None else None


def _pct(a, b):
    """(a-b)/|b|*100 — 서프라이즈/YoY 공용, 어느 한쪽 없거나 b=0이면 None."""
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / abs(b) * 100, 1)


def _last_quarter(today: dt.date) -> tuple[int, int]:
    """가장 최근에 '끝난' 분기 (발표 대상 분기)."""
    q = (today.month - 1) // 3 + 1
    y = today.year
    if q == 1:
        return y - 1, 4
    return y, q - 1


def _quarter_end(year: int, q: int) -> dt.date:
    m = q * 3
    nxt = dt.date(year + (m == 12), (m % 12) + 1, 1)
    return nxt - dt.timedelta(days=1)


def _months_window(today: dt.date) -> list[str]:
    """전월·당월·익월 (스키마 months 필드)."""
    out = []
    for off in (-1, 0, 1):
        m = today.month + off
        y = today.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        out.append(f"{y:04d}-{m:02d}")
    return out


# ── 종목 수집 (포트폴리오 + 유니버스 + 관심종목) ──────────────────

def _collect_codes() -> dict[str, str]:
    """{code: name} — portfolio.json 보유/유니버스 + value.json + config watchlist."""
    codes: dict[str, str] = {}

    def add(rec):
        c = str(rec.get("code", "")).strip()
        if len(c) == 6 and c.isdigit():
            codes.setdefault(c, (rec.get("name") or "").strip())

    try:
        p = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
        for r in p.get("holdings", []) or []:
            add(r)
        for r in p.get("universe", []) or []:
            add(r)
    except Exception as e:
        logger.warning("portfolio.json 읽기 실패(건너뜀): %s", e)
    try:
        v = json.loads(VALUE_PATH.read_text(encoding="utf-8"))
        for r in (v.get("universe", []) or []) + (v.get("portfolio", []) or []):
            add(r)
    except Exception as e:
        logger.warning("value.json 읽기 실패(건너뜀): %s", e)
    try:
        vu = json.loads(VALUE_UNIVERSE_PATH.read_text(encoding="utf-8"))
        for r in (vu.get("universe", []) or []) + (vu.get("portfolio", []) or []):
            add(r)
    except Exception as e:
        logger.warning("value_universe.json 읽기 실패(건너뜀): %s", e)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for c in cfg.get("watchlist", []) or []:
            if isinstance(c, str):
                add({"code": c})
            elif isinstance(c, dict):
                add(c)
    except Exception as e:
        logger.warning("config.json 읽기 실패(건너뜀): %s", e)
    return codes


# ── DART ──────────────────────────────────────────────────────────

def _dart_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read().decode())


def _corp_map() -> dict[str, str]:
    """종목코드(6자리) → DART corp_code(8자리). fetch_value.py 방식."""
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


def _is_earnings_report(nm: str) -> bool:
    """실적 관련 공시 제목 필터 — 잠정실적 공시 + 정기보고서."""
    nm = nm or ""
    if "영업(잠정)실적" in nm or "연결재무제표기준영업(잠정)실적" in nm:
        return True
    return any(k in nm for k in ("분기보고서", "반기보고서", "사업보고서"))


def _dart_disclosures(corp: str, bgn: dt.date, end: dt.date) -> list[dict]:
    """list.json — 기간 내 실적 관련 공시 [{rcept_no, rcept_dt, report_nm}]."""
    if not DART_KEY or not corp:
        return []
    url = (f"{_DART}/list.json?crtfc_key={DART_KEY}&corp_code={corp}"
           f"&bgn_de={bgn:%Y%m%d}&end_de={end:%Y%m%d}&page_count=100")
    try:
        d = _dart_json(url)
    except Exception as e:
        logger.warning("DART list 실패 %s: %s", corp, e)
        return []
    finally:
        time.sleep(_DART_SLEEP)
    if d.get("status") != "000":
        return []
    out = []
    for x in d.get("list", []) or []:
        if _is_earnings_report(x.get("report_nm", "")):
            out.append({"rcept_no": x.get("rcept_no"), "rcept_dt": x.get("rcept_dt"),
                        "report_nm": x.get("report_nm", "")})
    return out


def _dart_actual(corp: str, year: int, q: int) -> dict | None:
    """fnlttSinglAcnt — 해당 분기 실제치(억원). 반기/연간 보고서는 누적이라 직전 분기 차감."""
    if not DART_KEY or not corp:
        return None

    def fetch(reprt):
        url = (f"{_DART}/fnlttSinglAcnt.json?crtfc_key={DART_KEY}&corp_code={corp}"
               f"&bsns_year={year}&reprt_code={reprt}")
        try:
            d = _dart_json(url)
        except Exception as e:
            logger.warning("fnlttSinglAcnt 실패 %s/%s: %s", corp, reprt, e)
            return {}
        finally:
            time.sleep(_DART_SLEEP)
        if d.get("status") != "000" or not d.get("list"):
            return {}
        rows = [x for x in d["list"] if x.get("fs_div") == "CFS"] or d["list"]
        out = {}
        for x in rows:
            nm = x.get("account_nm", "")
            for key, names in (("revenue", ("매출액", "수익(매출액)", "영업수익")),
                               ("op", ("영업이익",)), ("np", ("당기순이익",))):
                if key not in out and any(nm == n or nm.startswith(n) for n in names):
                    out[key] = _num(x.get("thstrm_amount"))
        return out

    cur = fetch(_REPRT[q])
    if not cur:
        return None
    # 반기(Q2)/연간(Q4) 보고서의 손익 항목은 누적 — 직전 분기 누적을 차감해 분기 단독치로 환산
    if q in (2, 4):
        prev = fetch(_REPRT[q - 1]) if q == 2 else fetch("11014")  # Q4는 3분기 누적 차감
        # 11014(3분기)는 누적치가 아닐 수 있어 차감 실패 시 누적치 그대로 두지 않고 None 처리
        if prev:
            cur = {k: (cur.get(k) - prev.get(k)) if (cur.get(k) is not None and prev.get(k) is not None) else None
                   for k in ("revenue", "op", "np")}
        else:
            return None
    vals = {k: _eok(cur.get(k)) for k in ("revenue", "op", "np")}
    return vals if any(v is not None for v in vals.values()) else None


# ── 네이버 컨센서스 ───────────────────────────────────────────────

def _naver_quarter(code: str) -> dict:
    """네이버 모바일 분기 재무 — {"YYYYQn": {"revenue","op","np","consensus":bool}} (억원).

    검증된 스키마(005930 라이브 확인):
      financeInfo.trTitleList = [{key:"202606", isConsensus:"Y"|"N"}, ...]
      financeInfo.rowList = [{title:"매출액"|"영업이익"|"당기순이익", columns:{key:{value:"1,234"}}}]
    """
    url = f"https://m.stock.naver.com/api/stock/{code}/finance/quarter"
    headers = dict(_NAVER_HEADERS)
    headers["Referer"] = f"https://m.stock.naver.com/domestic/stock/{code}/finance"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            d = json.loads(r.read().decode())
        fi = d["financeInfo"]
        cons = {t["key"]: t.get("isConsensus") == "Y" for t in fi["trTitleList"]}
        rows = {r["title"]: r.get("columns", {}) for r in fi["rowList"]}
        out = {}
        for key, is_cons in cons.items():
            y, m = int(key[:4]), int(key[4:6])
            period = f"{y}Q{(m - 1) // 3 + 1}"
            vals = {}
            for field, title in (("revenue", "매출액"), ("op", "영업이익"), ("np", "당기순이익")):
                col = rows.get(title, {}).get(key) or {}
                vals[field] = _num(col.get("value"))
            if any(v is not None for v in vals.values()):
                vals["consensus"] = is_cons
                out[period] = vals
        return out
    except Exception as e:
        # 비공식 API — 스키마 변경/차단 시 종목 단위로 조용히 강등
        logger.warning("네이버 분기 조회 실패 %s: %s", code, e)
        return {}
    finally:
        time.sleep(_NAVER_SLEEP)


# ── 이벤트 생성 ───────────────────────────────────────────────────

def _build_event(code: str, name: str, today: dt.date) -> dict:
    year, q = _last_quarter(today)
    period = f"{year}Q{q}"
    qend = _quarter_end(year, q)

    corp = _corp_map().get(code)
    # 최근 ~120일 공시 — 발표일·rcept_no 확정 여부
    recent = _dart_disclosures(corp, today - dt.timedelta(days=120), today)
    # 분기말 이후 접수된 공시만 해당 분기 발표로 인정
    ann = None
    for x in sorted(recent, key=lambda x: x.get("rcept_dt") or ""):
        rd = x.get("rcept_dt") or ""
        if rd >= f"{qend:%Y%m%d}":
            ann = x
            break

    naver = _naver_quarter(code)
    nq = naver.get(period) or {}
    prev = naver.get(f"{year - 1}Q{q}") or {}

    consensus = None
    if nq and nq.get("consensus"):
        consensus = {k: nq.get(k) for k in ("revenue", "op", "np")}

    actual = None
    src = "추정"
    rcept_no = None
    if ann:
        rcept_no = ann["rcept_no"]
        periodic = any(k in ann["report_nm"] for k in ("분기보고서", "반기보고서", "사업보고서"))
        src = "확정" if periodic else "잠정"
        if periodic:
            actual = _dart_actual(corp, year, q)
        if actual is None and nq and not nq.get("consensus"):
            # 네이버가 이미 실적으로 표시한 분기 — 잠정공시~정기보고서 사이 보강
            actual = {k: nq.get(k) for k in ("revenue", "op", "np")}
    elif nq and not nq.get("consensus"):
        # DART 공시를 못 찾았지만 네이버에 실적 확정 반영된 경우
        actual = {k: nq.get(k) for k in ("revenue", "op", "np")}
        src = "잠정"

    # 발표일 — 확정(공시) 또는 예상(작년 발표일+365d, 없으면 분기말+45d)
    if ann:
        rd = ann["rcept_dt"]
        date = f"{rd[:4]}-{rd[4:6]}-{rd[6:]}"
        date_kind, status = "확정", "발표완료"
    else:
        last_year = _dart_disclosures(
            corp, _quarter_end(year - 1, q), _quarter_end(year - 1, q) + dt.timedelta(days=100))
        base = None
        for x in sorted(last_year, key=lambda x: x.get("rcept_dt") or ""):
            base = x.get("rcept_dt")
            break
        if base:
            est = dt.date(int(base[:4]), int(base[4:6]), int(base[6:])) + dt.timedelta(days=365)
        else:
            est = qend + dt.timedelta(days=45)
        date = est.isoformat()
        date_kind = "예상"
        status = "발표완료" if actual else "발표예정"

    ref = actual or consensus
    surprise = None
    if actual and consensus:
        surprise = {k: _pct(actual.get(k), consensus.get(k)) for k in ("revenue", "op", "np")}
    yoy = None
    if ref and prev:
        yoy = {k: _pct(ref.get(k), prev.get(k)) for k in ("revenue", "op", "np")}

    return {
        "code": code, "name": name, "period": period,
        "date": date, "date_kind": date_kind, "status": status,
        "src": src, "rcept_no": rcept_no,
        "consensus": consensus, "actual": actual,
        "surprise": surprise, "yoy": yoy,
    }


def build(codes: dict[str, str]) -> dict:
    today = dt.datetime.now(tz=KST).date()
    events = []
    for i, (code, name) in enumerate(sorted(codes.items()), 1):
        try:
            ev = _build_event(code, name, today)
            events.append(ev)
            logger.info("[%d/%d] %s %s — %s %s", i, len(codes), code, name or "", ev["date"], ev["status"])
        except Exception as e:
            logger.warning("이벤트 생성 실패 %s: %s", code, e)
    return {
        "updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
        "dart": bool(DART_KEY),
        "months": _months_window(today),
        "events": events,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="실적 캘린더 수집기")
    ap.add_argument("--codes", help="쉼표 구분 종목코드 (미지정 시 포트폴리오+유니버스+관심종목)")
    ap.add_argument("--limit", type=int, help="테스트용 — 앞 N종목만")
    args = ap.parse_args()

    if args.codes:
        all_names = _collect_codes()
        codes = {c.strip(): all_names.get(c.strip(), "") for c in args.codes.split(",") if c.strip()}
    else:
        codes = _collect_codes()
    if args.limit:
        codes = dict(sorted(codes.items())[: args.limit])
    if not DART_KEY:
        logger.warning("DART_API_KEY 없음 — 네이버 컨센서스 + 예상일 휴리스틱만 사용")

    d = build(codes)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s (이벤트 %d건)", OUT_PATH, len(d["events"]))


if __name__ == "__main__":
    main()
