"""Phase 0 — 섹터 ETF 가격 데이터 품질 검증 (1회성/수동 실행).

전 섹터 대표 ETF의 300일 일봉에서 |일간 수익률| > 15% 봉을 찾아 보고한다.
플래그가 나오면 해당 구간을 2차 소스(네이버금융/KRX)와 수동 대조할 것 —
수정주가(분배금·분할) 미반영이 흔한 원인이다.

운영 파이프라인은 sector_rrg.clean_daily()가 같은 기준으로 자동 필터링하므로,
이 스크립트는 원인 규명·문서화용이다.

사용: python validate_prices.py
종료코드: 0 = 플래그 없음, 1 = 플래그 존재
"""

import asyncio
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp949 콘솔 대응

from data_provider import fetch_ohlcv
from run_scan import SECTOR_ETFS
from sector_rrg import OUTLIER_PCT, clean_daily


async def main() -> int:
    total_flags = 0
    print(f"섹터 ETF {len(SECTOR_ETFS)}종목 — |일간 수익률| > {OUTLIER_PCT}% 검사\n")
    for slug, (code, name) in SECTOR_ETFS.items():
        try:
            df = await fetch_ohlcv(code, days=300)
        except Exception as e:
            print(f"[조회실패] {slug} {code} {name}: {e}")
            continue
        if df is None or df.empty:
            print(f"[데이터없음] {slug} {code} {name}")
            continue
        closes = df["close"]
        _, flags = clean_daily(closes)
        if flags:
            total_flags += len(flags)
            print(f"[플래그] {slug} {code} {name}: {len(flags)}건")
            for d in flags:
                row = closes[closes.index.strftime("%Y-%m-%d") == d]
                idx = closes.index.get_loc(row.index[0])
                prev = float(closes.iloc[idx - 1]) if idx > 0 else float("nan")
                cur = float(row.iloc[0])
                print(f"    {d}: {prev:,.0f} → {cur:,.0f} ({(cur / prev - 1) * 100:+.1f}%)")
        else:
            print(f"[정상] {slug} {code} {name} ({len(closes)}봉)")
    print(f"\n총 플래그: {total_flags}건")
    return 1 if total_flags else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
