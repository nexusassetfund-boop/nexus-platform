# -*- coding: utf-8 -*-
"""신형 영숫자 종목코드 — 회귀 테스트.

2025년 이후 신규 상장분은 종목코드가 숫자 6자리가 아니라 영숫자 혼합이다
(예: 에이치엘지노믹스 0156T0, 2026-07-24 상장). FDR·네이버·DART 모두 이 코드를
그대로 받는데 우리 쪽 `\\d{6}` 필터가 걸러내 스캔 유니버스·실적 캘린더에서
통째로 빠지고 있었다. FDR KRX-DESC 기준 이런 종목이 79개 있었다.

코드 형식: 숫자로 시작하는 6자리 영숫자. 'KOSDAQ' 같은 6글자 대문자 토큰이
마크업 파싱에서 코드로 새지 않도록 첫 글자는 숫자로 못박는다.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import earnings_calendar  # noqa: E402

CODE_RE = re.compile(r"\d[0-9A-Z]{5}")


def test_code_shape():
    assert CODE_RE.fullmatch("0156T0")     # 신형
    assert CODE_RE.fullmatch("005930")     # 기존
    assert not CODE_RE.fullmatch("KOSDAQ")  # 6글자 대문자 토큰은 코드가 아니다
    assert not CODE_RE.fullmatch("0156T")   # 5자리
    assert not CODE_RE.fullmatch("0156t0")  # 소문자는 정규화 후에만 통과


def test_collect_codes_accepts_new_format():
    codes = {}

    def add(rec):
        c = str(rec.get("code", "")).strip().upper()
        if CODE_RE.fullmatch(c):
            codes.setdefault(c, (rec.get("name") or "").strip())

    for rec in ({"code": "0156T0", "name": "에이치엘지노믹스"},
                {"code": "005930", "name": "삼성전자"},
                {"code": "abc", "name": "잡음"}):
        add(rec)
    assert set(codes) == {"0156T0", "005930"}


def test_investing_row_regex_takes_new_format():
    html = ('<span class="earnCalCompanyName middle">에이치엘지노믹스</span>'
            '&nbsp;(<a href="/x">0156T0</a>)')
    assert earnings_calendar._INV_ROW_RE.findall(html) == [("에이치엘지노믹스", "0156T0")]


if __name__ == "__main__":
    test_code_shape()
    test_collect_codes_accepts_new_format()
    test_investing_row_regex_takes_new_format()
    print("ok")
