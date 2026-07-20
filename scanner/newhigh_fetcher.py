# -*- coding: utf-8 -*-
"""52주 신고가 주도주 이벤트드리븐 데이터 수집 (넥서스 이벤트드리븐 > 52주 신고가 탭용)

데이터: marcap (KRX 원본 전종목 일별 시세·시가총액·거래대금, FinanceData 공개 배포)
  https://github.com/FinanceData/marcap — pykrx는 KRX 로그인 요구로 CI 불가라 사용 안 함.

원본 전략(검증 완료: 2021-01~2026-07, 29건, 승률 82.8%, 평균 +27.1%)의 계산부만 이식:
  KOSPI 주봉에서 ①주간 거래대금 순위 ②주 마지막 거래일 시총 순위 ③직전 51주 고가 최대값을
  구해 "주봉 종가 > 직전 51주 고가"인 후보 이벤트를 산출한다.
  프론트가 매수 필터(거래대금·시총 순위)를 조절할 수 있도록 검증 전략(10위/30~50위)보다
  넓게(거래대금 ≤30위, 시총 ≤200위) 담고, 매매 시뮬레이션은 전부 클라이언트에서 수행한다.

산출: docs/data/newhigh_backtest.json (repo 커밋 안 함 — post_newhigh_backtest.py가 KV 게시)
  events[] = 후보 이벤트(코드·이름·진입일·진입가·순위) — 시뮬레이션은 안 함
  prices{} = 코드 → [[날짜, 수정고가, 수정저가, 수정종가], ...] — 종목당 1회(이벤트 간 중복 제거)
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "marcap_cache"
OUT = ROOT / "docs" / "data" / "newhigh_backtest.json"

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).date()
START_SIGNAL = pd.Timestamp("2021-01-01")   # 백테스트 시작 — 이 날짜 이후 신호만
DATA_START_YEAR = 2020                       # 51주 룩백 확보용 여유
AMT_RANK_MAX = 30                            # 후보 상한 — 검증 전략(10위)보다 넓게 (프론트 필터용)
CAP_RANK_MAX = 200                           # 후보 상한 — 검증 전략(30~50위)보다 넓게
MAX_BARS_AFTER = 160                         # 마지막 신호 이후 최대 봉 수 (~7.5개월, 만기 6개월 + 여유)


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
    df = df[df["Market"] == "KOSPI"].copy()
    # 우선주 제외 (종목코드 끝자리 0 = 보통주) — 삼성전자우 등이 거래대금·시총 순위를
    # 차지해 실질 편입 슬롯을 줄이는 문제 방지. 순위 산정·후보 모두에서 뺀다 (사용자 요청 2026-07-21)
    df = df[df["Code"].str.endswith("0")]
    df["Date"] = pd.to_datetime(df["Date"])
    df.sort_values(["Code", "Date"], inplace=True)
    return df


def complete_week_of(last_date: pd.Timestamp):
    """완결 주(금요일) 판정 — 원본 로직. 금요일 데이터가 아직 없으면 직전 주까지만."""
    last_friday = TODAY - timedelta(days=(TODAY.weekday() - 4) % 7)
    if last_date.date() >= last_friday:
        return pd.Timestamp(last_friday)
    if (TODAY - last_friday).days >= 3:
        # 월요일 이후에도 금요일 데이터가 없으면 금요일 휴장으로 간주 (목요일 마감 주)
        return pd.Timestamp(last_friday)
    return pd.Timestamp(last_friday) - pd.Timedelta(weeks=1)


def main():
    print("=== 52주 신고가 데이터 수집 시작 (marcap) ===")
    df = download_marcap()
    last_date = df["Date"].max()
    print(f"  데이터: ~ {last_date.date()} / KOSPI {df['Code'].nunique()}종목")

    complete_week = complete_week_of(last_date)
    print(f"  완결 주: {complete_week.date()} 마감")

    # ── 수정주가 복원 (기준가 = Close - Changes) — 원본 로직 그대로 ──
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


if __name__ == "__main__":
    main()
