# -*- coding: utf-8 -*-
"""시황 브리핑 입력 데이터 수집 — 확정 숫자는 코드가 만들고, 해설만 Claude가 쓴다.

장전(am):  밤사이 글로벌 지수·금리·환율·원자재 + 전일 국내 마감 + 감지기 요약
           + 전일 시총 상위 15 등락률·외인/기관 수급 + 전략실·섹터맵 요약
장마감(pm): 국내 지수·등락 종목수·투자자 수급·업종 등락 + 글로벌 + 감지기 요약 + 아침 브리핑(복기용)

출력: briefing_input.json (repo 루트, gitignore 대상)
사용: python scanner/briefing_data.py --mode am|pm   (미지정 시 KST 시각으로 자동)
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

KST = timezone(timedelta(hours=9))
WORKER = "https://nexus-platform.nexusassetfund.workers.dev"
OUT = Path(__file__).resolve().parents[1] / "briefing_input.json"

YAHOO = [
    ("KOSPI", "^KS11"), ("KOSDAQ", "^KQ11"),
    ("S&P500", "^GSPC"), ("나스닥", "^IXIC"), ("다우", "^DJI"), ("나스닥선물", "NQ=F"),
    ("VIX", "^VIX"), ("미국채10Y", "^TNX"), ("달러인덱스", "DX-Y.NYB"),
    ("WTI", "CL=F"), ("금", "GC=F"), ("원달러", "KRW=X"), ("비트코인", "BTC-USD"),
]


def yahoo_quote(sym):
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d",
        headers={"user-agent": "Mozilla/5.0"}, timeout=15)
    res = r.json()["chart"]["result"][0]
    last = res["meta"].get("regularMarketPrice")
    bars = [(t, c) for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0]["close"])
            if c is not None]
    closes = [c for _, c in bars]
    prev = closes[-2] if len(closes) >= 2 else res["meta"].get("chartPreviousClose")
    if last is None or not prev:
        return None
    out = {"value": round(last, 2), "change": round(last - prev, 2),
           "change_pct": round((last / prev - 1) * 100, 2)}
    # 이 값이 어느 세션의 것인지 거래소 현지 날짜로 못박는다 — 브리핑이 "밤사이"를 하루 전 세션으로
    # 잘못 잡아 이틀 전 종목 등락률을 끌어오는 사고(2026-07-23 pm: 7/21 마이크론 +12.17%)를 막는 앵커.
    if bars:
        try:
            tz = ZoneInfo(res["meta"].get("exchangeTimezoneName") or "America/New_York")
            out["session_date"] = datetime.fromtimestamp(bars[-1][0], tz).strftime("%Y-%m-%d")
        except Exception:
            pass
    return out


def collect_world():
    out = {}
    for name, sym in YAHOO:
        try:
            q = yahoo_quote(sym)
            if q:
                out[name] = q
        except Exception:
            pass
    return out


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def collect_kr_index(today_str):
    """KOSPI/KOSDAQ 일별 시세 — 네이버 (야후보다 전일비가 정확)"""
    out = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        try:
            rows = requests.get(f"https://m.stock.naver.com/api/index/{mkt}/price?pageSize=5&page=1",
                                headers={"user-agent": "Mozilla/5.0"}, timeout=15).json()
            r0 = rows[0]
            sign = -1 if r0.get("compareToPreviousPrice", {}).get("name") == "FALLING" else 1
            out[mkt] = {
                "date": r0.get("localTradedAt"),
                "value": _num(r0.get("closePrice")),
                "change": sign * abs(_num(r0.get("compareToPreviousClosePrice")) or 0),
                "change_pct": sign * abs(_num(r0.get("fluctuationsRatio")) or 0),
                "high": _num(r0.get("highPrice")),
                "low": _num(r0.get("lowPrice")),
            }
        except Exception as e:
            out[f"{mkt}_error"] = str(e)[:100]
    return out


def collect_kr_close(today_str):
    """장마감 전용 — 등락 종목수·투자자 수급·업종 등락 (전부 네이버)"""
    import re
    out = {}
    ymd = today_str.replace("-", "")
    H = {"user-agent": "Mozilla/5.0"}
    # 상승/하락/보합 종목수 — 시세 페이지
    try:
        adv = {}
        for mkt, code in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")):
            t = requests.get(f"https://finance.naver.com/sise/sise_index.naver?code={code}",
                             headers=H, timeout=15).content.decode("euc-kr", "ignore")
            d = {}
            for kind, cnt in re.findall(r'(상승|보합|하락)종목수</span><a[^>]*><span>([\d,]+)', t):
                d[kind] = int(cnt.replace(",", ""))
            if d:
                adv[mkt] = d
        if adv:
            out["advance_decline"] = adv
    except Exception as e:
        out["advance_decline_error"] = str(e)[:100]
    # 투자자 수급 (억 원) — 투자자별 매매동향
    try:
        flows = {}
        for mkt, sosok in (("KOSPI", "01"), ("KOSDAQ", "02")):
            t = requests.get(f"https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate={ymd}&sosok={sosok}",
                             headers=H, timeout=15).content.decode("euc-kr", "ignore")
            cells = re.findall(r"<td[^>]*>\s*([\d,.\-+]+)\s*</td>", t)
            # 행 구조: 날짜, 개인, 외국인, 기관계, ... — 대상일 행을 찾는다
            want = f"{today_str[2:4]}.{today_str[5:7]}.{today_str[8:10]}"
            for i, c in enumerate(cells):
                if c == want and i + 3 < len(cells):
                    flows[mkt] = {"개인": _num(cells[i + 1]), "외국인": _num(cells[i + 2]), "기관": _num(cells[i + 3]),
                                  "unit": "억원", "date": today_str}
                    break
        if flows:
            out["investor_flows"] = flows
    except Exception as e:
        out["investor_flows_error"] = str(e)[:100]
    # 업종 등락 — 네이버 업종별 시세
    try:
        import pandas as pd
        from io import StringIO
        # pd.read_html에 URL을 직접 주면 cp949 페이지가 잘못 디코드되어 컬럼명이 깨진다(업종명 KeyError)
        html = requests.get("https://finance.naver.com/sise/sise_group.naver?type=upjong",
                            headers=H, timeout=15).content.decode("cp949", "replace")
        tables = pd.read_html(StringIO(html))
        df = max(tables, key=len)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]  # 멀티헤더 평탄화
        df = df.dropna(subset=["업종명"]).copy()
        df["chg"] = df["전일대비"].map(_num)
        df = df.dropna(subset=["chg"]).sort_values("chg", ascending=False)
        out["sectors_top"] = [{"name": r["업종명"], "chg_pct": r["chg"]} for _, r in df.head(5).iterrows()]
        out["sectors_worst"] = [{"name": r["업종명"], "chg_pct": r["chg"]} for _, r in df.tail(5).iterrows()][::-1]
    except Exception as e:
        out["sectors_error"] = str(e)[:100]
    return out


def _prev_trading_day(today_str):
    """직전 거래일 (YYYYMMDD) — pykrx 영업일 캘린더 기준"""
    from pykrx.stock import get_nearest_business_day_in_a_week
    prev = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y%m%d")
    return get_nearest_business_day_in_a_week(prev)


def _naver_stock_flows(code):
    """종목별 외국인/기관 순매수 근사 (억원) — 네이버 frgn 페이지 최신 거래일 행.

    페이지는 순매매 '수량'만 제공하므로 종가를 곱해 금액을 근사한다.
    """
    import pandas as pd
    from io import StringIO
    html = requests.get(f"https://finance.naver.com/item/frgn.naver?code={code}",
                        headers={"user-agent": "Mozilla/5.0"}, timeout=15).content.decode("euc-kr", "ignore")
    df = max(pd.read_html(StringIO(html)), key=lambda d: d.shape[0] * d.shape[1])
    df.columns = ["날짜", "종가", "전일비", "등락률", "거래량", "기관", "외국인", "보유주수", "보유율"][:len(df.columns)]
    df = df.dropna(subset=["날짜"])
    r0 = df.iloc[0]
    close = _num(r0["종가"])
    return {"foreign_bn": round(_num(r0["외국인"]) * close / 1e8, 0),
            "inst_bn": round(_num(r0["기관"]) * close / 1e8, 0)}


def _top_caps_naver(n=15):
    """폴백 — 네이버 시총 상위 페이지 + 종목별 frgn 수급 근사"""
    import re
    import time
    out = {}
    for mkt, sosok in (("KOSPI", "0"), ("KOSDAQ", "1")):
        t = requests.get(f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page=1",
                         headers={"user-agent": "Mozilla/5.0"}, timeout=15).content.decode("euc-kr", "ignore")
        rows = re.findall(r'href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>.*?'
                          r'<td class="number">[\d,]+</td>.*?([+-][\d,]+|0).*?([+-]?[\d.]+)%',
                          t, re.S)
        items = [{"code": c, "name": nm.strip(), "change_pct": _num(pct)} for c, nm, _, pct in rows[:n]]
        for it in items:
            try:
                it.update(_naver_stock_flows(it["code"]))
            except Exception:
                pass
            time.sleep(0.2)
        out[mkt] = items
    return out


def collect_top_caps(today_str, n=15):
    """장전 전용 — 전일 시총 상위 n종목의 등락률 + 외국인/기관 순매수 (KOSPI/KOSDAQ 각각)

    pykrx(KRX)가 기본. KRX 인증 이슈로 실패하면 네이버 폴백(수급 없이 등락률만).
    """
    out = {}
    try:
        from pykrx import stock
        prev = _prev_trading_day(today_str)
        out["base_date"] = f"{prev[:4]}-{prev[4:6]}-{prev[6:]}"
        for mkt in ("KOSPI", "KOSDAQ"):
            caps = stock.get_market_cap_by_ticker(prev, market=mkt)
            ohlcv = stock.get_market_ohlcv_by_ticker(prev, market=mkt)
            top = caps.sort_values("시가총액", ascending=False).head(n)
            rows = []
            for code, cap in top.iterrows():
                row = {
                    "code": code,
                    "name": stock.get_market_ticker_name(code),
                    "mktcap_tr": round(cap["시가총액"] / 1e12, 2),  # 조원
                    "change_pct": round(float(ohlcv.loc[code, "등락률"]), 2) if code in ohlcv.index else None,
                }
                try:  # 종목별 투자자 순매수 거래대금 (원) → 억원
                    tv = stock.get_market_trading_value_by_date(prev, prev, code)
                    r0 = tv.iloc[0]
                    row["foreign_bn"] = round(float(r0["외국인합계"]) / 1e8, 0)
                    row["inst_bn"] = round(float(r0["기관합계"]) / 1e8, 0)
                except Exception:
                    pass
                rows.append(row)
            out[mkt] = rows
        out["unit"] = "foreign_bn/inst_bn=억원 순매수, mktcap_tr=조원"
    except Exception as e:
        out["pykrx_error"] = str(e)[:100]
        try:
            out.update(_top_caps_naver(n))
            out["base_date"] = today_str
            out["note"] = "pykrx 실패 → 네이버 폴백 (수급은 순매매수량×종가 근사, 억원)"
            out["unit"] = "foreign_bn/inst_bn=억원 순매수(근사)"
        except Exception as e2:
            out["fallback_error"] = str(e2)[:100]
    return out


def _get_json(path, timeout=15):
    return requests.get(f"{WORKER}{path}", timeout=timeout).json()


def collect_platform(today_str):
    """장전 전용 — 넥서스 전략실·섹터맵 요약 (전부 공개 KV 데이터, 탭별 핵심만 압축)"""
    out = {}
    try:  # 섹터 ETF RS — 장기 RS 기준 상위/하위 (단기 RS·5일 수익률 동반)
        scan = _get_json("/data/scan.json")
        rs = scan.get("sector_etf_rs") or {}
        rows = sorted([{"sector": k, "etf": v.get("etf_name"), "long_rs": v.get("long_rs"),
                        "short_rs": v.get("short_rs"), "ret_d5": v.get("ret_d5")}
                       for k, v in rs.items() if isinstance(v, dict) and v.get("long_rs") is not None],
                      key=lambda x: -x["long_rs"])
        out["sector_rs"] = {"top": rows[:3], "bottom": rows[-3:]}
    except Exception as e:
        out["sector_rs_error"] = str(e)[:100]
    try:  # 퀄리티 성장 상위 5
        qg = (_get_json("/data/quality_growth.json").get("candidates") or [])[:5]
        out["quality_growth"] = [{"name": c.get("name"), "composite": c.get("composite")} for c in qg]
    except Exception as e:
        out["quality_growth_error"] = str(e)[:100]
    try:  # 눌림목 — 후보 수 + 상위 3
        cands = _get_json("/data/pullback.json").get("candidates") or []
        out["pullback"] = {"count": len(cands),
                           "top": [{"name": c.get("name"), "score": c.get("pullback_score")}
                                   for c in cands[:3]]}
    except Exception as e:
        out["pullback_error"] = str(e)[:100]
    try:  # 밸류 보드 상위 3
        vs = (_get_json("/data/value_screen.json").get("candidates") or [])[:3]
        out["value_screen"] = [{"name": c.get("name")} for c in vs]
    except Exception as e:
        out["value_screen_error"] = str(e)[:100]
    try:  # 모멘텀 원장(웨지팝) — 커버리지 유니버스 요약 (보유 현황은 detector.tracking에 있음)
        td = _get_json("/data/trade.json")
        stocks = td.get("stocks") or []
        out["momentum_ledger"] = {"data_month": td.get("data_month"), "universe_count": len(stocks),
                                  "sample_names": [s.get("name") for s in stocks[:5] if isinstance(s, dict)]}
    except Exception as e:
        out["momentum_ledger_error"] = str(e)[:100]
    try:  # 실적 캘린더 — 오늘·내일 예정 최대 5건
        ev = _get_json("/data/earnings_calendar.json").get("events") or []
        soon = [e for e in ev if e.get("date") and today_str <= e["date"] <=
                (datetime.strptime(today_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")]
        out["earnings_upcoming"] = [{"name": e.get("name"), "date": e.get("date")} for e in soon[:5]]
    except Exception as e:
        out["earnings_error"] = str(e)[:100]
    return out


def collect_detector():
    """감지기·전략 포트폴리오 요약 (KV 데이터) — 우리 플랫폼만의 차별점"""
    out = {}
    try:
        scan = requests.get(f"{WORKER}/data/scan.json", timeout=15).json()
        rs = scan.get("results") or []
        stages = {}
        for r in rs:
            stages[r.get("stage_label", "?")] = stages.get(r.get("stage_label", "?"), 0) + 1
        top = sorted([r for r in rs if r.get("confidence")], key=lambda x: -x["confidence"])[:5]
        out["scan"] = {
            "scan_time": scan.get("scan_time"),
            "total": len(rs),
            "stage_distribution": stages,
            "top_confidence": [{"name": t["name"], "stage": t.get("stage_label"), "confidence": t["confidence"],
                                "change_pct": t.get("change_pct")} for t in top],
        }
    except Exception as e:
        out["scan_error"] = str(e)[:100]
    try:
        tr = requests.get(f"{WORKER}/data/tracking.json", timeout=15).json()
        out["tracking"] = {
            "holdings": [{"name": h.get("name"), "return_pct": h.get("return_pct"), "entry_date": h.get("entry_date")}
                         for h in (tr.get("holdings") or [])],
            "stats": tr.get("stats") or {},
        }
    except Exception as e:
        out["tracking_error"] = str(e)[:100]
    return out


def collect_prev_briefings(mode, today_str):
    """직전 브리핑 — 장마감 브리핑의 '장전 전망 복기'와 논조 연속성에 사용"""
    out = {}
    try:
        idx = requests.get(f"{WORKER}/data/briefings.json", timeout=15).json()
        items = idx.get("items") or []
        out["recent_titles"] = [{"type": i["type"], "title": i["title"], "date": i["date"]} for i in items[:6]]
        if mode == "pm":
            am_id = f"{today_str}-am"
            if any(i["id"] == am_id for i in items):
                out["today_am_briefing"] = requests.get(f"{WORKER}/data/briefing/{am_id}", timeout=15).json()
    except Exception as e:
        out["prev_error"] = str(e)[:100]
    return out


def is_trading_day(today_str):
    try:
        from pykrx.stock import get_nearest_business_day_in_a_week
        return get_nearest_business_day_in_a_week(today_str.replace("-", "")) == today_str.replace("-", "")
    except Exception:
        return datetime.now(KST).weekday() < 5  # 판별 실패 시 주중이면 진행


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["am", "pm"], default=None)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (테스트용 — 기본 오늘)")
    args = ap.parse_args()
    now = datetime.now(KST)
    mode = args.mode or ("am" if now.hour < 12 else "pm")
    today_str = args.date or now.strftime("%Y-%m-%d")

    if not is_trading_day(today_str):
        print(f"휴장일({today_str}) — 브리핑 생성 건너뜀")
        OUT.write_text(json.dumps({"skip": True, "reason": "휴장일"}, ensure_ascii=False), "utf-8")
        sys.exit(0)

    world = collect_world()
    data = {
        "mode": mode,
        "generated_at": now.isoformat(),
        "date": today_str,
        # world의 미국 자산이 어느 정규장 세션 종가인지 — am/pm 모두 "밤사이"의 정의는 이 날짜다
        "us_session_date": (world.get("S&P500") or {}).get("session_date"),
        "world": world,
        "kr_index": collect_kr_index(today_str),  # KOSPI/KOSDAQ 공식 일별 시세 (네이버 — 이 값을 우선 사용)
        "detector": collect_detector(),
        "prev_briefings": collect_prev_briefings(mode, today_str),
    }
    if mode == "pm":
        data["kr_close"] = collect_kr_close(today_str)
    else:
        data["top_caps"] = collect_top_caps(today_str)      # 전일 시총 상위 15 등락률·수급
        data["platform"] = collect_platform(today_str)      # 전략실·섹터맵 요약

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"mode={mode} world={len(data['world'])} -> {OUT}")


if __name__ == "__main__":
    main()
