# -*- coding: utf-8 -*-
"""증권사 컨센서스 포워드 EPS — WISEreport(네이버 종목분석 백엔드) Financial Highlight 파싱.

소스: https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd=<6자리>
"주요지표" 표의 `YYYY/MM(E)` 추정 컬럼에서 EPS를 추출한다.

원칙 (fail-closed):
  - 파싱은 순수함수 `_parse_consensus(html)` — 구조가 예상과 다르면 오답 대신 None.
  - 네트워크는 `fetch_consensus(code)` — 어떤 예외도 {} 반환, 전체 스캔을 막지 않는다.
  - 컨센서스 미제공 종목(중소형·신규상장)은 (E) 컬럼이 없어 자연히 None.
  - 값은 "참고 전용" — 선정·정렬에 사용하지 않는다 (프론트 라벨 동일).
"""
from __future__ import annotations

import logging
import re
import urllib.request

logger = logging.getLogger("consensus")

_URL = "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _num(s: str):
    """'314,787원' → 314787.0 — 숫자 없으면 None."""
    m = re.search(r"-?[\d,]+(?:\.\d+)?", s or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_consensus(html: str) -> dict | None:
    """Financial Highlight '주요지표' 표에서 (E) 컬럼의 EPS 추출.

    반환: {"fwd_eps": float, "fwd_year": "2026/12(E)"} — 구조 불일치·(E) 없음 → None.
    """
    if not html:
        return None
    # (E) 추정 컬럼이 있는 테이블만 대상
    m = re.search(r"<table[^>]*>(?:(?!</table>).)*?\d{4}/\d{2}\(E\).*?</table>", html, re.S)
    if not m:
        return None
    tbl = m.group(0)
    # 헤더 <th> 태그 안의 연도만 컬럼으로 센다 (caption·summary 텍스트의 연도 오염 방지)
    years = re.findall(r"<th[^>]*>\s*(?:<[^>]+>)*\s*(\d{4}/\d{2}(?:\(E\)|\(A\))?)", tbl)
    est_idx = next((i for i, y in enumerate(years) if y.endswith("(E)")), None)
    if est_idx is None:
        return None
    # EPS 행: <th>EPS…</th> 다음 <td>들에서 est_idx 번째
    row = re.search(r"<th[^>]*>\s*(?:<[^>]+>)*\s*EPS(?:(?!</tr>).)*?</tr>", tbl, re.S)
    if not row:
        return None
    cells = re.findall(r"<td[^>]*>\s*([^<]*)", row.group(0))
    if len(cells) <= est_idx:
        return None
    eps = _num(cells[est_idx])
    if eps is None or eps == 0:
        return None
    return {"fwd_eps": eps, "fwd_year": years[est_idx]}


_AJAX = ("https://navercomp.wisereport.co.kr/v2/company/ajax/cF1001.aspx"
         "?cmp_cd={code}&fin_typ=0&freq_typ=Y&encparam={enc}")


def _parse_consensus_multi(html: str) -> list[dict]:
    """cF1001 연간 하이라이트에서 (E) 다개년 EPS 추출.

    추정 컬럼은 <td class="... bgE ..."> + title 속성(정밀값)으로 식별.
    반환: [{"year": "2026/12(E)", "eps": float}, ...] — 구조 불일치 시 [].
    """
    if not html:
        return []
    years, seen = [], set()
    for y in re.findall(r"\d{4}/\d{2}\(E\)", html):
        if y not in seen:
            seen.add(y)
            years.append(y)
    row = re.search(r"<tr[^>]*>(?:(?!</tr>).)*?>EPS(?:(?!</tr>).)*?</tr>", html, re.S)
    if not row or not years:
        return []
    est = re.findall(r'<td[^>]*class="[^"]*bgE[^"]*"[^>]*title="([^"]*)"', row.group(0))
    out = []
    for y, s in zip(years, est):
        v = _num(s)
        if v is not None and v != 0:
            out.append({"year": y, "eps": v})
    return out


def fetch_consensus_multi(code: str) -> list[dict]:
    """종목코드 → 다개년 컨센서스 EPS 리스트. 실패·미제공 시 [] (fail-closed).

    encparam(페이지 토큰) 확보용 본문 1회 + ajax 1회 = 종목당 2요청.
    """
    try:
        req = urllib.request.Request(_URL.format(code=code), headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            main = r.read().decode("utf-8", errors="replace")
        m = re.search(r"encparam\s*[:=]\s*['\"]([^'\"]+)", main)
        if not m:
            return []
        req = urllib.request.Request(
            _AJAX.format(code=code, enc=m.group(1)),
            headers={"User-Agent": _UA, "Referer": _URL.format(code=code),
                     "X-Requested-With": "XMLHttpRequest"})
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="replace")
        return _parse_consensus_multi(html)
    except Exception as e:
        logger.warning("다개년 컨센서스 조회 실패 %s: %s", code, e)
        return []


def fetch_consensus(code: str) -> dict:
    """종목코드(6자리) → 컨센서스 dict. 실패·미제공 시 {} (스캔 진행에 영향 없음)."""
    try:
        req = urllib.request.Request(_URL.format(code=code), headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="replace")
        return _parse_consensus(html) or {}
    except Exception as e:
        logger.warning("컨센서스 조회 실패 %s: %s", code, e)
        return {}
