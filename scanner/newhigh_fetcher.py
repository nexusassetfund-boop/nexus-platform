# -*- coding: utf-8 -*-
"""52주 신고가 주도주 이벤트드리븐 데이터 수집 (넥서스 이벤트드리븐 > 52주 신고가 탭용)

데이터: marcap (KRX 원본 전종목 일별 시세·시가총액·거래대금, FinanceData 공개 배포)
  https://github.com/FinanceData/marcap — 히스토리 본체. 다만 갱신이 1거래일+ 지연되고
  주말에 멈추므로, 마지막 날짜 이후 구간은 pykrx(KRX_ID/PW 로그인, pullback_screener와
  동일 경로)로 일별 전종목 시세·시총을 보충해 직전 거래일 종가까지 최신화한다.
  pykrx 보충 실패 시엔 marcap만으로 진행 (기존 동작 = fail-soft).

원본 전략(검증 완료: 2021-01~2026-07, 29건, 승률 82.8%, 평균 +27.1%)의 계산부만 이식:
  KOSPI 주봉에서 ①주간 거래대금 순위 ②주 마지막 거래일 시총 순위 ③직전 51주 고가 최대값을
  구해 "주봉 종가 > 직전 51주 고가"인 후보 이벤트를 산출한다.
  프론트가 매수 필터(거래대금·시총 순위)를 조절할 수 있도록 검증 전략(10위/30~50위)보다
  넓게(거래대금 ≤30위, 시총 ≤200위) 담고, 매매 시뮬레이션은 전부 클라이언트에서 수행한다.

산출: docs/data/newhigh_backtest.json (repo 커밋 안 함 — kv_push.py가 KV 게시)
  events[] = 후보 이벤트(코드·이름·진입일·진입가·순위) — 시뮬레이션은 안 함
  prices{} = 코드 → [[날짜, 수정고가, 수정저가, 수정종가], ...] — 종목당 1회(이벤트 간 중복 제거)

부산물: docs/data/newhigh_candidates.json (전략실 > 신고가 후보 탭용)
  위 이벤트가 "주봉 종가 돌파 + 대형주 순위"라는 좁은 사후 전략인 것과 달리, 이쪽은
  52주 신고가 문턱에 닿았던 종목을 일정 기간 추적하는 사전 관찰 리스트다. 이벤트 경로가
  KOSPI만 보는 것과 달리 코스닥을 포함하며, 같은 marcap DataFrame을 재사용하므로
  추가 수집이 없다. 상태 파일을 두지 않는다 — 매 실행마다 전체 이력에서 다시 계산하는
  스테이트리스 방식이라 재현·백필·백테스트가 가능하다.
"""
import json
import os
import socket
import sys
import urllib.request

socket.setdefaulttimeout(60)  # marcap parquet 다운로드 행 방지 (ipo_fetcher와 동일 패턴, 대용량이라 여유 있게)
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "marcap_cache"
OUT = ROOT / "docs" / "data" / "newhigh_backtest.json"
OUT_CAND = ROOT / "docs" / "data" / "newhigh_candidates.json"
SECTOR_CACHE = ROOT / "docs" / "data" / "sector_cache.json"

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).date()
START_SIGNAL = pd.Timestamp("2021-01-01")   # 백테스트 시작 — 이 날짜 이후 신호만
DATA_START_YEAR = 2020                       # 51주 룩백 확보용 여유
MARKETS = ("KOSPI", "KOSDAQ")                # 수집 대상 — 주봉 이벤트는 KOSPI만 쓴다
AMT_RANK_MAX = 30                            # 후보 상한 — 검증 전략(10위)보다 넓게 (프론트 필터용)
CAP_RANK_MAX = 200                           # 후보 상한 — 검증 전략(30~50위)보다 넓게
MAX_BARS_AFTER = 160                         # 마지막 신호 이후 최대 봉 수 (~7.5개월, 만기 6개월 + 여유)

