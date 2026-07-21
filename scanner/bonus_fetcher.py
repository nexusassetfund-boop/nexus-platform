#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""무상증자 이벤트드리븐 백테스터 데이터 수집 (넥서스 이벤트드리븐 > 무상증자 탭용)

1. DART 공시검색(list.json, 주요사항보고서) → 2023-01-01 이후 '무상증자결정' 공시
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

BGN_DATE = "2023-01-01"          # 백테스트 시작 — 이 날짜 이후 공시만
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
    공시상 발행주식총수 × 매수일 종가(수정주가)로 계산하되, 곱할 주식수는
    이 무증의 권리락이 일봉에 반영됐는지에 따라 달라진다:
    - 신주배정기준일 이후 봉 존재 → 매수일 가격이 권리락 배율(구주/(구주+신주))로
      수정돼 있음 → '증자 후 주식수'를 곱해야 명목 시총과 일치
    - 기준일 미도래(진행 중 이벤트) → 가격이 명목가 그대로 → '증자 전 주식수'
    (권리락일 당일 하루는 근사 오차 가능 — 다음날 자동 갱신에서 정정.
     이후 추가 감자·분할이 있으면 과거 이벤트에 오차 가능.)
    """
    base, new = det.get("base_shares"), det.get("new_shares")
    if not base or new is None:
        return None, None
    record = det.get("record_date")
    adjusted = bool(record) and all_prices[-1]["d"] >= record
    shares = base + new if adjusted else base
    d0 = round(shares * all_prices[idx0]["c"] / 1e8) if idx0 < len(all_prices) else None
    d1 = round(shares * all_prices[idx0 + 1]["c"] / 1e8) if idx0 + 1 < len(all_prices) else None
    return d0, d1


def drop_glitch_bars(rows: list[dict], name: str) -> list[dict]:
    """단일봉 스파이크 제거 — 전일 종가 대비 ±32% 초과 변동(가격제한폭 ±30% 위반 =
    데이터 오류)인데 다음 봉이 전일 수준(±32% 이내)으로 복귀하는 봉을 버린다.
    실제 기준가 변경(감자·병합 등)은 새 가격 수준이 유지되므로 걸리지 않는다.
    (사례: 폰드그룹 2025-12-30 네이버 일봉 +79% 스파이크 후 즉시 복귀)"""
    out = []
    for i, p in enumerate(rows):
        if 0 < i < len(rows) - 1:
            prev_c, next_c = rows[i - 1]["c"], rows[i + 1]["c"]
            if abs(p["c"] / prev_c - 1) > 0.32 and abs(next_c / prev_c - 1) < 0.32:
                print(f"    스파이크 봉 제거 {name} {p['d']}: 종가 {p['c']:,} (전일 {prev_c:,})")
                continue
        out.append(p)
    return out


def fix_unadjusted_rights(prices: list[dict], det: dict, name: str):
    """네이버 일봉의 권리락 미조정 감지·자체 수정.

    일부 종목(예: 지구홀딩스 2023)은 무증 권리락이 수정주가에 반영되지 않아
    기준일 부근에 -33% 초과(가격제한폭상 불가능) 명목 하락 갭이 그대로 남는다.
    배정비율·기준일을 알면 KRX 수정주가 방식대로 갭 이전 봉을 1/(1+비율)로
    정규화하고, 비율 미상이면 시리즈를 신뢰할 수 없어 이벤트를 제외한다.
    반환: (수정된 prices | None(제외), 수정 적용 여부)
    """
    ratio, record = det.get("ratio"), det.get("record_date")
    for i in range(1, len(prices)):
        if prices[i]["c"] / prices[i - 1]["c"] - 1 >= -0.32:
            continue
        near = False
        if record:
            gap_days = abs((datetime.strptime(prices[i]["d"], "%Y-%m-%d")
                            - datetime.strptime(record, "%Y-%m-%d")).days)
            near = gap_days <= 7
        if ratio and near:
            f = 1 + ratio
            for j in range(i):
                for k in ("o", "h", "l", "c"):
                    prices[j][k] = max(1, round(prices[j][k] / f))
            print(f" [권리락 미조정 → 1/{f:g} 자체수정]", end="")
            return prices, True
        print(f" [비정상 하락 갭 {prices[i]['d']} — 원인미상, 제외]", end="")
        return None, False
    return prices, False


def get_naver_shares(ticker: str) -> float | None:
    """네이버 현재 상장주식수 역산 (시가총액[백만원] / 현재가).

    신주 상장 전까지 주식수는 불변이므로 조회 시점(장중/장전)과 무관하게 정확.
    권리락 전 이벤트의 매수일 시총을 '실주식수 × 매수일 종가'로 확정하는 데 쓴다.
    """
    try:
        resp = requests.get(
            f"https://api.finance.naver.com/service/itemSummary.naver?itemcode={ticker}",
            headers={**HEADERS, "Referer": f"https://finance.naver.com/item/main.naver?code={ticker}"},
            timeout=15)
        d = resp.json()
        ms, now = d.get("marketSum"), d.get("now")
        if ms and now:
            return ms * 1e6 / now
    except Exception as e:
        print(f"    네이버 시총 오류 {ticker}: {e}")
    return None


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
    naver_shares_cache: dict[str, float | None] = {}  # ticker → 네이버 실주식수 (진행 중 이벤트용)
    events: list[dict] = []

    # 기존 KV 데이터에서 시가총액 보존 — 매수일 시총은 불변의 과거 사실이므로
    # 이벤트 발생 직후(가격이 명목가일 때) 한 번 확정한 값을 계속 쓴다.
    # 이후 다른 무증·분할로 수정주가가 재조정돼도 저장값은 영향받지 않는다.
    existing_mcap: dict[tuple[str, str], tuple] = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text("utf-8"))
            for e in old.get("events", []):
                existing_mcap[(e["ticker"], e["disc_date"])] = (e.get("mcap_d0"), e.get("mcap_d1"))
            n_kept = sum(1 for v in existing_mcap.values() if v[0] is not None)
            print(f"  기존 데이터 시총 보존: {n_kept}건")
        except Exception:
            pass

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
            price_cache[ticker] = drop_glitch_bars(prices, name) if prices else []
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
        start_i = max(0, idx0 - PRE_BARS)
        prices = [dict(p) for p in all_prices[start_i: idx0 + MAX_BARS_AFTER]]  # 이벤트별 사본
        li0 = idx0 - start_i  # 사본 내 공시일 봉 인덱스

        det = details.get(r["rcept_no"], {})
        prices, series_fixed = fix_unadjusted_rights(prices, det, name)
        if prices is None:
            print(" → 제외")
            continue

        old_d0, old_d1 = existing_mcap.get((ticker, disc_date), (None, None))
        if series_fixed:
            old_d0 = old_d1 = None  # 미조정 시세로 계산·저장됐던 시총 폐기 → 재계산

        # 1순위: 권리락 전(진행 중) 이벤트는 네이버 실주식수 × 매수일 종가로 직접 확정.
        #   저장값보다 우선해 잘못 저장된 값도 자가 치유. 권리락일(기준일 전영업일)
        #   부근에는 일봉이 이미 수정됐을 수 있어 기준일 4일 전까지만 적용(보수적).
        direct_d0 = direct_d1 = None
        record = det.get("record_date")
        if record:
            safe_until = (datetime.strptime(record, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
            if prices[-1]["d"] < safe_until:
                if ticker not in naver_shares_cache:
                    naver_shares_cache[ticker] = get_naver_shares(ticker)
                    time.sleep(0.1)
                sh = naver_shares_cache[ticker]
                if sh:
                    direct_d0 = round(sh * prices[li0]["c"] / 1e8)
                    if li0 + 1 < len(prices):
                        direct_d1 = round(sh * prices[li0 + 1]["c"] / 1e8)

        # 2순위: 저장값(최초 확정 보존) / 3순위: 공시 주식수 × 수정주가 계산 (과거 백필)
        calc_d0, calc_d1 = calc_mcaps(prices, li0, det)
        mcap_d0 = direct_d0 if direct_d0 is not None else (old_d0 if old_d0 is not None else calc_d0)
        mcap_d1 = direct_d1 if direct_d1 is not None else (old_d1 if old_d1 is not None else calc_d1)
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
