"""
실적 캘린더 수집기 — DART 공시 + 네이버 컨센서스로 earnings_calendar.json 생성.

- 종목: 포트폴리오 보유 + 유니버스(value_universe.json/value.json) + 관심종목(config.json watchlist)
- DART: list.json(최근 ~120일)에서 "영업(잠정)실적" 공시 + 분기/반기/사업보고서 → 발표일·rcept_no
  실제치는 정기보고서 제출 후 fnlttSinglAcnt.json (원 → 억원 환산)
- 네이버: m.stock.naver.com/api/stock/{code}/finance/quarter
  (financeInfo.trTitleList의 isConsensus="Y" 분기가 컨센서스, "N"은 실적 — 단위 이미 억원)
- 출력: docs/data/earnings_calendar.json (프론트 '실적 캘린더' 하위 탭이 읽음)

단독 실행: python earnings_calendar.py [--codes 005930,000660] [--limit 5] [--no-investing]
DART 키가 없으면 네이버 컨센서스 + 예상일 휴리스틱만으로 생성한다.

보조 소스 — 인베스팅닷컴 한국 실적 캘린더 (예정일 보강 + 시장 전체 이벤트):
  발표일 우선순위: DART 공시 확정일 > 인베스팅 예정일 > 작년잠정+365d(잠정 관행 종목)
    > IR컨콜일(잠정 관행 없는 종목의 첫 공개일) > 작년정기+365d > 분기말+45d
  모든 이벤트에 date_src("공시"|"IR공시"|"인베스팅"|"웹"|"추정") 필드로 출처 기록.
  예상일이 과거로 지나가면(stale) 미래 컨콜일로 승격, 없으면 분기말+45d 재추정(date_kind="지연").
  Cloudflare 안티봇에 차단될 수 있음 — 실패 시 경고만 남기고 기존 파이프라인 그대로 진행(fail-soft).
"""
from __future__ import annotations
import argparse
import io
import json
import logging
import os
import re
import time
import urllib.parse
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
CONCALL_OVERLAY_PATH = ROOT / "docs" / "data" / "concall_overlay.json"  # 웹 리서치 오버레이(별도 단계가 생성)

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
_corp_names: dict[str, str] = {}


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
    """종목코드(6자리) → DART corp_code(8자리). fetch_value.py 방식.
    부수적으로 _corp_names(종목코드 → 회사명)도 채운다 — 소스에 이름 없는 종목 보강용."""
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
                _corp_names[sc] = (e.findtext("corp_name") or "").strip()
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


def _is_ir_notice(nm: str) -> bool:
    """기업설명회(IR) 개최 공시 제목 필터 — 컨퍼런스콜 일정 소스."""
    nm = nm or ""
    return "기업설명회" in nm or ("IR" in nm and "개최" in nm)


def _dart_disclosures(corp: str, bgn: dt.date, end: dt.date) -> list[dict]:
    """list.json — 기간 내 실적 관련 공시 + IR개최 공시.
    [{rcept_no, rcept_dt, report_nm, ir}] — ir=True는 기업설명회(IR)개최 공시."""
    if not DART_KEY or not corp:
        return []
    out = []
    page = 1
    while page <= 10:  # 안전 상한 — 삼성전자류 공시 다량 종목도 120일 창이면 충분
        url = (f"{_DART}/list.json?crtfc_key={DART_KEY}&corp_code={corp}"
               f"&bgn_de={bgn:%Y%m%d}&end_de={end:%Y%m%d}&page_no={page}&page_count=100")
        try:
            d = _dart_json(url)
        except Exception as e:
            logger.warning("DART list 실패 %s p%d: %s", corp, page, e)
            break
        finally:
            time.sleep(_DART_SLEEP)
        if d.get("status") != "000":
            break
        for x in d.get("list", []) or []:
            nm = x.get("report_nm", "")
            is_earn = _is_earnings_report(nm)
            is_ir = _is_ir_notice(nm)
            if is_earn or is_ir:
                out.append({"rcept_no": x.get("rcept_no"), "rcept_dt": x.get("rcept_dt"),
                            "report_nm": nm, "ir": (is_ir and not is_earn)})
        try:
            total = int(d.get("total_page") or 1)
        except (TypeError, ValueError):
            total = 1
        if page >= total:
            break
        page += 1
    return out


# 공시 원문에서 "일시 2026-07-23" / "2026년 7월 23일" 류 날짜 추출
_IR_DATE_RE = re.compile(
    r'일시.{0,200}?(\d{4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})', re.S)


def _dart_document_text(rcept_no: str) -> str | None:
    """document.xml(원문 zip) → 태그 제거한 평문. 실패 시 None (fail-soft).

    검증된 구조(105560/000660 라이브 확인): zip 안의 단일 XML, euc-kr 인코딩."""
    if not DART_KEY or not rcept_no:
        return None
    url = f"{_DART}/document.xml?crtfc_key={DART_KEY}&rcept_no={rcept_no}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            raw = r.read()
        z = zipfile.ZipFile(io.BytesIO(raw))
        data = z.read(z.namelist()[0])
        txt = None
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                txt = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if txt is None:
            txt = data.decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", " ", txt)  # 태그 제거 후 텍스트로
    except Exception as e:
        logger.warning("DART 원문 조회 실패 %s: %s", rcept_no, e)
        return None
    finally:
        time.sleep(_DART_SLEEP)


# 잠정실적 표 단위 표기 → 원 환산 배수 (문서 내 "단위 : 백만원" 류에서 탐지)
_PROV_UNIT_MULT = (("백만원", 1e6), ("천원", 1e3), ("억원", 1e8), ("조원", 1e12), ("원", 1.0))