# ── 신고가 후보 파라미터 ──────────────────────────────────────────
# 상태 임계(%)는 스탁이지(stockeasy.intellio.kr) 화면을 실측해 맞췄다. gap 정의도 동일하게
# "(직전 52주 고가 − 현재가) / 현재가" — 분모가 현재가라 넥서스 scan.json의 off_high
# (분모가 고가)와는 다른 값이 나온다. 혼동 주의.
HIGH52_BARS = 250        # 52주 ≈ 250거래일. 당일 제외(shift 1)한 직전 고가를 기준가로 쓴다
HIGH52_MIN_BARS = 120    # 상장 6개월 미만은 기준가를 못 만든다 — 후보에서 자동 제외
GAP_IMMINENT = 3.0       # 이하 = 임박
GAP_NEAR = 7.0           # 이하 = 근접
GAP_WATCH = 12.0         # 이하 = 관찰. 초과하면 워치리스트에서 탈락 (스탁이지 "후보 범위 12% 이내")
WATCH_WINDOW = 60        # 등재 후 추적하는 영업일 수 (스탁이지 "최근 60영업일 기준"과 동일)
MIN_MARCAP = 2_000e8     # 시총 하한 2,000억원 (marcap의 Marcap은 원 단위)
# 유동성 하한 — 시총만으로는 지주사·리츠·인프라펀드처럼 '덩치는 큰데 거래가 없는' 종목이
# 남는다. 당일 거래대금이 아니라 20거래일 평균으로 재는 게 중요하다: 당일로 걸면 오늘만
# 조용했던 종목이 탈락하고, 평소 거래가 없다가 오늘 반짝한 종목이 통과해 정확히 거꾸로 걸린다.
MIN_AMOUNT_AVG20 = 10e8  # 20거래일 평균 거래대금 10억원
ENTER_GAP = GAP_IMMINENT  # 이 거리 안에 한 번이라도 들어오면 워치리스트 등재
FRESH_MAX_BREAKOUT_DAYS = 1   # 신선 후보: 창 안 돌파일이 이 이하
FRESH_MAX_GAP = 5.0           # 신선 후보: 현재 gap 상한
FRESH_MIN_AMT_RATIO = 1.2     # 신선 후보: 거래대금 20일 평균 대비 하한
SPARK_BARS = 60          # 미니 차트에 실을 종가 개수


def download_marcap() -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    years = range(DATA_START_YEAR, TODAY.year + 1)
    for y in years:
        path = CACHE / f"marcap-{y}.parquet"
        # 과거 연도는 캐시 재사용, 당해/전년도는 항상 갱신 (CI는 캐시 없음 → 전부 다운로드)
        if path.exists() and y < TODAY.year - 1:
            continue
        url = f"https://github.com/FinanceData/marcap/raw/master/data/marcap-{y}.parquet"
        print(f"  다운로드: marcap-{y}.parquet")
        urllib.request.urlretrieve(url, path)
    cols = ["Code", "Name", "Date", "High", "Low", "Close",
            "Changes", "Volume", "Amount", "Marcap", "Market"]
    df = pd.concat((pd.read_parquet(CACHE / f"marcap-{y}.parquet", columns=cols)
                    for y in years), ignore_index=True)
    # 코스닥까지 담아 두고, 주봉 이벤트 경로에서만 KOSPI로 좁힌다 (검증 전략 불변).
    # 신고가 후보 리스트는 코스닥을 포함한다.
    df = df[df["Market"].isin(MARKETS)].copy()
    # 우선주 제외 (종목코드 끝자리 0 = 보통주) — 삼성전자우 등이 거래대금·시총 순위를
    # 차지해 실질 편입 슬롯을 줄이는 문제 방지. 순위 산정·후보 모두에서 뺀다 (사용자 요청 2026-07-21)
    df = df[df["Code"].str.endswith("0")]
    df["Date"] = pd.to_datetime(df["Date"])
    df.sort_values(["Code", "Date"], inplace=True)
    return df


