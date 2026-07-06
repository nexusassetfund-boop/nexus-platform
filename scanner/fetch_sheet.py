"""
넥서스 모델 포트폴리오 — 구글 시트 미러링.

공개 구글 시트(CSV export)를 읽어 docs/data/portfolio.json 으로 변환한다.
시트가 원본(source of truth)이고, 웹은 이 JSON을 읽어 렌더한다. (Apps Script 불필요)

시트: https://docs.google.com/spreadsheets/d/1dpKl_YP9eAquU-G0Et9tcpyzw1YlziKvwoJzH4OsHfw
"""
from __future__ import annotations
import csv
import io
import json
import logging
import urllib.request
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("fetch_sheet")
KST = ZoneInfo("Asia/Seoul")

SHEET_ID = "1dpKl_YP9eAquU-G0Et9tcpyzw1YlziKvwoJzH4OsHfw"
GID = {
    "universe": 0,
    "holdings": 316234505,
    "kpi": 538734711,
    "transactions": 1051935097,
    "nav": 1482234975,
}
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "portfolio.json"


def _fetch_csv(gid: int) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nexus-mirror"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def _num(s):
    """'−₩9,124,700.00', '(3,206,394원)', '17.5x', '-6.37%' → float. 없으면 None."""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in ("-", "#DIV/0!", "#REF!", "#N/A", "#VALUE!"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    for ch in ("₩", "원", "%", ",", "배", "x", "X", "주", "+", " "):
        s = s.replace(ch, "")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _clean(s: str) -> str:
    return (s or "").replace("\n", " ").strip()


def _find_header_row(rows, must_have):
    """must_have의 모든 라벨을 포함하는 행의 인덱스를 찾는다."""
    for i, r in enumerate(rows):
        cells = [c.strip() for c in r]
        if all(any(lbl == c for c in cells) for lbl in must_have):
            return i
    return None


def parse_kpi(rows, holdings_rows) -> dict:
    """대시보드 KPI — 보유 시트 요약 블록 + KPI 시트 지표를 합친다."""
    kpi = {}
    # 보유 시트(holdings sheet) 요약 블록에서 핵심 수치
    for i, r in enumerate(holdings_rows):
        c = [x.strip() for x in r]
        if "포트폴리오 가치" in c:
            # 다음 행이 값
            v = holdings_rows[i + 1] if i + 1 < len(holdings_rows) else []
            v = [x.strip() for x in v]
            kpi["portfolio_value"] = _num(v[1]) if len(v) > 1 else None
            kpi["cash"] = _num(v[4]) if len(v) > 4 else None
            kpi["eval_pnl"] = _num(v[5]) if len(v) > 5 else None
            kpi["eval_pnl_pct"] = _num(v[6]) if len(v) > 6 else None
            kpi["cum_pnl"] = _num(v[7]) if len(v) > 7 else None
            kpi["cum_pnl_pct"] = _num(v[8]) if len(v) > 8 else None
            # 전일대비 행
            d = holdings_rows[i + 2] if i + 2 < len(holdings_rows) else []
            d = [x.strip() for x in d]
            if d and "전일대비" in d[1:3] + d[:1]:
                kpi["day_change"] = _num(d[2]) if len(d) > 2 else None
                kpi["day_change_pct"] = _num(d[3]) if len(d) > 3 else None
                for cell in d:
                    if "실현손익" in cell:
                        kpi["realized_pnl"] = _num(cell.split("실현손익")[-1])
            break
    # KPI 시트: 누적 기준가, 기업 수, 지수
    for i, r in enumerate(rows):
        c = [x.strip() for x in r]
        if "누적 기준가" in c:
            v = rows[i + 1] if i + 1 < len(rows) else []
            v = [x.strip() for x in v]
            # 누적 기준가는 같은 열
            j = c.index("누적 기준가")
            kpi["nav_index"] = _num(v[j]) if len(v) > j else None
        if any("총 투자 기업" in x for x in c):
            kpi["n_stocks"] = next((_num(c[k + 1]) for k, x in enumerate(c) if "총 투자 기업" in x and k + 1 < len(c)), None)
            kpi["n_win"] = next((_num(c[k + 1]) for k, x in enumerate(c) if "수익 기업" in x and k + 1 < len(c)), None)
            kpi["n_loss"] = next((_num(c[k + 1]) for k, x in enumerate(c) if "손실 기업" in x and k + 1 < len(c)), None)
        if "코스피 지수" in c:
            labels = c
            vals = [x.strip() for x in rows[i + 1]] if i + 1 < len(rows) else []
            chgs = [x.strip() for x in rows[i + 3]] if i + 3 < len(rows) else []
            idx = {}
            name_map = {"코스피 지수": "kospi", "코스닥 지수": "kosdaq", "다우 지수": "dow",
                        "나스닥 지수": "nasdaq", "S&P500 지수": "sp500", "원/달러 환율": "usdkrw"}
            for k, lbl in enumerate(labels):
                if lbl in name_map:
                    idx[name_map[lbl]] = {
                        "value": _num(vals[k]) if k < len(vals) else None,
                        "change": _num(chgs[k]) if k < len(chgs) else None,
                    }
            kpi["indices"] = idx
    return kpi


def parse_holdings(rows) -> list[dict]:
    hi = _find_header_row(rows, ["종목명", "잔고수량", "평가손익"])
    if hi is None:
        return []
    hdr = [h.strip() for h in rows[hi]]
    col = {name: k for k, name in enumerate(hdr)}
    def g(r, name):
        k = col.get(name)
        return r[k].strip() if k is not None and k < len(r) else ""
    out = []
    for r in rows[hi + 1:]:
        name = g(r, "종목명")
        if not name:
            break  # 데이터 끝 (아래는 메모/빈행)
        out.append({
            "strategy": _clean(g(r, "전략")),
            "code": g(r, "종목코드"),
            "name": name,
            "qty": _num(g(r, "잔고수량")),
            "price": _num(g(r, "현재가")),
            "change_pct": _num(g(r, "등락률(%)")),
            "avg_price": _num(g(r, "평균매입가")),
            "eval_pnl": _num(g(r, "평가손익")),
            "return_pct": _num(g(r, "수익률(%)")),
            "contrib_pct": _num(g(r, "수익 기여(%)")),
            "eval_amount": _num(g(r, "평가 금액")),
            "buy_amount": _num(g(r, "매입 금액")),
            "target_price": _num(g(r, "목표주가")),
            "buy_date": g(r, "매입일"),
            "hold_days": _num(g(r, "보유기간(일)")),
            "weight": g(r, "비중"),
            "memo": _clean(g(r, "메모")),
        })
    return out


def parse_universe(rows) -> list[dict]:
    hi = _find_header_row(rows, ["종목명", "목표주가", "투자포인트"])
    if hi is None:
        return []
    hdr = [h.strip() for h in rows[hi]]
    col = {name: k for k, name in enumerate(hdr)}
    def g(r, name):
        k = col.get(name)
        return r[k].strip() if k is not None and k < len(r) else ""
    out = []
    strat = ""
    for r in rows[hi + 1:]:
        s = _clean(g(r, "전략"))
        if s:
            strat = s
        name = g(r, "종목명")
        if not name:
            continue  # 빈 템플릿 행 건너뜀
        out.append({
            "strategy": strat,
            "sector": _clean(g(r, "섹터/테마")),
            "code": g(r, "종목코드"),
            "subclass": _clean(g(r, "소분류")),
            "name": name,
            "price": _num(g(r, "현재가")),
            "change_pct": _num(g(r, "등락률(%)")),
            "target_price": _num(g(r, "목표주가")),
            "upside_pct": _num(g(r, "상승여력(%)")),
            "mktcap": _clean(g(r, "시총")),
            "per": _clean(g(r, "PER")),
            "eps": _clean(g(r, "EPS")),
            "high52": _num(g(r, "high52")),
            "low52": _num(g(r, "low52")),
            "point": _clean(g(r, "투자포인트")),
            "risk": _clean(g(r, "리스크")),
        })
    return out


def parse_transactions(rows) -> list[dict]:
    hi = _find_header_row(rows, ["매매구분", "실현손익", "거래금액"])
    if hi is None:
        return []
    hdr = [h.strip() for h in rows[hi]]
    col = {}
    for k, name in enumerate(hdr):
        if name and name not in col:
            col[name] = k
    def g(r, name):
        k = col.get(name)
        return r[k].strip() if k is not None and k < len(r) else ""
    out = []
    for r in rows[hi + 1:]:
        date = g(r, "날짜")
        name = g(r, "종목명")
        if not date or not name:
            break
        out.append({
            "strategy": _clean(g(r, "전략")),
            "date": date,
            "code": g(r, "종목코드"),
            "name": name,
            "side": g(r, "매매구분"),
            "qty": _num(g(r, "수량")),
            "unit_price": _num(g(r, "단가")),
            "amount": _num(g(r, "거래금액")),
            "realized_pnl": _num(g(r, "실현손익")),
            "fee": _num(g(r, "거래 수수료")),
            "tax": _num(g(r, "세금")),
            "memo": _clean(g(r, "메모")),
        })
    return out


def parse_nav(rows) -> dict:
    """차트(indexed): 오른쪽 블록 '기준가'(base 1,000, 기준가/KOSPI/KOSDAQ).
    내역 표(value): 왼쪽 블록 '포트폴리오 가치'(전체 계좌 가치) + 총평가·평가손익."""
    def _dkey(d):
        try:
            return tuple(int(p) for p in d.replace(".", "-").split("-")[:3])
        except ValueError:
            return (0, 0, 0)

    # ── 차트용: 오른쪽 블록 '기준가' ──
    series = []
    hcol = hrow = None
    for i, r in enumerate(rows):
        for k, cell in enumerate(r):
            if cell.strip() == "기준가":
                hrow, hcol = i, k
                break
        if hcol is not None:
            break
    if hcol is not None:
        for r in rows[hrow + 1:]:
            date = r[hcol - 1].strip() if hcol - 1 < len(r) else ""
            nav = _num(r[hcol]) if hcol < len(r) else None
            kospi = _num(r[hcol + 1]) if hcol + 1 < len(r) else None
            kosdaq = _num(r[hcol + 2]) if hcol + 2 < len(r) else None
            if not date or nav in (None, 0):
                continue
            series.append({"date": date.replace(" ", ""), "nav": nav, "kospi": kospi, "kosdaq": kosdaq})

    # ── 내역 표용: 왼쪽 블록 '포트폴리오 가치' (날짜 중복제거 + 최신순) ──
    raw = {}
    hi = _find_header_row(rows, ["총 평가 금액", "포트폴리오가치"])
    if hi is not None:
        hdr = [h.strip() for h in rows[hi]]
        col = {name: k for k, name in enumerate(hdr)}
        def g(r, name):
            k = col.get(name)
            return r[k].strip() if k is not None and k < len(r) else ""
        for r in rows[hi + 1:]:
            date = g(r, "날짜")
            pv = _num(g(r, "포트폴리오가치"))
            if not date or pv in (None, 0):
                continue
            d = date.replace(" ", "")
            raw[d] = {
                "date": d,
                "portfolio_value": pv,
                "total_value": _num(g(r, "총 평가 금액")),
                "eval_pnl": _num(g(r, "평가손익")),
            }
    value_series = sorted(raw.values(), key=lambda s: _dkey(s["date"]), reverse=True)
    return {"indexed": series, "value": value_series}


def build() -> dict:
    logger.info("구글 시트 미러링 시작")
    sheets = {k: _fetch_csv(g) for k, g in GID.items()}
    holdings_rows = sheets["holdings"]
    data = {
        "updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
        "source": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}",
        "kpi": parse_kpi(sheets["kpi"], holdings_rows),
        "holdings": parse_holdings(holdings_rows),
        "universe": parse_universe(sheets["universe"]),
        "transactions": parse_transactions(sheets["transactions"]),
        "nav": parse_nav(sheets["nav"]),
    }
    return data


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장: %s (보유 %d, 유니버스 %d, 거래 %d, NAV %d)",
                OUT_PATH, len(data["holdings"]), len(data["universe"]),
                len(data["transactions"]), len(data["nav"]["indexed"]))


if __name__ == "__main__":
    main()