def _provisional_actual(rcept_no: str):
    """영업(잠정)실적 공시 원문에서 당해실적 파싱 → {"revenue","op","np"} (억원).

    검증된 구조(라이브): 표가 "매출액 … 당해실적 <값>" 토큰 순서.
      KB금융 20260723800451 (백만원): 매출 31,757,381 → 317,574억
      셀트리온 20260703800022 (억원): 매출 13,000 → 13,000억 (당기순이익 '-' → None)
    표 전체가 '-'(빈 표)면 "EMPTY" 반환 — 실적 '예고'공시로, 발표가 아님
      (기아 20260701800569 라이브 확인 — 월간 판매실적 예고류).
    표 자체를 못 찾으면 None (fail-soft).
    """
    txt = _dart_document_text(rcept_no)
    if txt is None:
        return None
    # 단위 탐지 — "단위" 근처의 화폐 단위 표기 중 첫 매칭 (대수/%만 있는 표는 건너뜀)
    mult = None
    for m in re.finditer(r"단위.{0,40}", txt):
        seg = m.group(0)
        for u, f in _PROV_UNIT_MULT:
            if u in seg:
                mult = f
                break
        if mult is not None:
            break
    if mult is None:
        mult = 1e6  # 잠정실적 공시 관행상 백만원이 최다 — 보수적 기본값
    toks = txt.split()
    vals: dict[str, float | None] = {}
    found_row = False
    self_ok = False
    for key, kw in (("revenue", "매출액"), ("op", "영업이익"), ("np", "당기순이익")):
        vals[key] = None
        for i, t in enumerate(toks):
            if not (t == kw or t.startswith(kw)):
                continue
            # 행 키워드 뒤 몇 토큰 내 "당해실적" → 그 다음 토큰이 당해 분기 값
            hit = False
            for j in range(i + 1, min(i + 6, len(toks))):
                if toks[j].startswith("당해실적"):
                    hit = found_row = True
                    if j + 1 < len(toks):
                        v = _num(toks[j + 1])
                        if v is not None:
                            vals[key] = round(v * mult / 1e8)  # 억원 환산
                            # 자기일관성 검증: 같은 행의 전년동기값·증감율(%)이
                            # 당해값과 산술적으로 맞으면(±2%p) 당해 컬럼을 제대로
                            # 잡은 것 — 급성장 분기가 외부(네이버) 스케일 게이트에
                            # 오폐기되는 것을 막는 근거로 쓴다
                            win = toks[j + 2:j + 10]
                            nums = [_num(w) for w in win]
                            for a in range(len(nums)):
                                py = nums[a]
                                if py is None or py <= 0:
                                    continue
                                for b in range(a + 1, min(a + 3, len(nums))):
                                    pct = nums[b]
                                    if pct is None or abs(pct) >= 10000:
                                        continue
                                    if abs(v / py - (1 + pct / 100)) <= 0.02:
                                        self_ok = True
                    break
            if hit:  # 이 키워드의 실적표 행을 찾았으면 다른 위치는 더 안 봄
                break
    if not found_row:
        return None
    if all(v is None for v in vals.values()):
        return "EMPTY"  # 예고공시 — 표는 있으나 값 전부 미기재
    vals["self_ok"] = self_ok
    return vals


def _ir_concall_date(rcept_no: str, target_q: int | None = None) -> dt.date | None:
    """document.xml API(원문 zip)에서 IR 개최일 추출 — 실패 시 None (fail-soft).

    검증된 구조(105560/000660 라이브 확인): zip 안의 단일 XML, euc-kr 인코딩,
    "1. 일시 및 방법" 표에 "일시  2026-07-23  16:00" 형태.

    게이트(오매칭 방지 — 감사에서 컨퍼런스 참석/R&D Day/전분기 컨콜 오인 확인):
      - "개최목적" 필드에 "실적"/"잠정" 미포함이면 실적 IR이 아님 → None
        (달바글로벌 NDR 사례: 목적 "투자자 이해도 제고", 내용에만 "경영실적 설명" —
         문서 전체 검사로는 통과해 버려 오매칭. 목적 필드 미탐지 시에만 전체 검사 폴백)
      - "N분기 … 실적" 명시 시 target_q와 불일치하면 → None (전분기 컨콜 기각)
    """
    txt = _dart_document_text(rcept_no)
    if txt is None:
        return None
    try:
        # 목적 게이트: 개최목적 필드에서 실적 문구 필수 (NDR·컨퍼런스 참가·R&D Day 제외).
        # 라이브 검증: KB "상반기 경영실적 등 발표"·SK하이닉스 "2분기 경영실적 발표" 통과,
        # 달바 "투자자 이해도 제고"(NDR) 기각, 달바 "2분기 잠정 경영 실적 발표"(진짜 컨콜) 통과.
        mp = re.search(r'개최\s*목적(.{0,160}?)(?:개최\s*방법|$)', txt, re.S)
        gate_txt = mp.group(1) if mp else txt  # 필드 미탐지 시 전체 검사로 폴백(fail-soft)
        if not any(k in gate_txt for k in ("실적", "잠정")):
            return None
        # "N분기 … 실적" 명시 시 대상 분기 일치 검증 (Q1 컨콜을 Q2로 오인 방지)
        if target_q is not None:
            mq = re.search(r'([1-4])\s*분기.{0,20}?실적', txt)
            if mq and int(mq.group(1)) != target_q:
                return None
        m = _IR_DATE_RE.search(txt)
        if not m:
            return None
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception as e:
        logger.warning("IR 원문 파싱 실패 %s: %s", rcept_no, e)
        return None


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


_WISE_SLEEP = 0.3
_WISE_BASE = "https://navercomp.wisereport.co.kr/v2/company"