def _pykrx_stock():
    """pykrx 지연 import + 재시도 (pullback_screener와 동일 패턴).
    import 시점 KRX 로그인이 간헐 차단되면 재시도 후 최후엔 무로그인 폴백."""
    import time
    for attempt in range(3):
        try:
            from pykrx import stock
            return stock
        except Exception as e:
            print(f"  pykrx import 실패(시도 {attempt + 1}): {e}")
            time.sleep(3)
    os.environ.pop("KRX_ID", None)
    os.environ.pop("KRX_PW", None)
    from pykrx import stock
    return stock


def supplement_krx(df: pd.DataFrame) -> pd.DataFrame:
    """marcap 지연 구간(마지막 날짜 다음날 ~ 직전 거래일)을 KRX 일별 시세로 보충.
    16시 이전 실행이면 당일은 미마감이므로 어제까지만. 어떤 실패든 marcap만으로 진행."""
    try:
        stock = _pykrx_stock()
    except Exception as e:
        print(f"  pykrx 사용 불가, marcap만 사용: {e}")
        return df
    names = df.sort_values("Date").groupby("Code")["Name"].last()
    end = TODAY if datetime.now(KST).hour >= 16 else TODAY - timedelta(days=1)
    added = []
    d = df["Date"].max().date() + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            for mkt in MARKETS:
                try:
                    ohlcv = stock.get_market_ohlcv_by_ticker(d.strftime("%Y%m%d"), market=mkt)
                    # 휴장일은 빈 프레임 또는 거래대금 0으로 온다
                    if ohlcv is not None and len(ohlcv) > 0 and ohlcv["거래대금"].sum() > 0:
                        cap = stock.get_market_cap_by_ticker(d.strftime("%Y%m%d"), market=mkt)["시가총액"]
                        # 기준가 = 종가/(1+등락률) — 액면분할 등 수정계수가 marcap의 Close-Changes와 동일하게 복원되도록
                        base = ohlcv["종가"] / (1 + ohlcv["등락률"] / 100)
                        part = pd.DataFrame({
                            "Code": ohlcv.index.astype(str),
                            "Name": [names.get(c, "") for c in ohlcv.index.astype(str)],
                            "Date": pd.Timestamp(d),
                            "High": ohlcv["고가"].values,
                            "Low": ohlcv["저가"].values,
                            "Close": ohlcv["종가"].values,
                            "Changes": (ohlcv["종가"] - base).values,
                            "Volume": ohlcv["거래량"].values,
                            "Amount": ohlcv["거래대금"].values,
                            "Marcap": cap.reindex(ohlcv.index).values,
                            "Market": mkt,
                        })
                        # marcap과 동일 필터: 보통주(코드 끝 0)만, 신규상장 등 이름 미상은 제외
                        part = part[part["Code"].str.endswith("0") & (part["Name"] != "")]
                        added.append(part)
                        print(f"  KRX 보충: {d} {mkt} {len(part)}종목")
                except Exception as e:
                    print(f"  KRX 보충 실패({d} {mkt}), 건너뜀: {e}")
        d += timedelta(days=1)
    if not added:
        return df
    df = pd.concat([df] + added, ignore_index=True)
    df.sort_values(["Code", "Date"], inplace=True)
    return df


def classify(gap: float, touched: bool) -> str:
    """gap(%)과 장중 터치 여부로 상태를 정한다. 임계값은 스탁이지 화면 실측치.
    터치 후 밀림은 '고가는 기준가에 닿았는데 종가가 아래'라 gap 구간과 별개로 먼저 본다."""
    if gap <= 0:
        return "breaking"
    if touched:
        return "touched_failed"
    if gap <= GAP_IMMINENT:
        return "imminent"
    if gap <= GAP_NEAR:
        return "near"
    if gap <= GAP_WATCH:
        return "watch"
    return ""


