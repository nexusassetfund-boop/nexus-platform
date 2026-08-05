# -*- coding: utf-8 -*-
"""인베스팅 캘린더 파싱 자체검증 — 2026-08 기준 실제 응답 마크업 발췌."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from earnings_calendar import _INV_DAY_RE, _INV_ROW_RE  # noqa: E402

# kr.investing.com getCalendarFilteredData 실제 응답에서 발췌 (종목코드가 <a> 안에 있음)
SAMPLE = """        <tr tablesorterdivider>
            <td colspan="9" class="theDay">2026년 8월 5일 수요일</td>
        </tr>
                        <tr>
                    <td class="flag"><span title="한국" class="ceFlags South_Korea middle"></span></td>
                    <td class="left noWrap earnCalCompany" title="SK텔레콤" _p_pid="43472">
                        <span class="earnCalCompanyName middle">SK텔레콤</span>&nbsp;(<a href="/equities/sk-telecom-co-ltd-earnings" class="bold middle" target="_blank">017670</a>)
                    </td>
                    <td class="right">19.47T</td>
                </tr>
        <tr tablesorterdivider>
            <td colspan="9" class="theDay">2026년 8월 7일 금요일</td>
        </tr>
                        <tr>
                    <td class="left noWrap earnCalCompany" title="파마리서치">
                        <span class="earnCalCompanyName middle">파마리서치</span>&nbsp;(<a href="/equities/pharma-research-earnings" class="bold middle" target="_blank">214450</a>)
                    </td>
                </tr>
"""


def _parse(html):
    import re
    import datetime as dt
    rows, cur = [], None
    for tr in re.split(r"(?=<tr)", html):
        m = _INV_DAY_RE.search(tr)
        if m:
            cur = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            continue
        m = _INV_ROW_RE.search(tr)
        if m and cur:
            rows.append({"code": m.group(2), "name": re.sub(r"\s+", " ", m.group(1)).strip(),
                         "date": cur.isoformat()})
    return rows


def test():
    rows = _parse(SAMPLE)
    assert rows == [
        {"code": "017670", "name": "SK텔레콤", "date": "2026-08-05"},
        {"code": "214450", "name": "파마리서치", "date": "2026-08-07"},
    ], rows
    print("OK: investing rows parsed (code inside <a> markup)")


if __name__ == "__main__":
    test()
