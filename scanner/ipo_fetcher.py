#!/usr/bin/env python3
"""IPO 백테스팅 데이터 수집 스크립트 (넥서스 Post IPO 백테스터용)

1. KIND(KRX 공시) → 2023-06-26 이후 상장 종목 목록 + 상장일
2. finuts.co.kr    → 공모가 (상장일 기준 매핑)
3. FinanceDataReader → 일봉 OHLCV
결과: docs/data/ipo_backtest.json (repo 커밋 안 함 — kv_push.py가 Worker KV로 게시)

주의: finuts는 롤링 윈도우라 옛 종목 공모가가 사이트에서 빠진다. 아래 main()의
'기존 데이터에서 공모가 보존' 로직이 그 공백을 메우므로, 실행 전 반드시
기존 KV 데이터를 OUT 경로에 받아둘 것 (ipo-backtest.yml이 수행).
"""

import io, json, time, re, socket, requests
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from pathlib import Path

# FDR 네이버 내부 requests에 timeout이 없어 CI에서 행이 걸릴 수 있음 → 전역 소켓 타임아웃
socket.setdefaulttimeout(15)

IPO_START = "2023-06-26"
OUT = Path(__file__).resolve().parents[1] / "docs" / "data" / "ipo_backtest.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 이 전략은 'IPO 공모청약 신규상장주' 기반이다.
# 이전상장·재상장·분할신설·스팩합병·사명변경 등 공모청약을 거치지 않은 종목은 제외한다.
# (1) KIND 상장방법으로 걸러내고, (2) 아래 티커는 명시적으로 항상 제외한다(안전장치).
EXCLUDE_TICKERS = {
    "0017J0", "0010F0", "0004V0", "493330", "0126Z0", "0120G0", "342870",
    "397810", "462310", "455180", "125020", "101970", "460870", "478560",
    "499790", "398120", "389680", "474610", "489790", "351870", "487570",
    "034230", "323350", "475150", "420570", "452430", "443670", "472850",
    "199550", "066970", "452190", "022100", "109670", "452160", "290560",
    "188260", "221800", "403490", "465770", "146060", "355390", "030190",
    "462520",
    # 2026-08-10 KIND 상장방법 전수 대조로 확인된 비공모 상장 (스팩합병 14 + 분할재상장 1).
    # 같은 날 다른 종목 공모가가 오매칭되어 '공모가 있음 → 공모주' 판정을 통과했던 케이스들.
    "384470",  # 코어라인소프트 (신한제7호스팩 소멸합병)
    "362990",  # 드림인사이트 (스팩합병)
    "451250",  # 삐아 (신영해피투모로우제7호스팩 합병)
    "140430",  # 카티스 (스팩합병)
    "469750",  # 아이비젼웍스 (스팩합병)
    "471820",  # 셀로맥스사이언스 (스팩합병)
    "432980",  # 엠에프씨 (스팩합병)
    "435570",  # 에르코스 (스팩합병)
    "364950",  # 에이아이코리아 (스팩합병)
    "188040",  # 바이오포트 (스팩합병)
    "014950",  # 삼익제약 (하나28호스팩 합병)
    "459550",  # 알트 (IBKS제21호스팩 소멸합병)
    "012210",  # 삼미금속 (스팩합병)
    "288180",  # 케이피항공산업 (엔에이치기업인수목적30호 소멸합병)
    "0218L0",  # 네오뷰 (토비스 인적분할 재상장)
}

# 소스 자동 매칭이 틀린 것으로 확인된 공모가 수동 보정(최우선 적용).
# 아이티센피엔에스: finuts가 같은 날 다른 종목 값(34,000)을 잘못 매칭 →
#   38커뮤니케이션 값 3,000이 시초가(8,940, 공모가 대비 +198%)와 정합.
MANUAL_IPO_PRICES = {
    "232830": 3000,   # 아이티센피엔에스 (코넥스→코스닥 이전상장)
}


# ──────────────────────────────────────────
# 1. KIND에서 상장 종목 목록 (상장일 포함)
# ──────────────────────────────────────────

def get_kind_listing(market_type: str, market_label: str) -> pd.DataFrame:
    """
    market_type: 'stockMkt' (KOSPI) | 'kosdaqMkt' (KOSDAQ)
    """
    url = "http://kind.krx.co.kr/corpgeneral/corpList.do"
    params = {"method": "download", "searchType": "13", "marketType": market_type}
    # KIND는 간헐적으로 타임아웃·연결 거부가 발생 → 재시도 (CI 실패의 주원인)
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            df = pd.read_html(io.StringIO(resp.content.decode("euc-kr")))[0]
            df["Market"] = market_label
            return df
        except Exception as e:
            last_err = e
            print(f"  KIND {market_label} 요청 실패 ({attempt}/3): {e}")
            time.sleep(10 * attempt)
    raise RuntimeError(f"KIND {market_label} 목록 조회 3회 실패") from last_err


