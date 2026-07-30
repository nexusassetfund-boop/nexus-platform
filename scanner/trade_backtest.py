"""
수출 신호 백테스트 — ATH·가속·급감 신호가 실제 주가 수익으로 이어졌는지 검증한다.

왜: 트래커는 '역대최고' 배지가 매수 신호로 유효하다고 가정만 하고 있다. 같은 발상의
KoAct K수출핵심기업TOP30액티브(0074K0)가 최근 1개월 -34%를 맞은 것이 보여주듯,
검증 없는 모멘텀 신호는 위험하다. HANDOFF §3 — 전략 규칙은 백테스트 후 사용자 승인.

이벤트 스터디 설계:
  신호 (종목×월 단위, 시계열 전체에서 판정 — 종목의 현재 상태와 무관):
    S1 신규 ATH   : 그 달 수출이 직전까지의 전 기간 최고치 경신 (선행 12개월 이상,
                    금액 $1M 이상 — 소액 ATH는 노이즈)
    S2 분기 가속   : 분기 합계 YoY ≥ +50% 이면서 직전 분기는 미달 (분기말 월에만 판정)
    S3 급감 경고   : 분기 합계 YoY ≤ -30% 이면서 직전 분기는 미달 (회피 신호 검증)
  시차 (lookahead 방지): 신호 월 M의 확정치는 M+1개월 15일 공개
    → 진입은 M+1개월 **월말 종가**, 수익률은 그로부터 1·3·6개월 뒤 월말 종가.
  벤치마크: 같은 구간 KOSPI 수익률을 차감한 초과수익.

한계 (결과 해석 시 필수):
  - 현재 52종목은 '지금 좋아 보여서' 큐레이션된 목록이다 — 생존/선택 편향으로
    성과가 과대평가될 수 있다. 신호 판정은 시계열 전체에서 하지만 유니버스 자체가
    사후 선택이라는 점은 남는다.
  - 거래비용·슬리피지 미반영. 월말 종가 체결 가정.

실행: CUSTOMS 키 불필요. 주가는 pykrx 월봉.
출력: docs/data/trade_backtest.json + 콘솔 요약. 화면 규칙을 자동으로 바꾸지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from pullback_screener import _pykrx_stock   # 지연 import + KRX 재시도 헬퍼 재사용

logger = logging.getLogger("trade_bt")

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
TRADE_PATH = ROOT / "docs" / "data" / "trade.json"
OUT_PATH = ROOT / "docs" / "data" / "trade_backtest.json"

MIN_HISTORY = 12          # ATH 판정에 필요한 선행 개월
ATH_MIN_AMT = 1000.0      # ATH 신호 하한 (천USD = $1M)
ACCEL_YOY = 50.0          # S2 가속 기준 (%)
DROP_YOY = -30.0          # S3 급감 기준 (%)
HORIZONS = (1, 3, 6)      # 보유 개월


def _prev_yymm(yymm: str, back: int) -> str:
    y, m = int(yymm[:4]), int(yymm[4:])
    t = y * 12 + (m - 1) - back
    return f"{t // 12:04d}{t % 12 + 1:02d}"


def _add_months(yymm: str, n: int) -> str:
    return _prev_yymm(yymm, -n)


def _q_yoy(ser: dict[str, float], month: str) -> float | None:
    """month가 속한 분기(월말 기준 직전 3개월)의 합 대 전년 동기간 합."""
    ms = [_prev_yymm(month, k) for k in range(3)]
    a = sum(ser.get(m, 0.0) for m in ms)
    b = sum(ser.get(_prev_yymm(m, 12), 0.0) for m in ms)
    if b < ATH_MIN_AMT:      # 기준이 너무 작으면 증감률이 폭주한다
        return None
    return (a - b) / b * 100


def find_signals(ser: dict[str, float]) -> dict[str, list[str]]:
    """{신호명: [발생월...]}. 시계열 전체에서 판정한다."""
    months = sorted(ser)
    out = {"S1_ath": [], "S2_accel": [], "S3_drop": []}
    for i, m in enumerate(months):
        amt = ser[m]
        prior = [ser[x] for x in months[:i]]
        if len(prior) >= MIN_HISTORY and amt >= ATH_MIN_AMT and amt > max(prior):
            out["S1_ath"].append(m)
        # 분기 신호는 분기말 월(3·6·9·12)에만 — 같은 분기를 세 번 세지 않는다
        if int(m[4:]) in (3, 6, 9, 12) and i >= 15:
            q = _q_yoy(ser, m)
            qp = _q_yoy(ser, _prev_yymm(m, 3))
            if q is not None and qp is not None:
                if q >= ACCEL_YOY and qp < ACCEL_YOY:
                    out["S2_accel"].append(m)
                if q <= DROP_YOY and qp > DROP_YOY:
                    out["S3_drop"].append(m)
    return out


def _naver_month_closes(symbol: str, start: str, end: str) -> dict[str, float]:
    """네이버 fchart 일봉 → 월말 종가. pykrx가 막혔을 때의 폴백 경로."""
    import re
    import urllib.request
    url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}"
           "&timeframe=day&count=2000&requestType=0")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            xml = r.read().decode("euc-kr", errors="replace")
    except Exception as e:
        logger.warning("네이버 fchart 실패 %s: %s", symbol, e)
        return {}
    daily: dict[str, tuple[str, float]] = {}
    for m in re.finditer(r'data="(\d{8})\|[\d.]+\|[\d.]+\|[\d.]+\|([\d.]+)\|', xml):
        d, close = m.group(1), float(m.group(2))
        ym = d[:6]
        if ym not in daily or d > daily[ym][0]:
            daily[ym] = (d, close)
    return {ym: c for ym, (_, c) in daily.items() if start <= ym <= end}


def month_closes(code: str, start: str, end: str) -> dict[str, float]:
    """월말 종가 {YYYYMM: close}. pykrx 월봉 → 실패 시 네이버 fchart."""
    try:
        stock = _pykrx_stock()
        df = stock.get_market_ohlcv(f"{start}01", f"{end}28", code, freq="m")
        out = {idx.strftime("%Y%m"): float(row["종가"]) for idx, row in df.iterrows()}
        if out:
            return out
    except Exception as e:
        logger.warning("pykrx 주가 실패 %s: %s — 네이버 폴백", code, str(e)[:50])
    return _naver_month_closes(code, start, end)


def index_closes(start: str, end: str) -> dict[str, float]:
    """KOSPI 월말 종가. pykrx가 KRX 차단으로 깨지면 네이버 fchart로 폴백(HANDOFF §4-5)."""
    try:
        stock = _pykrx_stock()
        df = stock.get_index_ohlcv(f"{start}01", f"{end}28", "1001", freq="m")
        return {idx.strftime("%Y%m"): float(row["종가"]) for idx, row in df.iterrows()}
    except Exception as e:
        logger.warning("pykrx 지수 실패(%s) — 네이버 fchart 폴백", str(e)[:60])
    import re
    import urllib.request
    url = ("https://fchart.stock.naver.com/sise.nhn?symbol=KOSPI"
           "&timeframe=day&count=2000&requestType=0")
    with urllib.request.urlopen(url, timeout=30) as r:
        xml = r.read().decode("euc-kr", errors="replace")
    daily: dict[str, tuple[str, float]] = {}   # {YYYYMM: (YYYYMMDD, close)} 마지막 거래일
    for m in re.finditer(r'data="(\d{8})\|[\d.]+\|[\d.]+\|[\d.]+\|([\d.]+)\|', xml):
        d, close = m.group(1), float(m.group(2))
        ym = d[:6]
        if ym not in daily or d > daily[ym][0]:
            daily[ym] = (d, close)
    return {ym: c for ym, (_, c) in daily.items() if start <= ym <= end}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    trade = json.loads(TRADE_PATH.read_text(encoding="utf-8"))
    stocks = trade["stocks"]
    today = dt.datetime.now(tz=KST).date()
    now_m = f"{today.year:04d}{today.month:02d}"

    all_months = sorted({p["m"] for s in stocks for p in s["series"]})
    start, end = all_months[0], now_m
    logger.info("종목 %d · 시계열 %s~%s", len(stocks), start, all_months[-1])

    kospi = index_closes(start, end)
    if not kospi:
        logger.error("KOSPI 월봉 실패 — 중단")
        sys.exit(1)

    episodes = []
    px_cache: dict[str, dict[str, float]] = {}
    for s in stocks:
        ser = {p["m"]: p["amt"] for p in s["series"]}
        sigs = find_signals(ser)
        if not any(sigs.values()):
            continue
        code = str(s["ticker"]).zfill(6)
        if code not in px_cache:
            px_cache[code] = month_closes(code, start, end)
        px = px_cache[code]
        if not px:
            continue
        for sig, months in sigs.items():
            for m in months:
                entry_m = _add_months(m, 1)           # 확정치 공개(M+1 15일) 후 월말 진입
                if entry_m not in px or entry_m not in kospi:
                    continue
                ep = {"signal": sig, "ticker": code, "name": s["name"],
                      "signal_month": m, "entry_month": entry_m,
                      "amt": round(ser[m], 1)}
                for h in HORIZONS:
                    xm = _add_months(entry_m, h)
                    if xm in px and xm in kospi:
                        r = (px[xm] / px[entry_m] - 1) * 100
                        b = (kospi[xm] / kospi[entry_m] - 1) * 100
                        ep[f"ret_{h}m"] = round(r, 1)
                        ep[f"exc_{h}m"] = round(r - b, 1)
                episodes.append(ep)

    # 집계
    summary = {}
    for sig in ("S1_ath", "S2_accel", "S3_drop"):
        eps = [e for e in episodes if e["signal"] == sig]
        row = {"n": len(eps)}
        for h in HORIZONS:
            vals = [e[f"exc_{h}m"] for e in eps if f"exc_{h}m" in e]
            raw = [e[f"ret_{h}m"] for e in eps if f"ret_{h}m" in e]
            if vals:
                row[f"{h}m"] = {
                    "n": len(vals),
                    "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
                    "avg_exc": round(statistics.fmean(vals), 2),
                    "med_exc": round(statistics.median(vals), 2),
                    "avg_raw": round(statistics.fmean(raw), 2),
                }
        summary[sig] = row

    doc = {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "design": {
            "signals": {"S1_ath": "신규 역대최고 (선행 12M+, $1M+)",
                        "S2_accel": f"분기 YoY ≥ +{ACCEL_YOY:.0f}% 진입 (분기말 판정)",
                        "S3_drop": f"분기 YoY ≤ {DROP_YOY:.0f}% 진입 (회피 신호)"},
            "entry": "신호월 M+1 월말 종가 (확정치 공개 이후 — lookahead 없음)",
            "benchmark": "KOSPI 동일구간 차감 (exc_*)",
            "caveat": "52종목 유니버스가 사후 큐레이션이라 선택 편향으로 성과가 "
                      "과대평가될 수 있다. 거래비용 미반영.",
        },
        "summary": summary,
        "episodes": episodes,
    }
    OUT_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    print()
    print("=== 수출 신호 백테스트 (KOSPI 대비 초과수익) ===")
    for sig, row in summary.items():
        label = doc["design"]["signals"][sig]
        print(f"\n[{sig}] {label} — 에피소드 {row['n']}건")
        for h in HORIZONS:
            r = row.get(f"{h}m")
            if r:
                print(f"   {h}M 보유: n={r['n']:>3}  승률 {r['win_rate']:>5.1f}%  "
                      f"평균초과 {r['avg_exc']:>+6.2f}%  중앙 {r['med_exc']:>+6.2f}%  "
                      f"(절대 {r['avg_raw']:>+6.2f}%)")
    print(f"\n저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