def _wise_quarter_consensus(code: str, period: str) -> dict | None:
    """WISEreport 분기 Financial Highlight에서 대상 분기 '추정(E)' 컨센서스 추출.

    네이버 컨센서스가 없거나 게이트에서 폐기됐을 때만 호출되는 폴백.
    2단계 요청 (라이브 검증 005930/000660/019210):
      1) c1010001.aspx?cmp_cd={code} 메인 페이지에서 encparam·id 토큰 추출
      2) ajax/cF1001.aspx?fin_typ=0&freq_typ=Q&extY=1&extQ=1&encparam=..&id=..
         → 분기 8컬럼 테이블 HTML (헤더 "2026/06(E)"처럼 추정 컬럼에 (E) 표기,
           행: 매출액/영업이익/당기순이익, 값은 이미 억원, td title 속성에 정밀값)
    대상 분기 컬럼이 (E)가 아니면(이미 실적 반영) None — 실적은 기존 경로가 담당.
    실패·컨센서스 미제공(빈 셀) 시 None (fail-soft).
    """
    m = re.fullmatch(r"(\d{4})Q([1-4])", period)
    if not m:
        return None
    target = f"{m.group(1)}/{int(m.group(2)) * 3:02d}"  # 2026Q2 → 2026/06
    headers = dict(_NAVER_HEADERS)
    headers["Accept"] = "text/html,*/*"
    headers["Referer"] = f"{_WISE_BASE}/c1010001.aspx?cmp_cd={code}"
    try:
        req = urllib.request.Request(f"{_WISE_BASE}/c1010001.aspx?cmp_cd={code}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            main_html = r.read().decode("utf-8", "replace")
        time.sleep(_WISE_SLEEP)
        me = re.search(r"encparam\s*:\s*'([^']+)'", main_html)
        mi = re.search(r"\bid\s*:\s*'([^']+)'", main_html)
        if not me:
            logger.warning("WISE encparam 미발견 %s — 페이지 구조 변경?", code)
            return None
        qs = urllib.parse.urlencode({
            "cmp_cd": code, "fin_typ": 0, "freq_typ": "Q", "extY": 1, "extQ": 1,
            "encparam": me.group(1), "id": mi.group(1) if mi else ""})
        req = urllib.request.Request(f"{_WISE_BASE}/ajax/cF1001.aspx?{qs}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        logger.warning("WISE 분기 조회 실패 %s: %s", code, e)
        return None
    finally:
        time.sleep(_WISE_SLEEP)
    try:
        # 헤더 — 날짜형 <th>만 순서대로 (placeholder 더미 테이블의 th는 날짜형이 아님)
        cols = re.findall(r"<th[^>]*>\s*(\d{4}/\d{2})(\(E\))?", html)
        if not cols:
            return None
        try:
            idx = [c[0] for c in cols].index(target)
        except ValueError:
            return None  # 대상 분기 컬럼 없음
        if not cols[idx][1]:
            return None  # (E) 아님 = 이미 실적 컬럼 — 컨센서스로 쓰지 않음
        vals: dict[str, float | None] = {}
        for key, kw in (("revenue", "매출액"), ("op", "영업이익"), ("np", "당기순이익")):
            vals[key] = None
            # th 텍스트가 정확히 행 이름인 행만 ("영업이익(발표기준)"/"당기순이익(지배)" 제외)
            row = re.search(r"<th[^>]*>\s*" + kw + r"\s*</th>(.*?)</tr>", html, re.S)
            if not row:
                continue
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)
            if idx < len(tds):
                vals[key] = _num(re.sub(r"<[^>]+>", "", tds[idx]))
        if all(v is None for v in vals.values()):
            return None  # 커버리지 없음 — 컨센서스 미제공 종목
        return vals
    except Exception as e:
        logger.warning("WISE 분기 파싱 실패 %s: %s", code, e)
        return None


def _last_actual_quarter(naver: dict, year: int, q: int) -> dict | None:
    """대상 분기 직전의 '실적'(consensus=False) 분기 — 없으면 None.

    4개 분기까지 거슬러 올라가며 찾는다(직전이 아직 미보고일 수 있으므로).
    """
    for back in range(1, 5):
        yy, qq = year, q - back
        while qq <= 0:
            yy, qq = yy - 1, qq + 4
        v = naver.get(f"{yy}Q{qq}")
        if v and not v.get("consensus"):
            return v
    return None


def _consensus_sane(cons: dict, naver: dict, year: int, q: int) -> bool:
    """컨센서스 타당성 게이트 — 통과 못 하면 컨센서스 전체 폐기(재스케일 금지).

    배경(2026-07 조사): FnGuide 상류 오염으로 메가캡(000660/005930/402340)의
    컨센서스·당해연도 수치가 2~4배 부풀려짐. 값이 날마다 요동치므로 매 실행 검증.
    대체 엔드포인트(WISEreport/finance/annual/integration) 전부 동일 오염 확인 —
    공식으로 보정 불가, 탐지 후 폐기만 가능.
    검사 기준은 전년 동분기 '실적'(오염되지 않음이 확인된 열).
    """
    rev, op = cons.get("revenue"), cons.get("op")
    py = naver.get(f"{year - 1}Q{q}") or {}
    if py.get("consensus"):  # 전년 열이 실적이 아니면 비교 불능 — 보수적으로 통과
        py = {}
    py_rev, py_op = py.get("revenue"), py.get("op")
    # 형상 검사: 영업이익 ≫ 매출액 (지주사 오염 사례 402340: op 99,084 vs rev 3,650)
    if rev is not None and op is not None and rev > 0 and op > rev * 1.5:
        return False
    # 기준점은 '직전 실적 분기'를 우선한다.
    #   YoY만 보면 실제 급성장을 오염으로 오판한다 — 2026-08 확인: 삼성전자 2026Q2
    #   컨센 173.9조는 2025Q2(74.6조) 대비 2.33배라 YoY 기준에 걸렸지만,
    #   직전 실적인 2026Q1(133.9조, DART 정기보고서와 일치) 대비로는 1.30배로 정상이었다.
    #   오염분은 직전 실적 대비로도 2~4배로 튀므로 QoQ 기준으로 여전히 걸러진다.
    last = _last_actual_quarter(naver, year, q)
    if rev is not None and last and last.get("revenue"):
        base = last["revenue"]
        if base > 0 and not (0.4 <= rev / base <= 2.5):
            return False
    # 직전 실적이 없을 때만 전년 동분기 기준 — 매출 0.4~2.0배 (금융주는 rev None → 생략)
    elif rev is not None and py_rev is not None and py_rev > 0:
        if not (0.4 <= rev / py_rev <= 2.0):
            return False
    # 영업이익: 0.2~3.0배 — 단 흑→적 전환 전망(op<0)이나 전년 적자/저베이스(마진<3%)는 비율 무의미 → 생략
    # 매출과 같은 이유로 직전 실적 분기를 우선 기준으로 삼는다.
    if op is not None and op > 0 and last and last.get("op") and last["op"] > 0:
        if not (0.2 <= op / last["op"] <= 3.0):
            return False
    elif (op is not None and op > 0 and py_op is not None and py_op > 0
            and py_rev is not None and py_rev > 0 and py_op / py_rev >= 0.03):
        if not (0.2 <= op / py_op <= 3.0):
            return False
    return True


# ── 인베스팅닷컴 (보조 소스 — 예정일) ─────────────────────────────

_INVESTING_URL = "https://kr.investing.com/earnings-calendar/Service/getCalendarFilteredData"
_INVESTING_HEADERS = {
    "User-Agent": _NAVER_HEADERS["User-Agent"],
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://kr.investing.com/earnings-calendar/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
_MARKET_EVENT_CAP = 60  # 시장 전체(scope:"market") 이벤트 상한

# HTML 파싱 — 날짜 구분행(theDay)과 회사행(earnCalCompanyName + 6자리 코드)
_INV_DAY_RE = re.compile(r'class="theDay"[^>]*>\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일')
# 종목코드는 <a> 링크 안에 들어있다 — 예전 마크업의 평문 "(A005930)" 형태가 아니다.
#   <span class="earnCalCompanyName middle">SK텔레콤</span>&nbsp;(<a href="...">017670</a>)
_INV_ROW_RE = re.compile(
    r'earnCalCompanyName[^>]*>([^<]+)</span>.*?>A?(\d{6})<', re.S)


def _investing_html(bgn: dt.date, end: dt.date) -> str:
    """캘린더 HTML 조각 — 사전수집 파일 우선, 없으면 직접 HTTP (실패 시 "").

    인베스팅은 Cloudflare 안티봇이 순수 HTTP를 403으로 막는다(페이지 GET도 차단돼
    쿠키 우회도 불가). 워크플로는 scanner/investing_fetch.js(Playwright)로 미리 받아
    INVESTING_HTML 경로에 넣어 두고, 여기서는 그 파일을 읽는다.
    """
    path = os.environ.get("INVESTING_HTML", "").strip()
    if path and Path(path).exists():
        html = Path(path).read_text("utf-8", "replace")
        logger.info("인베스팅 사전수집 HTML 사용: %s (%d자)", path, len(html))
        return html
    form = [
        ("country[]", "11"),
        ("importance[]", "2"), ("importance[]", "3"),
        ("dateFrom", bgn.isoformat()), ("dateTo", end.isoformat()),
        ("currentTab", "custom"), ("limit_from", "0"),
    ]
    req = urllib.request.Request(
        _INVESTING_URL, data=urllib.parse.urlencode(form).encode(),
        headers=_INVESTING_HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
        return json.loads(body).get("data") or ""
    except Exception as e:
        logger.warning("인베스팅 캘린더 조회 실패(건너뜀): %s", e)
        return ""


def _investing_fetch(bgn: dt.date, end: dt.date) -> list[dict]:
    """인베스팅 한국 실적 캘린더 [{code, name, date}] — 실패 시 [] (fail-soft)."""
    html = _investing_html(bgn, end)
    if not html:
        return []
    rows: list[dict] = []
    cur_date: dt.date | None = None
    try:
        # 날짜행/회사행이 섞인 <tr> 시퀀스를 순서대로 훑는다
        for tr in re.split(r"(?=<tr)", html):
            m = _INV_DAY_RE.search(tr)
            if m:
                cur_date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                continue
            m = _INV_ROW_RE.search(tr)
            if m and cur_date:
                name = re.sub(r"\s+", " ", m.group(1)).strip()
                rows.append({"code": m.group(2), "name": name, "date": cur_date.isoformat()})
    except Exception as e:
        logger.warning("인베스팅 캘린더 파싱 실패(건너뜀): %s", e)
        return []
    logger.info("인베스팅 캘린더 %d건 (%s~%s)", len(rows), bgn, end)
    return rows


def _investing_by_code(today: dt.date, enabled: bool = True) -> dict[str, dict]:
    """{code: {name, date}} — 향후 ~90일 예정 발표만. 과거 행은 DART가 담당하므로 제외."""
    if not enabled:
        return {}
    out: dict[str, dict] = {}
    for r in _investing_fetch(today, today + dt.timedelta(days=90)):
        if r["date"] >= today.isoformat():
            out.setdefault(r["code"], r)  # 종목당 가장 이른 예정일
    return out


# ── 이벤트 생성 ───────────────────────────────────────────────────

def _resolve_consensus(code: str, period: str, year: int, q: int,
                       naver: dict) -> tuple[dict | None, list[dict]]:
    """컨센서스 확정 — 네이버 우선, 부재/폐기 시 WISEreport 추정(E) 폴백.

    반환: (채택 컨센서스|None, 게이트 폐기분 목록 — 자기일관 실제치로 사후 복권 후보)
    """
    nq = naver.get(period) or {}
    consensus = None
    rejected: list[dict] = []
    if nq and nq.get("consensus"):
        consensus = {k: nq.get(k) for k in ("revenue", "op", "np")}
        # 타당성 게이트 — 오염된 컨센서스는 통째로 폐기 (YoY/서프라이즈 오염 방지)
        if not _consensus_sane(consensus, naver, year, q):
            logger.warning("컨센서스 폐기(스케일 이상) %s %s: %s", code, period, consensus)
            rejected.append(consensus)
            consensus = None
    # naver 데이터가 아예 없으면 게이트는 형상 검사(op≤1.5×rev)만 수행하게 됨
    if consensus is None:
        wise = _wise_quarter_consensus(code, period)
        if wise:
            if _consensus_sane(wise, naver, year, q):
                logger.info("WISE 컨센서스 사용 %s %s: %s", code, period, wise)
                consensus = wise
            else:
                logger.warning("WISE 컨센서스 폐기(스케일 이상) %s %s: %s", code, period, wise)
                rejected.append(wise)
    return consensus, rejected

# 작년 공시일 기준 재추정은 +365d가 아니라 +52주 — 365d는 요일이 하루 밀려
# 작년 금요일 발표가 올해 토요일로 찍힌다. 기업은 날짜가 아니라 요일 기준으로 잡는다.
_YEAR_52W = dt.timedelta(weeks=52)


def _bizday(d: dt.date) -> dt.date:
    """주말이면 다음 월요일 — 한국 기업은 주말에 실적을 공시하지 않는다."""
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d

def _build_event(code: str, name: str, today: dt.date, inv: dict | None = None) -> dict:
    year, q = _last_quarter(today)
    period = f"{year}Q{q}"
    qend = _quarter_end(year, q)

    corp = _corp_map().get(code)
    # 최근 ~120일 공시 — 발표일·rcept_no 확정 여부
    recent = _dart_disclosures(corp, today - dt.timedelta(days=120), today)
    # 분기말 이후 접수된 공시만 해당 분기 발표로 인정.
    # 잠정공시는 원문 표를 파싱해 (1) 당해실적 확보, (2) 빈 표(예고공시) 기각 —
    # 예고공시(기아 20260701800569류: 전 항목 '-')는 발표가 아니므로 다음 후보로 넘어간다.
    ann = None
    prov = None       # 잠정공시 원문에서 파싱한 당해실적 (억원)
    prov_tries = 0    # 원문 조회 비용 상한 — 이벤트당 최대 3건
    for x in sorted(recent, key=lambda x: x.get("rcept_dt") or ""):
        rd = x.get("rcept_dt") or ""
        if rd < f"{qend:%Y%m%d}" or x.get("ir"):
            continue
        nm_x = x.get("report_nm") or ""
        periodic_x = any(k in nm_x for k in ("분기보고서", "반기보고서", "사업보고서"))
        # 타 분기 정기보고서 기각 — [기재정정] 사업보고서(작년 결산)가 당분기 발표로 오인되는
        # 사례(LG전자 7/6 정정 사업보고서 → 7/7 잠정공시를 가림). 잠정공시 정정은 유효하므로 유지.
        if periodic_x and ("기재정정" in nm_x or ("사업보고서" in nm_x and q != 4)):
            continue
        if not periodic_x and prov_tries < 3:
            prov_tries += 1
            p = _provisional_actual(x.get("rcept_no"))
            if p == "EMPTY":
                logger.info("예고공시(빈 표) 기각 %s %s — 발표예정 유지", code, x.get("rcept_no"))
                continue  # 발표 아님 — 다음 후보 공시로
            prov = p
        ann = x
        break

    naver = _naver_quarter(code)
    nq = naver.get(period) or {}
    prev = naver.get(f"{year - 1}Q{q}") or {}

    consensus, cons_rejected = _resolve_consensus(code, period, year, q, naver)

    actual = None
    src = "추정"
    rcept_no = None
    if ann:
        rcept_no = ann["rcept_no"]
        periodic = any(k in ann["report_nm"] for k in ("분기보고서", "반기보고서", "사업보고서"))
        src = "확정" if periodic else "잠정"
        if periodic:
            actual = _dart_actual(corp, year, q)
        elif prov:
            actual = prov  # 잠정공시 원문 표에서 직접 파싱한 당해실적 (억원)
        if actual is None and nq and not nq.get("consensus"):
            # 네이버가 이미 실적으로 표시한 분기 — 잠정공시~정기보고서 사이 보강
            actual = {k: nq.get(k) for k in ("revenue", "op", "np")}
    elif nq and not nq.get("consensus"):
        # DART 공시를 못 찾았지만 네이버에 실적 확정 반영된 경우
        actual = {k: nq.get(k) for k in ("revenue", "op", "np")}
        src = "잠정"

    # 실제치도 동일 타당성 게이트 — 공시 표 파싱이 누계/타컬럼을 잡는 사례 차단
    # (예: 삼성전자 잠정공시에서 매출 171조가 파싱되던 오류 — 전년동기 대비 스케일 검사로 폐기)
    # 단 공시 원문의 전년동기·증감율과 자기일관(self_ok)하면 원문이 우선 —
    # 급성장 분기(예: SK하이닉스 2026Q2 매출 +256.8%)가 외부 게이트에 오폐기되지 않게
    self_ok = False
    if actual:
        self_ok = bool(actual.pop("self_ok", False)) if isinstance(actual, dict) else False
        if self_ok:
            logger.info("실제치 자기일관 검증 통과(게이트 생략) %s %s", code, period)
        elif not _consensus_sane(actual, naver, year, q):
            logger.warning("실제치 폐기(스케일 이상) %s %s: %s", code, period, actual)
            actual = None

    # 컨센서스 복권: 게이트 폐기분이 자기일관 검증된 실제치와 스케일 부합(0.5~2x)하면
    # "오염"이 아니라 진짜 급성장 전망이었던 것 — 복원해 서프라이즈 계산에 사용
    # (전년동기 기반 게이트는 급성장 국면에서 컨센서스도 실제치와 같이 오폐기함)
    if consensus is None and cons_rejected and actual and self_ok:
        for cand in cons_rejected:
            pair = next(((a, c) for a, c in [(actual.get("revenue"), cand.get("revenue")),
                                             (actual.get("op"), cand.get("op"))] if a and c), None)
            if pair and 0.5 <= pair[1] / pair[0] <= 2.0:
                logger.info("컨센서스 복권(실제치 스케일 부합) %s %s: %s", code, period, cand)
                consensus = cand
                break

    # 컨퍼런스콜(확정실적 발표)일 — IR개최 공시 원문에서 추출. 날짜 결정보다 먼저 확보:
    # 잠정공시를 안 하는 종목(예: SK하이닉스)은 컨콜이 첫 공개일이라 발표예정일 후보가 됨.
    concall_date = None
    concall_src = None
    concall_max = (qend + dt.timedelta(days=75)).isoformat()
    # 최신 IR 공시부터 최대 3건만 원문 조회 (API 비용 절약)
    for x in sorted((x for x in recent if x.get("ir")),
                    key=lambda x: x.get("rcept_dt") or "", reverse=True)[:3]:
        d_ir = _ir_concall_date(x.get("rcept_no"), target_q=q)
        if d_ir is None:
            continue
        iso = d_ir.isoformat()
        # 하한 = 분기말 — 분기말 이전 IR은 전분기(Q1) 컨콜/일반 IR 오매칭이므로 기각
        if iso < qend.isoformat() or iso > concall_max:
            continue
        concall_date, concall_src = iso, "IR공시"
        break

    # 발표일 — 우선순위: DART 공시 확정 > 인베스팅 예정일
    #   > 작년잠정+365d(잠정 관행 있는 종목만) > IR컨콜일 > 작년정기+365d > 분기말+45d
    if ann:
        rd = ann["rcept_dt"]
        date = f"{rd[:4]}-{rd[4:6]}-{rd[6:]}"
        date_kind, status, date_src = "확정", "발표완료", "공시"
        # 발표완료면 발표일-3d 이전 컨콜은 무관 IR로 보고 기각
        lo_cc = (dt.date.fromisoformat(date) - dt.timedelta(days=3)).isoformat()
        if concall_date and concall_date < lo_cc:
            concall_date = concall_src = None
    elif inv and inv.get("date") and inv["date"] >= qend.isoformat():
        # 분기말 이후 예정된 인베스팅 날짜만 해당 분기 발표로 인정
        date = inv["date"]
        date_kind = "예상"
        status = "발표완료" if actual else "발표예정"
        date_src = "인베스팅"
    else:
        # 작년 동분기 창에서 잠정 관행 여부 판정 + base 선정
        # ("잠정" 우선, [기재정정]·타 분기 사업보고서 제외 — LG화학 정정 사업보고서 오인 사례)
        last_year = _dart_disclosures(
            corp, _quarter_end(year - 1, q), _quarter_end(year - 1, q) + dt.timedelta(days=100))
        prelim = None       # 작년 잠정공시 접수일
        periodic_base = None  # 작년 정기보고서 접수일 (잠정 관행 없는 종목 폴백)
        for x in sorted(last_year, key=lambda x: x.get("rcept_dt") or ""):
            nm = x.get("report_nm") or ""
            rdt = x.get("rcept_dt")
            if x.get("ir") or not rdt or "기재정정" in nm:
                continue
            if "잠정" in nm:
                if prelim is None:
                    prelim = rdt
            elif periodic_base is None and ("사업보고서" not in nm or q == 4):
                periodic_base = rdt
        date_kind = "예상"
        status = "발표완료" if actual else "발표예정"
        if prelim:
            # 잠정 관행 Y — 작년 잠정공시일 +52주
            est = _bizday(dt.date(int(prelim[:4]), int(prelim[4:6]), int(prelim[6:])) + _YEAR_52W)
            date, date_src = est.isoformat(), "추정"
        elif concall_date and concall_date >= today.isoformat():
            # 잠정 관행 N — IR 컨콜이 첫 공개일
            date, date_src = concall_date, "IR공시"
        elif periodic_base:
            est = _bizday(dt.date(int(periodic_base[:4]), int(periodic_base[4:6]),
                                  int(periodic_base[6:])) + _YEAR_52W)
            date, date_src = est.isoformat(), "추정"
        else:
            date, date_src = _bizday(qend + dt.timedelta(days=45)).isoformat(), "추정"

    # stale 처리 — 발표예정인데 예상일이 이미 지난 경우(올해 잠정 미공시 등):
    # 미래 컨콜일이 있으면 그 날로 승격, 없으면 분기말+45d로 재추정하고 date_kind="지연"
    if status == "발표예정" and date < today.isoformat():
        if concall_date and concall_date >= today.isoformat():
            date, date_src, date_kind = concall_date, "IR공시", "예상"
        else:
            date = _bizday(max(qend + dt.timedelta(days=45), today)).isoformat()
            date_kind = "지연"

    # IR 승격 — 추정일보다 IR공시 컨콜일이 빠르면 그 날이 첫 숫자 공개일
    # (NAVER·카카오처럼 잠정 없이 발표=콜 당일인 회사 + 추정 오차 교정. 공식 소스 우선)
    if status == "발표예정" and date_src == "추정" and concall_date and concall_date < date:
        date, date_src, date_kind = concall_date, "IR공시", "예상"

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
        "date_src": date_src, "src": src, "rcept_no": rcept_no,
        "concall_date": concall_date, "concall_src": concall_src,
        "consensus": consensus, "actual": actual,
        "surprise": surprise, "yoy": yoy,
    }


def _market_event(row: dict, today: dt.date) -> dict:
    """추적 외 종목의 인베스팅 예정 발표 — 상세조회 없이 가벼운 이벤트(scope:"market")."""
    d = dt.date.fromisoformat(row["date"])
    year, q = _last_quarter(d)  # 발표일 기준 가장 최근 마감 분기 = 발표 대상 분기 (best-effort)
    return {
        "code": row["code"], "name": row.get("name", ""), "period": f"{year}Q{q}",
        "date": row["date"], "date_kind": "예상", "status": "발표예정",
        "date_src": "인베스팅", "src": "추정", "rcept_no": None, "scope": "market",
        "concall_date": None, "concall_src": None,  # 시장 전체 이벤트는 컨콜일 미추적
        "consensus": None, "actual": None, "surprise": None, "yoy": None,
    }


def _fill_market_consensus(events: list[dict]) -> int:
    """scope:"market" 이벤트에 컨센서스·YoY·서프라이즈 보강 (종목당 네이버 1회 + 필요시 WISE 1회).

    시장 이벤트는 원래 '상세조회 없이 가볍게' 만들어져 컨센서스가 전부 비어 있었다.
    추적 종목과 같은 소스·같은 타당성 게이트를 쓰되, 종목별 실패는 건너뛴다(fail-soft).
    """
    filled = 0
    targets = [e for e in events if e.get("scope") == "market"]
    for i, e in enumerate(targets, 1):
        code, period = e.get("code"), e.get("period") or ""
        if not code or len(period) != 6:
            continue
        year, q = int(period[:4]), int(period[-1])
        try:
            naver = _naver_quarter(code)
            consensus, _ = _resolve_consensus(code, period, year, q, naver)
        except Exception as ex:
            logger.warning("시장 컨센서스 보강 실패 %s: %s", code, ex)
            continue
        nq = naver.get(period) or {}
        prev = naver.get(f"{year - 1}Q{q}") or {}
        # 네이버가 이미 실적으로 표시한 분기 — 잠정공시 원문 파싱이 없었던 건 보강
        if e.get("actual") is None and nq and not nq.get("consensus"):
            vals = {k: nq.get(k) for k in ("revenue", "op", "np")}
            if any(v is not None for v in vals.values()):
                e["actual"] = vals
        e["consensus"] = consensus
        actual = e.get("actual")
        if actual and consensus:
            e["surprise"] = {k: _pct(actual.get(k), consensus.get(k)) for k in ("revenue", "op", "np")}
        ref = actual or consensus
        if ref and prev:
            e["yoy"] = {k: _pct(ref.get(k), prev.get(k)) for k in ("revenue", "op", "np")}
        if consensus or actual:
            filled += 1
        if i % 20 == 0:
            logger.info("시장 컨센서스 보강 %d/%d", i, len(targets))
    logger.info("시장 이벤트 컨센서스 보강 — %d/%d건 확보", filled, len(targets))
    return filled


def _market_provisional_events(tracked: set[str], today: dt.date) -> list[dict]:
    """시장 전체 최근 잠정실적 공시 → scope:"market" 발표완료 이벤트.

    DART list.json을 corp_code 없이 전체 시장으로 조회(pblntf_ty=I 수시공시,
    최근 10일, 최대 5페이지×100건)해 "영업(잠정)실적" 공시를 골라낸다 —
    인베스팅 예정 캘린더가 못 잡는 당일 발표(LG이노텍·두산에너빌리티류) 커버.
    추적 종목은 정규 경로가 처리하므로 제외. 원문 파싱은 최대 25건(비용 상한),
    초과분은 actual 없이 발표 사실만 기록. 실패 시 [] (fail-soft).
    """
    if not DART_KEY:
        return []
    year, q = _last_quarter(today)
    rev_map = {v: k for k, v in _corp_map().items()}  # corp_code → 종목코드 (비상장 제외)
    rows: list[dict] = []
    page = 1
    while page <= 5:
        url = (f"{_DART}/list.json?crtfc_key={DART_KEY}"
               f"&bgn_de={today - dt.timedelta(days=10):%Y%m%d}&end_de={today:%Y%m%d}"
               f"&pblntf_ty=I&page_no={page}&page_count=100")
        try:
            d = _dart_json(url)
        except Exception as e:
            logger.warning("시장 잠정공시 조회 실패 p%d(중단): %s", page, e)
            break
        finally:
            time.sleep(_DART_SLEEP)
        if d.get("status") != "000":
            break
        for x in d.get("list", []) or []:
            if "영업(잠정)실적" in (x.get("report_nm") or ""):
                rows.append(x)
        try:
            total = int(d.get("total_page") or 1)
        except (TypeError, ValueError):
            total = 1
        if page >= total:
            break
        page += 1
    out: list[dict] = []
    seen: set[str] = set()
    doc_budget = 25  # 원문(document.xml) 파싱 비용 상한
    for x in sorted(rows, key=lambda x: x.get("rcept_dt") or "", reverse=True):
        code = rev_map.get((x.get("corp_code") or "").strip())
        rd = x.get("rcept_dt") or ""
        if not code or code in tracked or code in seen or len(rd) != 8:
            continue
        actual = None
        if doc_budget > 0:
            doc_budget -= 1
            p = _provisional_actual(x.get("rcept_no"))
            if p == "EMPTY":
                continue  # 빈 표 = 예고공시 — 발표 아님, 이벤트 제외
            if isinstance(p, dict):
                p.pop("self_ok", None)  # 내부 검증 플래그 — 출력 JSON엔 미노출
            actual = p
            # 형상 검사만 (시장 이벤트는 네이버 미조회) — 영업이익 > 매출 1.5배면 파싱 오류로 폐기
            if actual and actual.get("op") and actual.get("revenue") and abs(actual["op"]) > abs(actual["revenue"]) * 1.5:
                logger.warning("시장 이벤트 실제치 폐기(형상 이상) %s: %s", code, actual)
                actual = None
        seen.add(code)
        out.append({
            "code": code, "name": _corp_names.get(code, x.get("corp_name") or ""),
            "period": f"{year}Q{q}",
            "date": f"{rd[:4]}-{rd[4:6]}-{rd[6:]}", "date_kind": "확정",
            "status": "발표완료", "date_src": "공시", "src": "잠정",
            "rcept_no": x.get("rcept_no"), "scope": "market",
            "concall_date": None, "concall_src": None,
            "consensus": None, "actual": actual, "surprise": None, "yoy": None,
        })
        if len(out) >= _MARKET_EVENT_CAP:
            break
    logger.info("시장 잠정실적 공시 이벤트 %d건 (원문 파싱 %d건)", len(out), 25 - doc_budget)
    return out


def build(codes: dict[str, str], use_investing: bool = True) -> dict:
    today = dt.datetime.now(tz=KST).date()
    _corp_map()  # corp_code·회사명 캐시 선적재 (이름 보강은 첫 이벤트부터 필요)
    inv = _investing_by_code(today, enabled=use_investing)
    events = []
    for i, (code, name) in enumerate(sorted(codes.items()), 1):
        try:
            ev = _build_event(code, name or _corp_names.get(code, ""), today, inv=inv.get(code))
            events.append(ev)
            logger.info("[%d/%d] %s %s — %s %s", i, len(codes), code, name or "", ev["date"], ev["status"])
        except Exception as e:
            logger.warning("이벤트 생성 실패 %s: %s", code, e)
    # 추적 외 종목 — 시장 전체 잠정실적 '발표완료' 이벤트 (DART 전체 시장 공시, fail-soft)
    try:
        dart_market = _market_provisional_events(set(codes), today)
    except Exception as e:
        logger.warning("시장 잠정실적 이벤트 생성 실패(건너뜀): %s", e)
        dart_market = []
    events.extend(dart_market)
    dart_market_codes = {ev["code"] for ev in dart_market}
    # 추적 외 종목 — 시장 전체 예정 발표 (네이버/DART 상세조회 없이 저렴하게, 상한 적용)
    market = [r for c, r in sorted(inv.items())
              if c not in codes and c not in dart_market_codes][:_MARKET_EVENT_CAP]
    for r in market:
        try:
            events.append(_market_event(r, today))
        except Exception as e:
            logger.warning("시장 이벤트 생성 실패 %s: %s", r.get("code"), e)
    if market:
        logger.info("시장 전체(scope:market) 이벤트 %d건 추가", len(market))
    # 시장 이벤트 컨센서스 보강 — 추적 종목과 동일 소스/게이트 (fail-soft)
    try:
        _fill_market_consensus(events)
    except Exception as e:
        logger.warning("시장 컨센서스 보강 실패(건너뜀): %s", e)
    return {
        "updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
        "dart": bool(DART_KEY),
        "months": _months_window(today),
        "events": events,
    }


def _apply_concall_overlay(data: dict) -> dict:
    """웹 리서치 오버레이(concall_overlay.json) 병합.

    스키마: {"items":[{"code","period","concall_date"?,"announce_date"?,"source_url"}]}
    - concall_date: 확정실적·컨퍼런스콜일 — 비어있는 이벤트만 채움 (concall_src "웹")
    - announce_date: 인베스팅닷컴 기준 발표(잠정)예정일 — 발표예정이고 DART 공시 확정이 아닌
      이벤트의 date를 교체 (date_src "인베스팅", date_kind "예상"). 공시 사실은 절대 덮지 않음.
    유효성: YYYY-MM-DD + 분기말~분기말+75d 창(공시확정 이벤트의 concall은 발표일-7d 하한). 불일치·미지 코드는 로그 후 스킵.
    파일 없으면 no-op (fail-soft).
    """
    try:
        if not CONCALL_OVERLAY_PATH.exists():
            return data
        ov = json.loads(CONCALL_OVERLAY_PATH.read_text(encoding="utf-8"))
        items = ov.get("items", []) or []
    except Exception as e:
        logger.warning("concall 오버레이 읽기 실패(건너뜀): %s", e)
        return data
    ev_map = {(e.get("code"), e.get("period")): e for e in data.get("events", [])}
    applied = 0
    for it in items:
        try:
            key = (str(it.get("code", "")).strip(), str(it.get("period", "")).strip())
            cd = str(it.get("concall_date") or "").strip()
            ev = ev_map.get(key)
            if ev is None:
                logger.warning("오버레이 스킵 — 미지 code+period: %s %s", *key)
                continue
            m = re.fullmatch(r"(\d{4})Q([1-4])", key[1])
            if not m:
                logger.warning("오버레이 스킵 — period 형식 오류: %s", key[1])
                continue
            qend = _quarter_end(int(m.group(1)), int(m.group(2)))
            hi = qend + dt.timedelta(days=75)

            # ── 발표예정일 (인베스팅 기준) — DART 공시 사실은 절대 덮지 않음 ──
            ad = str(it.get("announce_date", "") or "").strip()
            if ad and ev.get("status") == "발표예정" and ev.get("date_src") != "공시":
                try:
                    d_ad = dt.date.fromisoformat(ad)
                    if qend <= d_ad <= hi:
                        ev["date"] = ad
                        ev["date_kind"] = "예상"
                        ev["date_src"] = "인베스팅"
                        applied += 1
                    else:
                        logger.warning("오버레이 스킵 — 예정일 범위 밖 %s %s: %s (%s~%s)", *key, ad, qend, hi)
                except ValueError:
                    logger.warning("오버레이 스킵 — 예정일 형식 오류 %s %s: %s", *key, ad)

            # ── 컨콜일 — 비어있는 이벤트만 채움 (IR공시 등 기존 값 우선) ──
            if not cd or ev.get("concall_date"):
                continue
            d_cd = dt.date.fromisoformat(cd)  # 형식 검증 (실패 시 except로)
            # 하한: 발표일이 공시 확정이면 발표일-7d, 추정이면 분기말 —
            # 휴리스틱 예상일이 실제보다 늦을 때 옳은 컨콜일을 기각하지 않도록 (예: 삼성전자 7/30 vs 예상 8/14)
            if ev.get("date_src") == "공시":
                lo = dt.date.fromisoformat(ev["date"]) - dt.timedelta(days=7)
            else:
                lo = qend
            if not (lo <= d_cd <= hi):
                logger.warning("오버레이 스킵 — 날짜 범위 밖 %s %s: %s (%s~%s)", *key, cd, lo, hi)
                continue
            ev["concall_date"] = cd
            ev["concall_src"] = "웹"
            # 소급 승격 — 오버레이로 찾은 컨콜일이 추정 발표예정일보다 빠르면 그 날이 첫 공개일
            # (_build_event의 IR 승격과 동일 규칙, 오버레이 단계에도 적용)
            if ev.get("status") == "발표예정" and ev.get("date_src") == "추정" and cd < ev.get("date", ""):
                ev["date"], ev["date_src"], ev["date_kind"] = cd, "웹", "예상"
            applied += 1
        except Exception as e:
            logger.warning("오버레이 항목 스킵 %s: %s", it, e)
    if applied:
        logger.info("concall 오버레이 %d건 적용", applied)
    return data


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="실적 캘린더 수집기")
    ap.add_argument("--codes", help="쉼표 구분 종목코드 (미지정 시 포트폴리오+유니버스+관심종목)")
    ap.add_argument("--limit", type=int, help="테스트용 — 앞 N종목만")
    ap.add_argument("--no-investing", action="store_true", help="인베스팅 보조 소스 비활성화")
    ap.add_argument("--overlay-only", action="store_true",
                    help="재수집 없이 기존 OUT_PATH에 concall 오버레이만 재병합")
    args = ap.parse_args()

    if args.overlay_only:
        # 워크플로우가 리서치 오버레이 갱신 후 재병합할 때 사용 — 수집 전체를 다시 돌리지 않음
        try:
            d = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("--overlay-only: 기존 %s 읽기 실패: %s", OUT_PATH, e)
            return
        d = _apply_concall_overlay(d)
        OUT_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("오버레이 재병합 저장: %s (이벤트 %d건)", OUT_PATH, len(d.get("events", [])))
        return

    if args.codes:
        all_names = _collect_codes()
        codes = {c.strip(): all_names.get(c.strip(), "") for c in args.codes.split(",") if c.strip()}
    else:
        codes = _collect_codes()
    if args.limit:
        codes = dict(sorted(codes.items())[: args.limit])
    if not DART_KEY:
        logger.warning("DART_API_KEY 없음 — 네이버 컨센서스 + 예상일 휴리스틱만 사용")

    d = build(codes, use_investing=not args.no_investing)
    d = _apply_concall_overlay(d)  # 웹 리서치 오버레이 병합 (없으면 no-op)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s (이벤트 %d건)", OUT_PATH, len(d["events"]))


if __name__ == "__main__":
    main()
