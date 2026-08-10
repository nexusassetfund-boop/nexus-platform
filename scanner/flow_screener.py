"""수급 감지기 — 외국인·기관 연속 순매수 종목 탐지. 출력: docs/data/flow.json

프론트 '스테이지 감지기 > 수급' 하위탭이 읽는다.

설계 요지 (flow_history 참조):
  · '연속 N일'을 1차 기준으로 쓰지 않는다. 하루 노이즈 매도로 리셋되는 게 옛 방식의 결함.
    흠집 허용 스트릭 + 창(10거래일) 강도·지속성을 함께 본다.
  · 순매수는 주수를 거래량으로 정규화한다(intensity). 10만주는 종목마다 의미가 다르다.
  · 1일 집중도(concentr)가 높으면 지수 리밸런싱·블록딜·ETF LP 물량일 공산이 크다 — 후보에서 뺀다.
  · 추세형(종가≥MA20) / 역발상형(종가<MA20)을 나눠서 낸다. 같은 표에 섞으면 해석이 안 된다.

등급:
  S  외국인·기관 양쪽 스트릭 ≥ MIN_STREAK (쌍끌이)
  A  한쪽 스트릭 ≥ MIN_STREAK
  (둘 다 미달이면 후보 아님)

임계치는 flow_backtest.py 로 검증한 뒤 조정할 것. 검증 전에는 '매수 신호'가 아니라
**관찰 리스트**다 — 프론트 문구도 그렇게 되어 있다.

실행: 매일 장마감 후. 테스트: FLOW_LIMIT=20 python scanner/flow_screener.py
실패 정책: 수급 확보가 유니버스의 절반 미만이면 기존 출력 보존 후 exit 1.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import flow_history as fh

logger = logging.getLogger("flow")
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = ROOT / "docs" / "data" / "flow.json"
UNIV_CACHE = ROOT / "docs" / "data" / "flow_universe.json"

UNIVERSE_N = int(os.environ.get("FLOW_LIMIT", "300"))   # 시총 상위 N
MIN_STREAK = 4              # 흠집 허용 연속 순매수일
MIN_INTENSITY = 0.02        # 창 누적 순매수 / 창 누적 거래량 ≥ 2%
MAX_CONCENTR = 0.6          # 1일 집중도 상한 (리밸런싱·블록딜 배제)
WORKERS = 8


def _row(code: str, name: str) -> dict | None:
    rows = fh._safe_fetch(code, pages=2)            # 40거래일 — MA20 + 창 여유
    if len(rows) < 20:
        return None
    m = fh.metrics(rows)
    if m is None:
        return None
    f, i = m["frgn"], m["inst"]
    ma20 = statistics.fmean(r["close"] for r in rows[-20:])
    close = m["close"]
    concentr = max(f["concentr"], i["concentr"])
    both = f["streak"] >= MIN_STREAK and i["streak"] >= MIN_STREAK
    one = f["streak"] >= MIN_STREAK or i["streak"] >= MIN_STREAK
    return {
        "ticker": code,
        "name": name,
        "close": close,
        "base_date": m["date"],
        "frgn_streak": f["streak"], "frgn_blemish": f["blemish"],
        "inst_streak": i["streak"], "inst_blemish": i["blemish"],
        "frgn_hard_streak": f["hard_streak"], "inst_hard_streak": i["hard_streak"],
        "frgn_net": int(f["net"]), "inst_net": int(i["net"]),
        "frgn_intensity_pct": round(f["intensity"] * 100, 2),
        "inst_intensity_pct": round(i["intensity"] * 100, 2),
        "intensity_pct": round(m["both_intensity"] * 100, 2),
        "frgn_persist": round(f["persist"], 2),
        "inst_persist": round(i["persist"], 2),
        "concentr": round(concentr, 2),
        "net_value_억": round((f["net"] + i["net"]) * close / 1e8),
        "avg_volume": int(m["avg_volume"]),
        "ma20": round(ma20),
        "regime": "trend" if close >= ma20 else "contra",
        "grade": "S" if both else ("A" if one else "-"),
        "is_candidate": int(one and m["both_intensity"] >= MIN_INTENSITY
                            and concentr <= MAX_CONCENTR),
    }


def build() -> dict | None:
    univ = fh.universe(UNIVERSE_N, UNIV_CACHE)
    logger.info("유니버스 %d종목 수급 조회", len(univ))
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(lambda kv: _row(*kv), univ.items()):
            if r:
                rows.append(r)
    logger.info("수급 확보 %d/%d", len(rows), len(univ))
    if len(rows) < len(univ) * 0.5:
        return None

    cands = [r for r in rows if r["is_candidate"]]
    cands.sort(key=lambda r: (r["grade"] != "S", -r["intensity_pct"]))
    for n, r in enumerate(cands, 1):
        r["rank"] = n
    base = max((r["base_date"] for r in rows), default="")
    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "snap_date": f"{base[:4]}-{base[4:6]}-{base[6:]}" if base else "",
        "thresholds": {
            "window": fh.WINDOW, "min_streak": MIN_STREAK,
            "tol_ratio": fh.TOL_RATIO, "max_blemish": fh.MAX_BLEMISH,
            "min_intensity_pct": MIN_INTENSITY * 100,
            "max_concentr": MAX_CONCENTR,
            "universe_n": UNIVERSE_N,
            "note": "스트릭은 '흠집 허용' — 직전 평균 순매수의 30% 미만인 매도일은 연속을 끊지 않는다. "
                    "강도는 순매수주수/거래량(주수 정규화). 기관은 네이버 기준 기관합계로 "
                    "금융투자(ETF LP·차익거래)가 섞여 있다. 백테스트 검증 전까지 관찰용.",
        },
        "scanned": len(rows),
        "count": len(cands),
        "s_count": sum(1 for r in cands if r["grade"] == "S"),
        "trend_count": sum(1 for r in cands if r["regime"] == "trend"),
        "candidates": cands,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = build()
    if data is None:
        logger.error("수급 스캔 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("저장: %s (후보 %d, 쌍끌이 %d, 추세형 %d)",
                OUT_PATH, data["count"], data["s_count"], data["trend_count"])


if __name__ == "__main__":
    main()
