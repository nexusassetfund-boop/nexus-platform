#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""무상증자 이벤트드리븐 백테스터 데이터 수집 (넥서스 이벤트드리븐 > 무상증자 탭용)

1. DART 공시검색(list.json, 주요사항보고서) → 2024-01-01 이후 '무상증자결정' 공시
   - 유무상증자·정정공시 제외 (원 공시의 접수일이 발표일)
2. DART fricDecsn.json → 1주당 신주배정 주식수·신주배정기준일·신주상장예정일 보강
3. pykrx(KRX 정보데이터시스템) → 수정주가 일봉 OHLC (실패 시 FinanceDataReader 폴백)
   - 수정주가라 권리락 전후 가격이 연속적 → 발표일 매수 수익률 계산이 왜곡되지 않음
결과: docs/data/bonus_backtest.json (repo 커밋 안 함 — post_bonus_backtest.py가 Worker KV로 게시)

환경변수: DART_API_KEY (필수)
"""

import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

socket.setdefaulttimeout(20)

BGN_DATE = "2024-01-01"          # 백테스트 시작 — 이 날짜 이후 공시만
PRE_BARS = 5                     # 공시일 이전 컨텍스트 봉 수
MAX_BARS_AFTER = 220             # 공시일(포함) 이후 최대 봉 수 (~10.5개월) — KV 용량 관리
OUT = Path(__file__).resolve().parents[1] / "docs" / "data" / "bonus_backtest.json"
DART = "https://opendart.fss.or.kr/api"
DART_KEY = os.environ.get("DART_API_KEY", "").strip()


def _dart_get(endpoint: str, **params) -> dict:
    params["crtfc_key"] = DART_KEY
    for attempt in range(1, 4):
        try:
            r = requests.get(f"{DART}/{endpoint}", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"    DART {endpoint} 실패 ({attempt}/3): {e}")
            time.sleep(5 * attempt)
    return {}


# ──────────────────────────────────────────
# 1. 공시검색 — 무상증자결정 목록
# ──────────────────────────────────────────

def month_windows(bgn: str, end: str):
    """DART list.json 조회를 월 단위 구간으로 분할 (장기간 조회 제한 회피)."""
    cur = datetime.strptime(bgn, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    while cur <= stop:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        yield cur.strftime("%Y%m%d"), min(nxt, stop).strftime("%Y%m%d")
        cur = nxt + timedelta(days=1)


def fetch_disclosures() -> list[dict]:
    """주요사항보고서 중 순수 '무상증자결정' 공시 (KOSPI/KOSDAQ, 정정 제외)."""
    today = datetime.now().strftime("%Y-%m-%d")
    found: dict[str, dict] = {}  # rcept_no → row
    for bgn_de, end_de in month_windows(BGN_DATE, today):
        page = 1
        while True:
            data = _dart_get("list.json", bgn_de=bgn_de, end_de=end_de,
                             pblntf_ty="B", page_no=page, page_count=100,
                             last_reprt_at="N")
            status = data.get("status")
            if status == "013":  # 조회 결과 없음
                break
            if status != "000":
                print(f"  list.json {bgn_de}~{end_de} p{page} status={status} {data.get('message','')}")
                break
            for row in data.get("list", []):
                nm = row.get("report_nm", "")
                if "무상증자결정" not in nm:
                    continue
                if "유무상" in nm or "정정" in nm:  # 유무상증자·기재정정 제외
                    continue
                if row.get("corp_cls") not in ("Y", "K"):
                    continue
                if not (row.get("stock_code") or "").strip():
                    continue
                found[row["rcept_no"]] = row
            if page >= int(data.get("total_page", 1)):
                break
            page += 1
        time.sleep(0.3)
    print(f"  무상증자결정 공시: {len(found)}건")
    return sorted(found.values(), key=lambda r: r["rcept_dt"])


# ──────────────────────────────────────────
# 2. fricDecsn — 배정비율·기준일·상장예정일
# ──────────────────────────────────────────

def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _date_iso(v):
    s = "".join(ch for ch in str(v or "") if ch.isdigit())
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else None


def fetch_details(corp_codes: set[str]) -> dict[str, dict]:
    """corp_code별 fricDecsn 조회 → rcept_no → 상세 매핑."""
    today = datetime.now().strftime("%Y%m%d")
    bgn = BGN_DATE.replace("-", "")
    details: dict[str, dict] = {}
    for i, corp in enumerate(sorted(corp_codes), 1):
        data = _dart_get("fricDecsn.json", corp_code=corp, bgn_de=bgn, end_de=today)
        for row in data.get("list", []) or []:
            rno = row.get("rcept_no")
            if not rno:
                continue
            new_shares = _num(row.get("nstk_ostk_cnt"))
            base_shares = _num(row.get("bfic_tisstk_ostk"))
            ratio = _num(row.get("nstk_ascnt_ps_ostk"))
            if ratio is None and new_shares and base_shares:
                ratio = round(new_shares / base_shares, 4)
            details[rno] = {
                "ratio": ratio,
                "record_date": _date_iso(row.get("nstk_asstd")),
                "listing_date": _date_iso(row.get("nstk_lstprd")),
                "new_shares": new_shares,      # 신주 보통주식수
                "base_shares": base_shares,    # 증자 전 발행주식총수(보통)
            }
        if i % 50 == 0:
            print(f"  fricDecsn {i}/{len(corp_codes)}")
        time.sleep(0.15)
    print(f"  fricDecsn 상세: {len(details)}건")
    return details


# ──────────────────────────────────────────
# 3. 일봉 — pykrx (KRX) 우선, FDR 폴백
# ──────────────────────────────────────────

def get_prices_krx(ticker: str, frm: str, to: str) -> list[dict] | None:
    try:
        from pykrx import stock as krx
        df = krx.get_market_ohlcv(frm.replace("-", ""), to.replace("-", ""), ticker)
        if df is None or df.empty:
            return None
        rows = []
        for dt, r in df.iterrows():
            o, h = int(r.get("시가", 0) or 0), int(r.get("고가", 0) or 0)
            l, c = int(r.get("저가", 0) or 0), int(r.get("종가", 0) or 0)
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:  # 거래정지·결측일 제외
                continue
            rows.append({"d": dt.strftime("%Y-%m-%d"), "o": o, "h": h, "l": l, "c": c})
        return rows or None
    except Exception as e:
        print(f"    pykrx 오류 {ticker}: {e}")
        return None


def calc_mcaps(all_prices: list[dict], idx0: int, det: dict) -> tuple[int | None, int | None]:
    """발표일(d0)·다음 거래일(d1) 종가 기준 시가총액 (억원).

    KRX 일별 시총 API는 로그인(KRX_ID/PW)이 필요해 CI에서 못 쓴다. 대신
    공시상 '증자 전 발행주식총수 + 신주 수' × 매수일 수정주가 종가로 계산한다.
    수정주가는 권리락 배율(구주/(구주+신주))로 나눠져 있어, 증자 후 주식수를
    곱하면 명목 시총과 정확히 일치한다 (이후 추가 감자·분할 시에만 오차).
    """
    base, new = det.get("base_shares"), det.get("new_shares")
    if not base or new is None:
        return None, None
    shares_after = base + new
    d0 = round(shares_after * all_prices[idx0]["c"] / 1e8) if idx0 < len(all_prices) else None
    d1 = round(shares_after * all_prices[idx0 + 1]["c"] / 1e8) if idx0 + 1 < len(all_prices) else None
    return d0, d1


def get_prices_fdr(ticker: str, frm: str, to: str) -> list[dict] | None:
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(ticker, start=frm, end=to)
        if df is None or df.empty:
            return None
        rows = []
        for dt, r in df.iterrows():
            o, h = int(r.get("Open", 0) or 0), int(r.get("High", 0) or 0)
            l, c = int(r.get("Low", 0) or 0), int(r.get("Close", 0) or 0)
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                continue
            rows.append({"d": dt.strftime("%Y-%m-%d"), "o": o, "h": h, "l": l, "c": c})
        return rows or None
    except Exception as e:
        print(f"    FDR 오류 {ticker}: {e}")
        return None


# ──────────────────────────────────────────
# 4. 메인
# ──────────────────────────────────────────

def main():
    if not DART_KEY:
        print("DART_API_KEY 없음 — 수집 불가")
        sys.exit(1)

    print("=== 무상증자 데이터 수집 시작 ===")
    print(f"대상 기간: {BGN_DATE} ~ 오늘\n")

    print("[1/3] DART 공시검색 (무상증자결정)...")
    rows = fetch_disclosures()
    if not rows:
        print("공시 0건 — 수집 실패로 간주")
        sys.exit(1)

    print("\n[2/3] fricDecsn 상세 (배정비율·기준일)...")
    details = fetch_details({r["corp_code"] for r in rows})

    print(f"\n[3/3] 일봉·시가총액 수집 ({len(rows)}건)...")
    today = datetime.now().strftime("%Y-%m-%d")
    price_cache: dict[str, list[dict]] = {}  # ticker → 전체 구간 일봉 (동일종목 복수 이벤트 공유)
    events: list[dict] = []

    # 종목별 필요 구간: 가장 이른 공시 2주 전 ~ 오늘
    earliest: dict[str, str] = {}
    for r in rows:
        t = r["stock_code"].strip()
        d = _date_iso(r["rcept_dt"])
        if t not in earliest or d < earliest[t]:
            earliest[t] = d

    for i, r in enumerate(rows, 1):
        ticker = r["stock_code"].strip()
        name = r.get("corp_name", "")
        disc_date = _date_iso(r["rcept_dt"])
        market = "KOSPI" if r["corp_cls"] == "Y" else "KOSDAQ"
        print(f"  [{i}/{len(rows)}] {ticker} {name} ({disc_date})", end="", flush=True)

        if ticker not in price_cache:
            frm = (datetime.strptime(earliest[ticker], "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")
            # FDR(네이버 일봉·KRX 정규장) 우선 — pykrx는 KRX 로그인 요구로 CI에서 실패함
            prices = get_prices_fdr(ticker, frm, today) or get_prices_krx(ticker, frm, today)
            price_cache[ticker] = prices or []
            time.sleep(0.3)
        all_prices = price_cache[ticker]
        if not all_prices:
            print(" → 시세 없음, 건너뜀")
            continue

        # 공시일 이전 PRE_BARS봉 + 공시일부터 MAX_BARS_AFTER봉으로 절단
        idx0 = next((j for j, p in enumerate(all_prices) if p["d"] >= disc_date), None)
        if idx0 is None:
            print(" → 공시 이후 거래일 없음, 건너뜀")
            continue
        prices = all_prices[max(0, idx0 - PRE_BARS): idx0 + MAX_BARS_AFTER]

        det = details.get(r["rcept_no"], {})
        mcap_d0, mcap_d1 = calc_mcaps(all_prices, idx0, det)
        events.append({
            "ticker": ticker,
            "name": name,
            "market": market,
            "disc_date": disc_date,
            "ratio": det.get("ratio"),
            "record_date": det.get("record_date"),
            "listing_date": det.get("listing_date"),
            "mcap_d0": mcap_d0,  # 발표일 종가 기준 시가총액(억원)
            "mcap_d1": mcap_d1,  # 다음거래일 종가 기준 시가총액(억원)
            "prices": prices,
        })
        ratio_txt = f"1주당 {det['ratio']:g}주" if det.get("ratio") else "비율 ?"
        print(f" → {len(prices)}봉 / {ratio_txt}")

    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "start_date": BGN_DATE,
        "events": events,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\n=== 완료: {len(events)}건 이벤트 → {OUT} ===")


if __name__ == "__main__":
    main()