def _wide(df: pd.DataFrame, col: str, like: pd.DataFrame | None = None) -> pd.DataFrame:
    """(날짜 × 종목) 행렬. 거래정지일은 NaN으로 빼서 rolling 통계를 오염시키지 않는다.

    pivot_table은 값이 전부 비는 날짜·종목을 통째로 떨어뜨려 컬럼별로 축이 어긋난다
    (예: 시총이 없는 날). like를 주면 그 축에 맞춰 정렬해 .loc[날짜]가 항상 통하게 한다.
    """
    w = df.loc[~df["halted"]].pivot_table(index="Date", columns="Code", values=col)
    return w if like is None else w.reindex(index=like.index, columns=like.columns)


def build_candidates(df_all: pd.DataFrame, last_date: pd.Timestamp) -> dict:
    """52주 신고가 문턱에 닿았던 종목의 관찰 리스트를 만든다.

    상태 파일 없이 매번 전체 이력에서 다시 계산한다. 등재 규칙은
    "최근 WATCH_WINDOW 영업일 중 하루라도 gap ≤ ENTER_GAP" 이고,
    현재 gap이 GAP_WATCH를 넘으면 탈락한다.
    """
    # 250일 룩백 + 60일 창 + 여유. 전 구간을 행렬로 펴면 메모리가 커서 뒤쪽만 자른다.
    need = HIGH52_BARS + WATCH_WINDOW + 20
    dates = np.sort(df_all.loc[~df_all["halted"], "Date"].unique())[-need:]
    d = df_all[df_all["Date"] >= dates[0]]

    close = _wide(d, "adjClose")            # 축의 기준 — 나머지는 여기에 맞춘다
    high = _wide(d, "adjHigh", close)
    amount, raw_close = _wide(d, "Amount", close), _wide(d, "Close", close)
    marcap = _wide(d, "Marcap", close)
    # 기준가 = 당일을 뺀 직전 52주 고가. shift(1)을 빼면 gap이 항상 0 이하가 되어 무의미해진다.
    base = high.rolling(HIGH52_BARS, min_periods=HIGH52_MIN_BARS).max().shift(1)

    gap = (base - close) / close * 100
    touched = (high >= base) & (close < base)
    # 당일을 뺀 직전 20거래일 평균(shift 1). 당일을 포함하면 하루 급등이 스스로 평균을 끌어올려
    # 유동성 하한을 통과시키고, 배수도 그만큼 눌려 '평소의 몇 배인가'를 과소평가한다.
    avg20 = amount.rolling(20, min_periods=10).mean().shift(1)
    amt_ratio = amount / avg20

    win_gap = gap.tail(WATCH_WINDOW)
    entered = (win_gap <= ENTER_GAP).any()                    # 창 안에서 한 번이라도 문턱 도달
    seen_days = (win_gap <= GAP_WATCH).sum()                  # 후보로 노출된 일수
    breakout_days = (win_gap <= 0).sum()                      # 종가 기준 돌파 성공 일수

    today = gap.index[-1]
    cur_gap, cur_close = gap.loc[today], close.loc[today]
    cur_cap, cur_avg20 = marcap.loc[today], avg20.loc[today]
    # 규모·유동성 하한. 둘 다 값이 없으면 통과시키지 않는다 — 조용히 섞이면 하한이 무의미해진다.
    codes = [c for c in gap.columns
             if entered.get(c, False) and pd.notna(cur_gap[c]) and cur_gap[c] <= GAP_WATCH
             and pd.notna(cur_cap.get(c)) and float(cur_cap[c]) >= MIN_MARCAP
             and pd.notna(cur_avg20.get(c)) and float(cur_avg20[c]) >= MIN_AMOUNT_AVG20]

    # RS: 전 종목 수익률 백분위 (0~100). 같은 행렬을 재사용하므로 추가 조회가 없다.
    def rs_of(bars: int) -> pd.Series:
        if len(close) <= bars:
            return pd.Series(dtype=float)
        ret = close.iloc[-1] / close.iloc[-1 - bars] - 1
        return (ret.rank(pct=True) * 100).round()

    rs_12m, rs_1m = rs_of(HIGH52_BARS), rs_of(20)
    names = (df_all.sort_values("Date").groupby("Code")[["Name", "Market"]].last())
    sectors = {}
    if SECTOR_CACHE.exists():
        try:
            sectors = json.loads(SECTOR_CACHE.read_text(encoding="utf-8")).get("sectors", {})
        except Exception as e:
            print(f"  섹터 캐시 읽기 실패, 섹터 없이 진행: {e}")

    rows, history = [], {}
    for c in codes:
        g_now = float(cur_gap[c])
        status = classify(g_now, bool(touched.loc[today, c]))
        if not status:
            continue
        px = raw_close[c].dropna()
        # 등락률은 수정주가로 — 원시 종가끼리 나누면 액면분할일에 -50% 같은 값이 찍힌다
        adj_px = close[c].dropna()
        chg = round(float(adj_px.iloc[-1] / adj_px.iloc[-2] - 1) * 100, 2) if len(adj_px) >= 2 else 0.0
        ratio = amt_ratio.loc[today, c]
        n_break = int(breakout_days.get(c, 0))
        rows.append({
            "code": c,
            "name": str(names["Name"].get(c, c)),
            "market": str(names["Market"].get(c, "")),
            "sector": sectors.get(c, ""),
            "status": status,
            "gap": round(g_now, 2),
            "price": int(px.iloc[-1]),
            "high52": int(round(float(base.loc[today, c]) / float(cur_close[c]) * px.iloc[-1])),
            "change": chg,
            "marcap_eok": round(float(cur_cap[c]) / 1e8),
            # 소액 종목은 정수로 반올림하면 0억이 되어 데이터 누락처럼 보인다 → 10억 미만은 소수 1자리
            "amount_eok": (lambda v: round(v, 1) if v < 10 else round(v))(
                float(amount.loc[today, c]) / 1e8),
            "amt_vs20": round(float(ratio), 1) if pd.notna(ratio) else None,
            "rs": None if rs_12m.empty or pd.isna(rs_12m.get(c)) else int(rs_12m[c]),
            "rs_1m": None if rs_1m.empty or pd.isna(rs_1m.get(c)) else int(rs_1m[c]),
            "seen_days": int(seen_days.get(c, 0)),
            "breakout_days": n_break,
            # 신선 후보 = 아직 많이 안 뚫렸는데 거래대금이 붙기 시작한 종목 (스탁이지 동일 조건)
            "fresh": (n_break <= FRESH_MAX_BREAKOUT_DAYS and g_now <= FRESH_MAX_GAP
                      and pd.notna(ratio) and float(ratio) >= FRESH_MIN_AMT_RATIO),
            "spark": [round(float(v), 1) for v in close[c].tail(SPARK_BARS).dropna()],
        })
        # 일자별 이력 — 후보로 노출된 날만 (전체 60일을 담으면 payload가 5배가 된다)
        hist = []
        for dt_ in win_gap.index:
            gv = win_gap.loc[dt_, c]
            if pd.isna(gv) or gv > GAP_WATCH:
                continue
            st = classify(float(gv), bool(touched.loc[dt_, c]))
            if st:
                hist.append([dt_.strftime("%Y-%m-%d"), st, round(float(gv), 2),
                             int(raw_close.loc[dt_, c]) if pd.notna(raw_close.loc[dt_, c]) else None])
        history[c] = hist

    rows.sort(key=lambda r: r["gap"])
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in ("breaking", "imminent", "near", "watch", "touched_failed")}
    counts["fresh"] = sum(1 for r in rows if r["fresh"])
    out = {
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "data_last_date": str(pd.Timestamp(today).date()),
        "markets": list(MARKETS),
        "params": {
            "high52_bars": HIGH52_BARS, "enter_gap": ENTER_GAP, "watch_gap": GAP_WATCH,
            "window": WATCH_WINDOW, "imminent": GAP_IMMINENT, "near": GAP_NEAR,
            "min_marcap_eok": round(MIN_MARCAP / 1e8),
            "min_amount_avg20_eok": round(MIN_AMOUNT_AVG20 / 1e8),
        },
        "counts": counts,
        "candidates": rows,
        "history": history,
    }
    OUT_CAND.parent.mkdir(parents=True, exist_ok=True)
    OUT_CAND.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    print(f"  신고가 후보: {len(rows)}종목 {counts} → {OUT_CAND} "
          f"({OUT_CAND.stat().st_size / 1024:.0f}KB)")
    return out


