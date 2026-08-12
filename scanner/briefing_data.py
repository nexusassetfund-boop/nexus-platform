# -*- coding: utf-8 -*-
"""시황 브리핑 입력 데이터 수집 — 확정 숫자는 코드가 만들고, 해설만 Claude가 쓴다.

장전(am):  밤사이 글로벌 지수·금리·환율·원자재 + 전일 국내 마감 + 감지기 요약
           + 전일 시총 상위 15 등락률·외인/기관 수급 + 전략실·섹터맵 요약
장마감(pm): 국내 지수·등락 종목수·투자자 수급·업종 등락 + 글로벌 + 감지기 요약 + 아침 브리핑(복기용)
야간(night): 코스피200 야간선물 스냅샷만 Worker에 기록 (04:50 KST — 세션이 아직 열려 있을 때)

출력: briefing_input.json (repo 루트, gitignore 대상). night 모드는 파일을 쓰지 않는다.
사용: python scanner/briefing_data.py --mode am|pm|night   (미지정 시 KST 시각으로 자동)
"""
import argparse
import json
import os
import re
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


def _top_caps_rank_naver(n=15):
    """시총 순위·등락률 폴백 — 네이버 시총 상위 페이지 (순위·등락률은 정확값)"""
    import re
    out = {}
    for mkt, sosok in (("KOSPI", "0"), ("KOSDAQ", "1")):
        t = requests.get(f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page=1",
                         headers={"user-agent": "Mozilla/5.0"}, timeout=15).content.decode("euc-kr", "ignore")
        rows = re.findall(r'href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>.*?'
                          r'<td class="number">[\d,]+</td>.*?([+-][\d,]+|0).*?([+-]?[\d.]+)%',
                          t, re.S)
        out[mkt] = [{"code": c, "name": nm.strip(), "change_pct": _num(pct)} for c, nm, _, pct in rows[:n]]
    return out


