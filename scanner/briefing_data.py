# -*- coding: utf-8 -*-
"""시황 브리핑 입력 데이터 수집 — 확정 숫자는 코드가 만들고, 해설만 Claude가 쓴다.

장전(am):  밤사이 글로벌 지수·금리·환율·원자재 + 전일 국내 마감 + 감지기 요약
장마감(pm): 국내 지수·등락 종목수·투자자 수급·업종 등락 + 글로벌 + 감지기 요약 + 아침 브리핑(복기용)

출력: briefing_input.json (repo 루트, gitignore 대상)
사용: python scanner/briefing_data.py --mode am|pm   (미지정 시 KST 시각으로 자동)
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    prev = closes[-2] if len(closes) >= 2 else res["meta"].get("chartPreviousClose")
    if last is None or not prev:
        return None
    return {"value": round(last, 2), "change": round(last - prev, 2),
            "change_pct": round((last / prev - 1) * 100, 2)}


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

    data = {
        "mode": mode,
        "generated_at": now.isoformat(),
        "date": today_str,
        "world": collect_world(),
        "kr_index": collect_kr_index(today_str),  # KOSPI/KOSDAQ 공식 일별 시세 (네이버 — 이 값을 우선 사용)
        "detector": collect_detector(),
        "prev_briefings": collect_prev_briefings(mode, today_str),
    }
    if mode == "pm":
        data["kr_close"] = collect_kr_close(today_str)

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"mode={mode} world={len(data['world'])} -> {OUT}")


if __name__ == "__main__":
    main()