def complete_week_of(last_date: pd.Timestamp):
    """완결 주(금요일) 판정 — 원본 로직. 금요일 데이터가 아직 없으면 직전 주까지만."""
    last_friday = TODAY - timedelta(days=(TODAY.weekday() - 4) % 7)
    if last_date.date() >= last_friday:
        return pd.Timestamp(last_friday)
    if (TODAY - last_friday).days >= 3:
        # 월요일 이후에도 금요일 데이터가 없으면 금요일 휴장으로 간주 (목요일 마감 주)
        return pd.Timestamp(last_friday)
    return pd.Timestamp(last_friday) - pd.Timedelta(weeks=1)


def add_adjusted(df: pd.DataFrame) -> pd.DataFrame:
    """수정주가 복원 (기준가 = Close - Changes) — 원본 로직 그대로.
    종목별 groupby라 시장을 나누기 전에 한 번만 돌려도 결과가 같다."""
    g = df.groupby("Code", sort=False)
    prev_close = g["Close"].shift(1)
    factor = ((df["Close"] - df["Changes"]) / prev_close).fillna(1.0)
    factor = factor.where((factor > 0.005) & (factor < 200), 1.0)
    factor = factor.where(prev_close > 0, 1.0).replace([np.inf, -np.inf], 1.0)
    df["_f"] = factor
    rev_cum = df.iloc[::-1].groupby("Code", sort=False)["_f"].cumprod().iloc[::-1]
    adjmult = rev_cum / df["_f"]  # 최신일 기준 스케일 (오늘 가격 단위)
    for c in ["High", "Low", "Close"]:
        df["adj" + c] = df[c] * adjmult
    df["halted"] = (df["Volume"] <= 0) | (df["High"] <= 0)
    return df