def _kis_flows(codes, base_ymd):
    """KIS inquire-investor — 종목별 외국인/기관 순매수 거래대금(백만원 → 억원 환산, 정확값).

    응답에서 base_ymd(YYYYMMDD) 날짜 행을 골라 다른 세션 값이 섞이지 않게 한다.
    """
    import asyncio
    sys.path.insert(0, str(Path(__file__).parent))
    from data_provider import load_config, kis_get

    async def run():
        cfg = load_config()
        out = {}
        for code in codes:
            try:
                data = await kis_get(cfg, "/uapi/domestic-stock/v1/quotations/inquire-investor",
                                     "FHKST01010900",
                                     {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
                for row in (data or {}).get("output") or []:
                    if row.get("stck_bsop_date") == base_ymd:
                        out[code] = {
                            "foreign_bn": round(float(row.get("frgn_ntby_tr_pbmn") or 0) / 100, 1),
                            "inst_bn": round(float(row.get("orgn_ntby_tr_pbmn") or 0) / 100, 1),
                        }
                        break
            except Exception:
                pass
            await asyncio.sleep(0.15)  # KIS 레이트리밋
        return out
    return asyncio.run(run())


def _cme_front_month():
    """CME 연계 야간선물(코스피200) 최근월물 단축코드 — KIS 종목마스터에서 매번 읽는다.

    fo_cme_code.mst: 1행=선물, 2행=스프레드. 선물 첫 행이 최근월물(예: A01609 = 2026-09물).
    월물 롤오버를 코드에 하드코딩하지 않기 위한 것.
    """
    import io
    import zipfile
    r = requests.get("https://new.real.download.dws.co.kr/common/master/fo_cme_code.mst.zip",
                     timeout=20, verify=False)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    for line in z.read(z.namelist()[0]).decode("cp949").splitlines():
        if line[0:1] == "1":
            return line[1:10].strip()
    return None


def night_quote_is_preopen_reset(q):
    """야간세션이 끝나고 장전 리셋된 시세인가.

    07:20 장전 시점에 KIS를 부르면 야간 체결이 지워지고 전일 종가·등락 0.00·거래량 0이 온다.
    이걸 그대로 실으면 브리핑에 "야간선물 981.15(보합, 0.00%)"처럼 매일 가짜 보합이 찍힌다
    (실제 사고 2026-08-06·08-07 am — 8/7 야간선물은 실제로 +1.45%였다).

    사후 조회로는 야간 종가를 복원할 수 없다(08시대에 부르면 장전에 흘러가는 또 다른 값이 온다).
    그래서 04:50 스냅샷이 정답이고, 이 판별기는 리셋된 값이 새어 나가는 것만 막는다.
    """
    if not q:
        return True
    if (q.get("volume") or 0) > 0:
        return False
    prev = q.get("prev_close")
    return q.get("change") == 0 and (prev is None or q.get("value") == prev)


def collect_night_futures():
    """코스피200 야간선물(CME 연계 KRX 야간시장) 종가 — 장전 전략용.

    야간장은 18:00~익일 05:00이므로 07:20 장전 시점엔 직전 밤 세션이 이미 끝나 있고,
    KIS 시세는 그때 이미 전일 종가로 리셋돼 있다. 그래서 세션이 열려 있는 04:50에
    `--mode night`으로 미리 찍어 Worker에 넣어 두고(put_night_snapshot), 장전엔 그걸 읽는다.
    라이브 조회는 스냅샷이 없을 때의 보루이며, 리셋으로 판정되면 버린다.

    KIS 선물옵션 시세(FHMIF10000000, FID_COND_MRKT_DIV_CODE=F)에 CME 연계 야간물 코드
    (A016… 최근월물)를 넣어 조회한다. 등락은 부호 없이 오므로 prdy_vrss_sign으로 부호를 붙인다.
    수치를 못 얻으면 키를 비워 둔다 — 근사치는 넣지 않는다.
    """
    import asyncio
    sys.path.insert(0, str(Path(__file__).parent))
    from data_provider import load_config, kis_get

    async def run():
        cfg = load_config()
        out = {}
        try:
            cme = _cme_front_month()
        except Exception as e:
            out["cme_code_error"] = str(e)[:100]
            cme = None
        if cme:
            try:
                data = await kis_get(cfg, "/uapi/domestic-futureoption/v1/quotations/inquire-price",
                                     "FHMIF10000000",
                                     {"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": cme})
                o = (data or {}).get("output1") or {}
                sign = -1 if str(o.get("prdy_vrss_sign") or "3") in ("4", "5") else 1
                last = _num(o.get("futs_prpr"))
                if last is not None:
                    out["cme_night"] = {
                        "code": cme,
                        "name": o.get("hts_kor_isnm"),
                        "value": last,
                        "change": sign * abs(_num(o.get("futs_prdy_vrss")) or 0),
                        "change_pct": sign * abs(_num(o.get("futs_prdy_ctrt")) or 0),
                        "prev_close": _num(o.get("futs_prdy_clpr")),
                        "high": _num(o.get("futs_hgpr")),
                        "low": _num(o.get("futs_lwpr")),
                        "basis": _num(o.get("basis")),          # 선물 - 현물
                        "open_interest": _num(o.get("hts_otst_stpl_qty")),
                        "volume": _num(o.get("acml_vol")),
                    }
            except Exception as e:
                out["cme_night_error"] = str(e)[:100]
        return out
    return asyncio.run(run())


def _admin_headers():
    token = os.environ.get("NEXUS_ADMIN_TOKEN")
    return {"authorization": f"Bearer {token}"} if token else None


def is_night_session(now=None):
    """KRX 야간 파생 세션(18:00~06:00 KST) 시간대인가."""
    hour = (now or datetime.now(KST)).hour
    return hour >= 18 or hour < 6


def put_night_snapshot(today_str):
    """`--mode night` (04:50 KST, 야간세션 진행 중) — 야간선물 시세를 Worker에 기록한다."""
    h = _admin_headers()
    if not h:
        print("NEXUS_ADMIN_TOKEN 없음 — 야간선물 스냅샷 건너뜀")
        return
    # 세션 밖(특히 정규장 중)에 부르면 주간 시세가 야간선물로 둔갑해 저장된다.
    # 리셋 판별은 거래량 0을 보므로 이건 못 걸러낸다 — 시각으로 막는다.
    if not is_night_session():
        print(f"야간세션 시간이 아님({datetime.now(KST):%H:%M} KST) — 스냅샷 건너뜀")
        return
    q = (collect_night_futures() or {}).get("cme_night")
    if not q or night_quote_is_preopen_reset(q):
        print(f"야간선물 시세 없음/리셋 상태 — 기록 안 함: {q}")
        return
    body = {**q, "date": today_str, "captured_at": datetime.now(KST).isoformat()}
    r = requests.put(f"{WORKER}/api/night-futures", headers=h, json=body, timeout=20)
    print(f"PUT /api/night-futures -> {r.status_code} {r.text[:120]}")
    r.raise_for_status()


def get_night_snapshot(today_str):
    """장전 — 오늘 새벽에 찍어 둔 야간선물 스냅샷. 날짜가 다르면 낡은 값이므로 쓰지 않는다."""
    h = _admin_headers()
    if not h:
        return {"error": "NEXUS_ADMIN_TOKEN 없음"}
    try:
        r = requests.get(f"{WORKER}/api/night-futures", headers=h, timeout=15)
        if r.status_code != 200:
            return {"error": f"스냅샷 없음 ({r.status_code})"}
        snap = r.json()
    except Exception as e:
        return {"error": str(e)[:100]}
    if snap.get("date") != today_str:
        return {"error": f"스냅샷이 오늘({today_str}) 것이 아님: {snap.get('date')}"}
    return {"cme_night": snap}


def collect_night_futures_for_am(today_str):
    """장전용 야간선물 — 스냅샷 우선, 없으면 라이브(리셋이면 폐기)."""
    snap = get_night_snapshot(today_str)
    if snap.get("cme_night"):
        return snap
    live = collect_night_futures() or {}
    q = live.get("cme_night")
    if q and not night_quote_is_preopen_reset(q):
        return {**live, "source": "live"}
    # 가짜 보합을 싣느니 비워 둔다 — 프롬프트가 야간선물 행 자체를 생략한다
    return {"error": snap.get("error") or "야간선물 시세 없음(장전 리셋)", "dropped_live": q}


def collect_top_caps(today_str, n=15):
    """장전 전용 — 전일 시총 상위 n종목의 등락률 + 외국인/기관 순매수 (KOSPI/KOSDAQ 각각)

    순위·등락률: pykrx(KRX) 기본, 실패 시 네이버 시총 페이지.
    수급: pykrx 투자자별 거래대금 기본, 실패 시 KIS inquire-investor 거래대금 — 둘 다 정확값.
    둘 다 실패하면 수급 열은 비워 두고 오류를 기록한다 (근사치는 쓰지 않는다).
    """
    out = {}
    prev = None
    try:
        prev = _prev_trading_day(today_str)
    except Exception:
        pass
    # 1) 시총 순위 + 등락률
    try:
        from pykrx import stock
        if not prev:
            raise RuntimeError("직전 거래일 산출 실패")
        for mkt in ("KOSPI", "KOSDAQ"):
            caps = stock.get_market_cap_by_ticker(prev, market=mkt)
            ohlcv = stock.get_market_ohlcv_by_ticker(prev, market=mkt)
            top = caps.sort_values("시가총액", ascending=False).head(n)
            out[mkt] = [{
                "code": code,
                "name": stock.get_market_ticker_name(code),
                "mktcap_tr": round(cap["시가총액"] / 1e12, 2),  # 조원
                "change_pct": round(float(ohlcv.loc[code, "등락률"]), 2) if code in ohlcv.index else None,
            } for code, cap in top.iterrows()]
    except Exception as e:
        out["pykrx_rank_error"] = str(e)[:100]
        try:
            out.update(_top_caps_rank_naver(n))
        except Exception as e2:
            out["rank_fallback_error"] = str(e2)[:100]
            return out
    if not prev:  # pykrx 캘린더 실패 시 네이버 순위는 최신 거래일 기준
        prev = today_str.replace("-", "")
    out["base_date"] = f"{prev[:4]}-{prev[4:6]}-{prev[6:]}"
    # 2) 수급 — pykrx 우선, 실패 종목은 KIS로 보충
    codes = [r["code"] for mkt in ("KOSPI", "KOSDAQ") for r in out.get(mkt) or []]
    flows = {}
    try:
        from pykrx import stock
        for code in codes:
            tv = stock.get_market_trading_value_by_date(prev, prev, code)
            r0 = tv.iloc[0]
            flows[code] = {"foreign_bn": round(float(r0["외국인합계"]) / 1e8, 1),
                           "inst_bn": round(float(r0["기관합계"]) / 1e8, 1)}
    except Exception as e:
        out["pykrx_flow_error"] = str(e)[:100]
    missing = [c for c in codes if c not in flows]
    if missing:
        try:
            flows.update(_kis_flows(missing, prev))
        except Exception as e:
            out["kis_flow_error"] = str(e)[:100]
    for mkt in ("KOSPI", "KOSDAQ"):
        for r in out.get(mkt) or []:
            r.update(flows.get(r["code"], {}))
    out["unit"] = "foreign_bn/inst_bn=억원 순매수, mktcap_tr=조원"
    return out


_JSON_CACHE = {}


def _get_json(path, timeout=15):
    # 한 번 실행하고 끝나는 스크립트라 프로세스 캐시로 충분하다. scan.json은 350종목
    # 짜리 큰 파일인데 감지기·섹터RS·전략교차 세 군데서 같은 URL을 부른다.
    if path not in _JSON_CACHE:
        _JSON_CACHE[path] = requests.get(f"{WORKER}{path}", timeout=timeout).json()
    return _JSON_CACHE[path]


def _note(*parts):
    return " · ".join(str(p) for p in parts if p)


CODE_RE = re.compile(r"\d[0-9A-Z]{5}")   # 신형 영숫자 코드(0156T0) 포함

# 전략 소스 — 홈 화면 '전략 교차' 카드(nexus-cloud/public/index.html의 _HOME_X)와 같은 규칙.
# '지금 후보 명단'을 내놓는 소스만 넣는다. 백테스터 3종(무상증자·주도주 승격·Post IPO)은
# 과거 이벤트 시뮬레이션이라 현재 명단이 아니므로 제외. 반면 신고가 후보는 매일 갱신되는
# 현재 명단이라 포함한다.
# (키, 라벨, 경로, 리스트필드, 코드필드, 필터, 근거)
STRATEGY_SOURCES = [
    ("stage", "스테이지 감지기", "/data/scan.json", "results", "ticker",
     lambda r: (r.get("stage") or 0) >= 1,
     lambda r: _note(r.get("stage_label") or f"Stage {r.get('stage')}",
                     f"RS {round(r['rs_rank'])}" if r.get("rs_rank") is not None else None)),
    ("portfolio", "전략 포트폴리오 보유", "/data/tracking.json", "holdings", "ticker", None,
     lambda r: _note(f"보유 {r.get('days_held', '-')}일",
                     f"{r['return_pct']:+.1f}%" if r.get("return_pct") is not None else None)),
    ("quality", "퀄리티 성장", "/data/quality_growth.json", "candidates", "code", None,
     lambda r: _note(f"종합Z {r['composite']:.2f}" if r.get("composite") is not None else None,
                     f"ROE {r['roe']:.1f}%" if r.get("roe") is not None else None)),
    ("value", "가치투자", "/data/value_screen.json", "candidates", "code", None,
     lambda r: _note(f"안전마진 {r.get('margin')}%" if r.get("margin") is not None else None,
                     f"신뢰 {r['conf']}" if r.get("conf") else None)),
    ("pullback", "눌림목", "/data/pullback.json", "candidates", "ticker", None,
     lambda r: _note(f"점수 {r.get('pullback_score', '-')}/7")),
    ("canslim", "CANSLIM", "/data/canslim.json", "candidates", "ticker", None,
     lambda r: _note(f"점수 {r.get('canslim_score', '-')}", "7요건" if r.get("strict") == 1 else None)),
    # 관찰(고가까지 7% 초과)까지 넣으면 명단이 묽어진다 — 실제로 문턱에 붙은 상태만 교차에 태운다.
    ("nhcand", "신고가 후보", "/data/newhigh_candidates.json", "candidates", "code",
     lambda r: r.get("status") in ("breaking", "imminent", "near", "touched_failed"),
     # gap은 0 이하가 '이미 넘었다'는 뜻 — 부호를 그대로 쓰면 돌파를 '남음'으로 읽는다
     lambda r: _note((f"{abs(r['gap']):.1f}% 돌파" if r["gap"] <= 0 else f"고가까지 {r['gap']:.1f}%")
                     if r.get("gap") is not None else None,
                     f"RS {r['rs']}" if r.get("rs") is not None else None)),
    # 수급은 S·A(외국인 기준)만 교차 대상. C(기관 단독)는 백테스트에서 (−) 신호라
    # 섞으면 교차 명단이 오히려 나쁜 종목을 추천하는 꼴이 된다.
    ("flow", "수급", "/data/flow.json", "candidates", "ticker",
     lambda r: r.get("grade") in ("S", "A"),
     lambda r: _note(f"{r.get('grade')}등급", f"외인 {r.get('frgn_streak', '-')}일")),
]


def collect_strategy_cross():
    """2개 이상 전략에 동시에 이름을 올린 종목 — 전략실 탭들의 교집합.

    탭별 상위 종목을 나열하는 것보다 교집합이 브리핑에서 훨씬 쓸모 있다.
    소스마다 갱신 주기가 달라(감지기·수급은 매 거래일, 퀄리티·가치는 주 1회)
    as_of를 반드시 함께 넘긴다 — 없으면 전부 '오늘 명단'으로 읽힌다.
    """
    sources, by_code, quotes = {}, {}, {}
    for key, label, path, list_field, code_field, flt, note in STRATEGY_SOURCES:
        try:
            d = _get_json(path)
        except Exception as e:
            sources[key] = {"label": label, "error": str(e)[:80]}
            continue
        rows = [r for r in (d.get(list_field) or []) if isinstance(r, dict)]
        if key == "stage":
            quotes = {r.get("ticker"): r for r in rows}   # 등락률 조인용 (스캔 유니버스 350종목)
        rows = [r for r in rows if flt(r)] if flt else rows
        members = []
        for r in rows:
            code = str(r.get(code_field) or "")
            if not CODE_RE.fullmatch(code):
                continue
            members.append(r)
            rec = by_code.setdefault(code, {"code": code, "name": r.get("name"), "hits": []})
            # 원장 2트랙처럼 한 소스에 같은 종목이 두 줄일 수 있다 — 소스당 1건만
            if not any(h["key"] == key for h in rec["hits"]):
                rec["hits"].append({"key": key, "label": label, "why": note(r)})
        sources[key] = {"label": label, "count": len(members),
                        "as_of": str(d.get("updated") or d.get("scan_time")
                                     or d.get("data_last_date") or "")[:10] or None,
                        "top": [{"name": r.get("name"), "why": note(r)} for r in members[:3]]}
    rows = sorted((r for r in by_code.values() if len(r["hits"]) >= 2),
                  key=lambda r: (-len(r["hits"]), r["name"] or ""))[:12]
    for r in rows:
        r["change_pct"] = (quotes.get(r["code"]) or {}).get("change_pct")
        r["strategies"] = [f"{h['label']}({h['why']})" if h["why"] else h["label"]
                           for h in r.pop("hits")]
    return {"rule": "2개 이상 전략에 동시에 잡힌 종목 (수급은 외국인 S·A등급만 집계)",
            "sources": sources, "rows": rows}


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
    try:  # 전략 교차 — 이 섹션의 머리. 탭별 상위 종목(sources[].top)도 여기서 함께 나온다
        out["cross"] = collect_strategy_cross()
    except Exception as e:
        out["cross_error"] = str(e)[:100]
    try:  # 관세청 수출 통관 데이터 — 종목별 월간 수출액(매출 프록시). 분기 실적을 2~7주 선행 관측.
        # 예전엔 이 블록을 '모멘텀 원장'이라 잘못 부르고 유니버스 종목 수·샘플 이름만 실어
        # 브리핑 마지막 문단이 정보량 0이었다. 실제 신호(분기 급증·급감)만 넘긴다.
        td = _get_json("/data/trade.json")
        stocks = [s for s in (td.get("stocks") or []) if isinstance(s, dict)]
        surge = sorted([s for s in stocks if s.get("q_sum_yoy") is not None],
                       key=lambda s: -s["q_sum_yoy"])[:5]
        out["trade_export"] = {
            "what": "관세청 시군구별 품목별 통관실적 기반 종목 수출 추적 — 월간 확정치, 매출 프록시",
            "data_month": td.get("data_month"), "universe_count": len(stocks),
            "surge": [{"name": s.get("name"), "item": s.get("label"),
                       "q_yoy_pct": s.get("q_sum_yoy"), "flags": s.get("flags")} for s in surge],
            # 분기 -30% 급감은 백테스트에서 가장 신뢰도 높았던 신호 (6개월 초과수익 중앙 -16.1%)
            "drop": [{"name": s.get("name"), "item": s.get("label"), "q_yoy_pct": s.get("q_sum_yoy")}
                     for s in stocks if "q_drop" in (s.get("flags") or [])][:5],
        }
    except Exception as e:
        out["trade_export_error"] = str(e)[:100]
    try:  # 신고가 후보 상태 분포 — 개별 종목은 cross.sources.nhcand가 이미 싣는다.
        # 돌파 11 / 관찰 14는 같은 후보 수라도 장세 해석이 정반대라 분포만 따로 넘긴다.
        nc = _get_json("/data/newhigh_candidates.json")
        out["newhigh_candidates"] = {
            "what": "52주 신고가까지 남은 거리로 추린 관찰 리스트 — 돌파한 뒤가 아니라 돌파 전을 본다",
            "as_of": nc.get("data_last_date"), "counts": nc.get("counts"),
        }
    except Exception as e:
        out["newhigh_candidates_error"] = str(e)[:100]
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
        scan = _get_json("/data/scan.json")
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
        tr = _get_json("/data/tracking.json")
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


def is_trading_day(today_str, mode="pm"):
    """당일이 거래일인지.

    pykrx 판정은 '당일 지수 데이터가 이미 존재하는가'라서 장전(07:20)에는 항상 전 거래일을
    돌려준다 — 즉 am에는 쓸 수 없다. am은 요일만 보고, 휴장일 정밀 판정은 pm에서만 한다.
    """
    weekday_ok = datetime.strptime(today_str, "%Y-%m-%d").weekday() < 5
    if mode == "am" or not weekday_ok:
        # ponytail: am은 요일 게이트만 — 공휴일 아침에도 브리핑이 나간다.
        # 신뢰할 만한 KRX 휴장일 캘린더 소스가 생기면 그때 정밀 판정.
        return weekday_ok
    try:
        from pykrx.stock import get_nearest_business_day_in_a_week
        return get_nearest_business_day_in_a_week(today_str.replace("-", "")) == today_str.replace("-", "")
    except Exception:
        return True  # 판별 실패 시 주중이면 진행


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["am", "pm", "night"], default=None)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (테스트용 — 기본 오늘)")
    args = ap.parse_args()
    now = datetime.now(KST)
    mode = args.mode or ("am" if now.hour < 12 else "pm")
    today_str = args.date or now.strftime("%Y-%m-%d")

    if mode == "night":
        # 야간세션 중 스냅샷만 찍고 끝 — briefing_input.json은 건드리지 않는다
        put_night_snapshot(today_str)
        return

    if not is_trading_day(today_str, mode):
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
        try:
            data["night_futures"] = collect_night_futures_for_am(today_str)  # 코스피200 야간선물
        except Exception as e:
            data["night_futures_error"] = str(e)[:100]

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"mode={mode} world={len(data['world'])} -> {OUT}")


if __name__ == "__main__":
    main()
