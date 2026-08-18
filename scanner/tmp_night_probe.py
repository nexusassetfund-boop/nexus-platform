# -*- coding: utf-8 -*-
"""일회성 진단 — KIS 야간선물(A016xx) 원본 필드와 일봉을 그대로 덤프한다.

배경: 2026-08-19 am 브리핑이 야간선물 1,078.25(-1.88%)로 나갔는데 실제는 -4.29% 급락이었다.
05:01 스냅샷이 잡은 값이 세션 종가가 아니었다는 뜻 — 어떤 필드가 진짜 종가를 담는지 확인한다.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from briefing_data import _cme_front_month  # noqa: E402
from data_provider import load_config, kis_get  # noqa: E402


async def main():
    cfg = load_config()
    code = _cme_front_month()
    print("front month:", code)

    data = await kis_get(cfg, "/uapi/domestic-futureoption/v1/quotations/inquire-price",
                         "FHMIF10000000",
                         {"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": code})
    print("\n=== inquire-price output1 ===")
    print(json.dumps((data or {}).get("output1") or {}, ensure_ascii=False, indent=1))
    for k in ("output2", "output3"):
        if (data or {}).get(k):
            print(f"\n=== {k} ===")
            print(json.dumps(data[k], ensure_ascii=False, indent=1)[:2000])

    print("\n=== 야간선물 일봉 (inquire-daily-fuopchartprice, D) ===")
    try:
        d = await kis_get(cfg, "/uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice",
                          "FHKIF03020100",
                          {"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": code,
                           "FID_INPUT_DATE_1": "20260810", "FID_INPUT_DATE_2": "20260819",
                           "FID_PERIOD_DIV_CODE": "D"})
        print(json.dumps((d or {}).get("output1") or {}, ensure_ascii=False, indent=1))
        print(json.dumps(((d or {}).get("output2") or [])[:12], ensure_ascii=False, indent=1))
    except Exception as e:
        print("daily chart error:", e)


asyncio.run(main())