def get_listing_df() -> pd.DataFrame:
    kospi  = get_kind_listing("stockMkt",  "KOSPI")
    kosdaq = get_kind_listing("kosdaqMkt", "KOSDAQ")
    df = pd.concat([kospi, kosdaq], ignore_index=True)

    # 열 이름 정규화 (EUC-KR 디코딩 결과에 따라 다를 수 있음)
    col_map = {}
    for c in df.columns:
        lc = c.strip()
        if "회사" in lc or "종목명" in lc:   col_map[c] = "name"
        elif "코드" in lc:                   col_map[c] = "ticker"
        elif "상장일" in lc:                 col_map[c] = "ipo_date"
    df = df.rename(columns=col_map)

    needed = {"name", "ticker", "ipo_date"}
    missing = needed - set(df.columns)
    if missing:
        raise RuntimeError(f"KIND 응답에서 필요 열 누락: {missing}\n실제 열: {list(df.columns)}")

    df["ticker"] = df["ticker"].astype(str).str.zfill(6)
    df["ipo_date"] = pd.to_datetime(df["ipo_date"], errors="coerce")
    df = df[df["ipo_date"] >= IPO_START].copy()
    # KIND 목록에 같은 종목이 중복 수록되는 경우가 있음 (예: 조선내화)
    df = df.drop_duplicates(subset="ticker", keep="first")
    # 리츠·스팩(스팩 자체)은 일반 IPO 전략 대상이 아님 → 이름으로 제외
    name_s = df["name"].astype(str)
    is_excluded = name_s.str.contains("스팩") | name_s.str.endswith("리츠")
    n_excluded = int(is_excluded.sum())
    if n_excluded:
        print(f"  리츠·스팩 제외: {n_excluded}개")
    df = df[~is_excluded]
    # 비공모 상장(이전상장·재상장·스팩합병 등)으로 확정된 티커 명시 제외
    df = df[~df["ticker"].isin(EXCLUDE_TICKERS)]
    df = df.sort_values("ipo_date").reset_index(drop=True)
    print(f"  KIND 상장 목록: {len(df)}개 "
          f"(KOSPI {(df['Market']=='KOSPI').sum()}, "
          f"KOSDAQ {(df['Market']=='KOSDAQ').sum()})")
    return df


def get_listing_methods() -> dict[tuple[str, str], str]:
    """
    KIND 신규상장현황에서 (상장일, 정규화이름) → 상장방법 매핑을 반환.
    상장방법: '신규상장'(공모 IPO) / '이전상장' / '재상장' / ''(스팩합병·분할 등).
    실패 시 빈 dict를 반환하며, 이 경우 상장방법 필터는 건너뛴다(데이터 보존 우선).
    """
    url = "https://kind.krx.co.kr/listinvstg/listingcompany.do"
    headers = {**HEADERS,
               "Referer": url + "?method=searchListingTypeMain"}
    items = [
        ("method", "searchListingTypeSub"), ("currentPageSize", "5000"),
        ("pageIndex", "1"), ("forward", "listingtype_sub"),
        ("listTypeArrStr", "01,02,03,04,05"), ("choicTypeArrStr", "02"),
        ("secuGrpArrStr", "ST|FS,MF|SC|RT|IF,DR"), ("marketType", ""),
        ("country", ""), ("fromDate", IPO_START),
        ("toDate", datetime.now().strftime("%Y-%m-%d")),
    ]
    for t in ["01", "02", "03", "04", "05"]:
        items.append(("listTypeArr", t))
    for s in ["0", "ST|FS", "MF|SC|RT|IF", "DR"]:
        items.append(("secuGrpArr", s))
    for c in ["01", "02", "03", "04", "05", "06"]:
        items.append(("choicTypeArr", c))

    methods: dict[tuple[str, str], str] = {}
    try:
        html = requests.post(url, data=items, headers=headers, timeout=40).text
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            if "<td" not in tr:
                continue
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip()
                     for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) >= 3:
                methods[(cells[1], _norm_name(cells[0]))] = cells[2]
        print(f"  KIND 상장방법: {len(methods)}개 조회")
    except Exception as e:
        print(f"  KIND 상장방법 조회 실패(필터 생략): {e}")
    return methods


