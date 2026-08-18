# -*- coding: utf-8 -*-
"""일회성 진단 2 — 야간선물이 어느 시장구분코드에 있는지 찾는다.

probe 1 결과: FID_COND_MRKT_DIV_CODE="F" 로 A01609 를 부르면 **주간 정규장** 선물이 온다.
(전일 O/H/L/C = 1119.80/1144.90/1070.25/1078.25, 거래량 150,055 — 8/18 정규장 봉과 일치)
즉 지금까지 야간선물을 한 번도 조회한 적이 없다. 야간 구분코드를 찾는다.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from data_provider import load_config, kis_get  # noqa: E402

FIELDS = ("futs_prpr", "futs_prdy_vrss", "futs_prdy_ctrt", "futs_prdy_clpr", "futs_sdpr",
          "futs_oprc", "futs_hgpr", "futs_lwpr", "acml_vol", "hts_kor_isnm")


async def main():
    cfg = load_config()
    for tr in ("FHMIF10000000", "FHMCF10000000"):
        for div in ("F", "CF", "NF", "JF", "MF", "CM"):
            try:
                d = await kis_get(cfg, "/uapi/domestic-futureoption/v1/quotations/inquire-price",
                                  tr, {"FID_COND_MRKT_DIV_CODE": div, "FID_INPUT_ISCD": "A01609"})
                o = (d or {}).get("output1") or {}
                if not o:
                    print(f"{tr} div={div:<3} -> 빈 응답 {str(d)[:120]}")
                    continue
                print(f"{tr} div={div:<3} -> " + " ".join(f"{k}={o.get(k)}" for k in FIELDS))
            except Exception as e:
                print(f"{tr} div={div:<3} -> ERR {str(e)[:120]}")


asyncio.run(main())