def main():
    print("=== 52주 신고가 데이터 수집 시작 (marcap) ===")
    df_all = add_adjusted(supplement_krx(download_marcap()))
    last_date = df_all["Date"].max()
    for mkt in MARKETS:
        print(f"  데이터: ~ {last_date.date()} / {mkt} "
              f"{df_all[df_all['Market'] == mkt]['Code'].nunique()}종목")

    # 주봉 이벤트(검증 전략)는 KOSPI 전용 — 코스닥 편입은 후보 리스트에만 적용한다
    df = df_all[df_all["Market"] == "KOSPI"].copy()

    complete_week = complete_week_of(last_date)
    print(f"  완결 주: {complete_week.date()} 마감")

    df["wk"] = df["Date"] + pd.to_timedelta(4 - df["Date"].dt.weekday, unit="D")

    # ── 주봉 집계 + 순위 + 직전 51주 고가 ──
    active = df[~df["halted"]]
    wagg = active.groupby(["Code", "wk"]).agg(
        whigh=("adjHigh", "max"), wclose=("adjClose", "last"),
        wclose_raw=("Close", "last"), wamount=("Amount", "sum"),
        lastdate=("Date", "max"), name=("Name", "last"),
    ).reset_index()
    wagg["amt_rank"] = wagg.groupby("wk")["wamount"].rank(ascending=False, method="min")
    daily_rank = active.copy()
    daily_rank["cap_rank"] = daily_rank.groupby("Date")["Marcap"].rank(ascending=False, method="min")
    cap_map = daily_rank.set_index(["Code", "Date"])["cap_rank"]
    wagg["cap_rank"] = cap_map.reindex(
        pd.MultiIndex.from_arrays([wagg["Code"], wagg["lastdate"]])).values
    wagg.sort_values(["Code", "wk"], inplace=True)
    wagg["prior51max"] = (wagg.groupby("Code")["whigh"]
                          .transform(lambda s: s.shift(1).rolling(51, min_periods=51).max()))

    sig = wagg[
        (wagg["amt_rank"] <= AMT_RANK_MAX)
        & (wagg["cap_rank"] <= CAP_RANK_MAX)
        & (wagg["wclose"] > wagg["prior51max"])
        & (wagg["lastdate"] >= START_SIGNAL)
        & (wagg["wk"] <= complete_week)
    ].sort_values("lastdate")
    print(f"  후보 이벤트(거래대금≤{AMT_RANK_MAX}위·시총≤{CAP_RANK_MAX}위): {len(sig)}건 / "
          f"검증 전략 기준(≤10위·30~50위): "
          f"{len(sig[(sig['amt_rank'] <= 10) & sig['cap_rank'].between(30, 50)])}건")

    events = []
    for _, s in sig.iterrows():
        events.append({
            "code": s["Code"],
            "name": s["name"],
            "entry_date": str(s["lastdate"].date()),   # 주 마지막 거래일 (보통 금요일)
            "entry_close_raw": float(s["wclose_raw"]),  # 원시 종가 (표시용)
            "entry_close_adj": round(float(s["wclose"]), 1),  # 수정 종가 (계산용)
            "amt_rank": int(s["amt_rank"]),
            "cap_rank": int(s["cap_rank"]),
        })

    # ── 종목당 일봉 1회 저장: 최초 신호 다음 거래일 ~ min(마지막 신호+MAX_BARS_AFTER, 오늘) ──
    span = sig.groupby("Code")["lastdate"].agg(["min", "max"])
    prices: dict[str, list] = {}
    for code, row in span.iterrows():
        sub = df[(df["Code"] == code) & (df["Date"] > row["min"]) & (~df["halted"])]
        after_last = (sub["Date"] > row["max"]).cumsum()  # 마지막 신호 이후 경과 봉 수
        sub = sub[after_last <= MAX_BARS_AFTER]
        prices[code] = [
            [d.strftime("%Y-%m-%d"), round(h, 1), round(l, 1), round(c, 1)]
            for d, h, l, c in zip(sub["Date"], sub["adjHigh"], sub["adjLow"], sub["adjClose"])
        ]
    n_bars = sum(len(v) for v in prices.values())
    print(f"  일봉: {len(prices)}종목 {n_bars:,}봉")

    output = {
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "start_date": str(START_SIGNAL.date()),
        "data_last_date": str(last_date.date()),
        "complete_week": str(complete_week.date()),
        "bounds": {"amt_rank_max": AMT_RANK_MAX, "cap_rank_max": CAP_RANK_MAX},
        "events": events,
        "prices": prices,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    size_mb = OUT.stat().st_size / 1e6
    print(f"=== 완료: {len(events)}건 이벤트 → {OUT} ({size_mb:.1f}MB) ===")

    # 후보 리스트는 이벤트와 독립 — 여기서 실패해도 본체 산출물은 이미 저장돼 있다
    try:
        build_candidates(df_all, last_date)
    except Exception as e:
        print(f"!!! 신고가 후보 산출 실패 (이벤트 데이터는 정상): {e}")


if __name__ == "__main__":
    main()