def _norm_name(s: str) -> str:
    return re.sub(r"[\s\-_·()㈜]", "", str(s)).lower()


def is_public_offering(ticker: str, name: str, ipo_date: str,
                       ipo_price, methods: dict) -> bool:
    """
    공모청약 신규상장주인지 판정. KIND 상장방법이 공모가 매칭보다 우선한다 —
    공모가는 같은 날 다른 종목 값이 오매칭될 수 있어서(케이피항공산업 스팩합병 등
    15종목이 이 경로로 유니버스에 섞였던 사고, 2026-08-10 확인) 단독 근거로 쓰지 않는다.
    - KIND '신규상장' → True
    - KIND '이전상장' → 공모가가 확인될 때만 True (코넥스→코스닥 공모 이전상장)
    - KIND 그 외('' = 스팩합병·분할, '재상장' 등) → 공모가가 매칭돼 있어도 False
    - KIND에서 방법을 못 찾은 종목 → 공모가 있으면 True
    - 상장방법 조회 자체가 실패(빈 dict) → 보수적으로 True (데이터 보존)
    """
    if methods:
        m = methods.get((ipo_date, _norm_name(name)))
        if m is None:
            # 이름 표기 차이 → 상장일 기준 부분일치로 재시도
            nn = _norm_name(name)
            for (dt, wn), mt in methods.items():
                if dt == ipo_date and nn and (nn in wn or wn in nn):
                    m = mt
                    break
        if m == "신규상장":
            return True
        if m == "이전상장":
            return bool(ipo_price)
        if m is not None:
            return False
    if ipo_price:
        return True
    return not methods


# ──────────────────────────────────────────
# 2. 공모가 — finuts.co.kr API
# ──────────────────────────────────────────

def get_finuts_ipo_prices() -> dict[str, int]:
    """
    finuts.co.kr의 ipoListQuery.php API에서
    {상장일: 공모가} 딕셔너리를 반환.
    KIND 데이터와 상장일로 매핑하여 종목코드별 공모가를 확인.
    """
    url = "https://www.finuts.co.kr/html/task/ipo/ipoListQuery.php"
    headers = {
        **HEADERS,
        "Referer": "https://www.finuts.co.kr/html/ipo/ipoList.php",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
    }
    # {상장일: [(회사명, 공모가), ...]}
    date_map: dict[str, list[tuple[str, int]]] = {}
    try:
        resp = requests.post(url, data={"active": "ipo-011", "search_text": ""},
                             headers=headers, timeout=30)
        resp.raise_for_status()
        items = resp.json().get("data", [])
        for item in items:
            ipo_date = item.get("IPO_DATE", "")
            pss_prc  = item.get("PSS_PRC", "")
            ent_nm   = item.get("ENT_NM", "")
            if ipo_date in ("9999-99-99", "", None):
                continue
            if not pss_prc:
                continue
            try:
                price = int(str(pss_prc).replace(",", ""))
            except ValueError:
                continue
            if price <= 0:
                continue
            date_map.setdefault(ipo_date, []).append((ent_nm, price))
        print(f"  finuts 공모가: {sum(len(v) for v in date_map.values())}개 항목 "
              f"({len(date_map)}개 날짜)")
    except Exception as e:
        print(f"  finuts API 실패: {e}")
    return date_map


def get_38_ipo_data() -> dict[str, list[tuple[str, int, int]]]:
    """
    38커뮤니케이션 신규상장 목록에서 {상장일: [(회사명, 공모가, 명목시초가), ...]}을 반환.
    finuts는 롤링 윈도우라 옛 종목이 빠지지만 38은 과거까지 커버 → 보완 소스.
    명목 시초가는 수정주가 보정용(수정 시초가 대비 배율 산출).
    (컬럼: 종목명 | 상장일 | 현재가 | 등락 | 공모가 | 공모가대비 | 시초가 | ... )
    """
    date_map: dict[str, list[tuple[str, int, int]]] = {}
    try:
        for page in range(1, 26):
            url = f"http://www.38.co.kr/html/fund/index.htm?o=nw&page={page}"
            html = requests.get(url, headers=HEADERS, timeout=20)\
                .content.decode("euc-kr", "replace")
            got = 0
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
                tds = [re.sub(r"\s+", " ",
                              re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ")
                              ).strip()
                       for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(tds) != 10:
                    continue
                m = re.match(r"(\d{4})/(\d\d)/(\d\d)", tds[1])
                if not m:
                    continue
                digits = re.sub(r"[^\d]", "", tds[4])
                if not digits:
                    continue
                price = int(digits)
                open_digits = re.sub(r"[^\d]", "", tds[6])
                open_px = int(open_digits) if open_digits else 0
                if price > 0:
                    d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                    date_map.setdefault(d, []).append((tds[0], price, open_px))
                    got += 1
            if got == 0:
                break
        print(f"  38 공모가: {sum(len(v) for v in date_map.values())}개 항목 "
              f"({len(date_map)}개 날짜)")
    except Exception as e:
        print(f"  38 조회 실패: {e}")
    return date_map


def find_38(date_map_38: dict, ipo_date: str, name: str):
    """38 date_map({상장일: [(회사명, 공모가, 명목시초가)]})에서 종목 레코드를 찾아 반환."""
    candidates = date_map_38.get(ipo_date, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    nn = _norm_name(name)
    for c in candidates:
        if _norm_name(c[0]) == nn:
            return c
    for c in candidates:
        if _norm_name(c[0]) in nn or nn in _norm_name(c[0]):
            return c
    return None


def match_ipo_price(date_map: dict, ipo_date: str, name: str) -> int | None:
    """
    공모가 date_map({상장일: [(회사명, 공모가, ...)]})과 (상장일, 회사명)으로 공모가 매핑.
    같은 날 1개면 바로 반환, 여러 개면 이름 유사도로 매핑.
    """
    candidates = date_map.get(ipo_date, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]

    # 이름 정규화 후 비교 (공백·특수문자 제거, 소문자)
    def normalize(s: str) -> str:
        return re.sub(r"[\s\-_·]", "", s).lower()

    norm_name = normalize(name)
    # 튜플 길이가 소스마다 다름: finuts (회사명, 공모가) / 38 (회사명, 공모가, 명목시초가)
    for cand_name, price, *_ in candidates:
        if normalize(cand_name) == norm_name:
            return price
    # 부분 일치
    for cand_name, price, *_ in candidates:
        if normalize(cand_name) in norm_name or norm_name in normalize(cand_name):
            return price
    # 매핑 실패 시 None
    return None


# ──────────────────────────────────────────
# 3. 일봉 OHLCV — FinanceDataReader
# ──────────────────────────────────────────

def last_closed_date() -> str:
    """마감이 확정된 마지막 날짜 — 16시 이전 실행이면 당일은 미마감이라 어제까지.

    크론은 08:00 KST지만 GitHub 지연이 15~40분 상습이라 09:00 개장 후에 도는 일이
    잦다. 그때 FDR이 주는 당일 봉은 장중 스냅샷이라, 그대로 저장하면 백테스터가
    미완성 종가로 매도를 확정해버린다(2026-08-04 사례). newhigh_fetcher와 동일 규칙.
    """
    now = datetime.now()  # 워크플로가 TZ=Asia/Seoul 설정
    return (now if now.hour >= 16 else now - timedelta(days=1)).strftime("%Y-%m-%d")


def get_prices(ticker: str, start_date: str) -> list[dict] | None:
    try:
        df = fdr.DataReader(ticker, start=start_date)
        if df is None or df.empty:
            return None
        cutoff = last_closed_date()
        rows = []
        for dt, r in df.iterrows():
            if dt.strftime("%Y-%m-%d") > cutoff:  # 미마감 당일 봉 제외
                continue
            o = int(r.get("Open", 0) or 0)
            h = int(r.get("High", 0) or 0)
            l = int(r.get("Low", 0) or 0)
            c = int(r.get("Close", 0) or 0)
            # 거래정지·결측일은 OHLC 중 일부가 0으로 들어옴(체결 불가) → 제외.
            # 정상 거래일은 o/h/l/c 모두 양수여야 한다.
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                continue
            rows.append({"d": dt.strftime("%Y-%m-%d"), "o": o, "h": h, "l": l, "c": c})
        return rows or None
    except Exception as e:
        print(f"    가격 오류 {ticker}: {e}")
        return None


# ──────────────────────────────────────────
# 4. 메인
# ──────────────────────────────────────────

def main():
    print("=== IPO 데이터 수집 시작 ===")
    print(f"대상 기간: {IPO_START} ~ 오늘\n")

    # ── 상장 목록 ──
    print("[1/3] KIND 상장 목록 조회...")
    listing = get_listing_df()

    # ── 상장방법 (공모 IPO 판별용) ──
    print("\n[1.5/3] KIND 상장방법 조회...")
    methods = get_listing_methods()

    # ── 공모가 ── (finuts + 38커뮤니케이션 이중 소스)
    print("\n[2/3] 공모가 수집 (finuts.co.kr + 38.co.kr)...")
    date_map = get_finuts_ipo_prices()
    date_map_38 = get_38_ipo_data()

    # 기존 데이터에서 공모가 보존
    existing: dict[str, dict] = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            for s in old.get("stocks", []):
                existing[s["ticker"]] = s
            prev_with_price = sum(1 for s in old["stocks"] if s.get("ipo_price"))
            print(f"  기존 데이터 {len(existing)}개 (공모가 보유 {prev_with_price}개) 로드")
        except Exception:
            pass

    # ── 일봉 수집 ──
    print(f"\n[3/3] 일봉 OHLCV 수집 ({len(listing)}개 종목)...")
    stocks: list[dict] = []

    for i, row in listing.iterrows():
        ticker   = row["ticker"]
        name     = str(row["name"])
        market   = row["Market"]
        ipo_date = row["ipo_date"].strftime("%Y-%m-%d")

        print(f"  [{i+1}/{len(listing)}] {ticker} {name} ({ipo_date})", end="", flush=True)

        prices = get_prices(ticker, ipo_date)
        if not prices:
            # 일시적 FDR 실패로 오늘 시세를 못 받은 경우, 기존에 정상 수집돼 있던
            # 종목이면 전체 레코드를 그대로 이월해 이력 소실을 막는다.
            # (KV가 ipo_backtest.json의 유일 저장소 — 스킵하면 과거 가격까지 영구 소실)
            prev = existing.get(ticker)
            if prev and prev.get("prices"):
                stocks.append({**prev, "stale": True})
                print(" → 데이터 없음, 기존 레코드 이월(stale)")
            else:
                print(" → 데이터 없음, 건너뜀")
            continue

        listing_open = prices[0]["o"]
        # 공모가 우선순위: 수동보정 > finuts > 38 > 기존값
        ipo_price = (
            MANUAL_IPO_PRICES.get(ticker)
            or match_ipo_price(date_map, ipo_date, name)
            or match_ipo_price(date_map_38, ipo_date, name)
            or existing.get(ticker, {}).get("ipo_price")
        )
        rec38 = find_38(date_map_38, ipo_date, name)

        # 공모가-시초가 정합성: 2023-06-26 이후 상장일 시초가는 공모가의 60~400%로
        # 제한된다. 밴드 밖이면 오매칭(스팩합병 등 공모가가 없어야 할 종목에 같은 날
        # 다른 종목 값이 붙은 것) → 폐기하고 KIND 상장방법만으로 판정. 수정주가 배율은
        # 38의 명목 시초가로 제거(없으면 listing_open으로 근사 — 수동보정 종목은 신뢰).
        if ipo_price and ticker not in MANUAL_IPO_PRICES:
            nominal_open = rec38[2] if rec38 and rec38[2] > 0 else listing_open
            band = nominal_open / ipo_price
            if band > 4.05 or band < 0.55:
                print(f" [공모가 {ipo_price:,} 폐기 — 시초가 {nominal_open:,} 대비 "
                      f"{band:.1f}배, 60~400% 밴드 밖]", end="")
                ipo_price = None

        # 공모청약 신규상장주가 아니면(이전상장·재상장·스팩합병 등) 제외
        if not is_public_offering(ticker, name, ipo_date, ipo_price, methods):
            print(" → 비공모 상장(제외)")
            continue

        # 수정주가 보정: 일봉이 수정주가(무상증자·분할)이므로, 명목 공모가를
        # 동일 배율(수정시초/명목시초)로 조정한 값을 공모가 기준 백테스트용으로 저장.
        ipo_price_adj = ipo_price
        if ipo_price and rec38 and rec38[2] > 0:
            ratio = listing_open / rec38[2]
            if ratio < 0.95:
                ipo_price_adj = round(ipo_price * ratio)

        stocks.append({
            "ticker":        ticker,
            "name":          name,
            "market":        market,
            "ipo_date":      ipo_date,
            "ipo_price":     ipo_price,
            "ipo_price_adj": ipo_price_adj,
            "listing_open":  listing_open,
            "prices":        prices,
        })
        flag = f"공모가 {ipo_price:,}" if ipo_price else "공모가 ?"
        print(f" → {len(prices)}일 / {flag} / 시초가 {listing_open:,}")
        time.sleep(0.2)

    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stocks": stocks,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    has_price = sum(1 for s in stocks if s["ipo_price"])
    print(f"\n=== 완료: {len(stocks)}개 종목 (공모가 {has_price}개) → {OUT} ===")


if __name__ == "__main__":
    main()
