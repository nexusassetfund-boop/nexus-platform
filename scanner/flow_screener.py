"""수급 감지기 — 외국인·기관 연속 순매수 종목 탐지. 출력: docs/data/flow.json

프론트 '스테이지 감지기 > 수급' 하위탭이 읽는다.

설계 요지 (flow_history 참조):
  · '연속 N일'을 1차 기준으로 쓰지 않는다. 하루 노이즈 매도로 리셋되는 게 옛 방식의 결함.
    흠집 허용 스트릭 + 창(10거래일) 강도·지속성을 함께 본다.
  · 순매수는 주수를 거래량으로 정규화한다(intensity). 10만주는 종목마다 의미가 다르다.
  · 1일 집중도(concentr)가 높으면 지수 리밸런싱·블록딜·ETF LP 물량일 공산이 크다 — 후보에서 뺀다.
  · 추세형(종가≥MA20) / 역발상형(종가<MA20)을 나눠서 낸다. 같은 표에 섞으면 해석이 안 된다.

## 백테스트 결과가 설계를 바꿨다 (flow_backtest.py, 96신호일 / 28,178관측)
  · 외국인은 (+), 기관은 (−)다. intensity_frgn IC +0.028(t=3.27) / intensity_inst
    IC −0.029(t=−2.85), persist_inst −0.031(t=−3.14). 기관합계는 '노이즈'가 아니라
    반대 방향 신호였다 — 금융투자·ETF LP가 섞인 탓으로 추정하나 분리 불가(위 참조).
  · 그래서 **쌍끌이를 최상위 등급으로 두지 않는다**. tol_both_ge4 IC −0.011로
    오히려 음수 — 기관 조건을 AND로 걸면 외국인 신호를 망친다.
  · 가장 강한 신호는 외국인 우위 강도(frgn − inst) IC +0.033(t=3.96),
    실전 조합은 추세형 한정 외국인 강도 IC +0.031(t=3.58, 초과수익 +0.60%/20거래일).
  · '연속 N일'은 강도·지속성에 진다. hard/tol 스트릭 4일·5일 전부 |t|<2.
    흠집 허용이 무관용보다 낫지도 않았다(tol 0.008 vs hard 0.011) — 스트릭은
    사용자가 요구한 탐지 조건으로 남기되 **랭킹은 강도로** 한다.
  · 초과수익 절대값이 +0.6%/20거래일이라 거래비용 감안 시 알파라 부르기 어렵다.
    → '매수 신호'가 아니라 **관찰 리스트**. 프론트 문구도 그렇게 되어 있다.

등급 (위 결과 반영):
  S  외국인 자격 + 추세형(종가≥MA20) + 외국인 우위(edge>0) — 백테스트 최선 조합
  A  외국인 자격
  C  기관 단독 자격 — 백테스트상 (−) 신호라 경고 표시용으로만 남긴다
  자격 = 흠집 허용 스트릭 ≥ MIN_STREAK 이고 해당 주체 강도 ≥ MIN_INTENSITY

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
# 창 누적 순매수 / 창 누적 거래량. 백테스트가 측정한 건 '강도 상위 20%' 구간이라
# 유니버스 상위 20% 언저리(실측 p80 ≈ 13%, 완화해서 5%)에 맞춘다. 2%로 두면 300종목 중
# 115종목이 후보로 잡혀 관찰 리스트 구실을 못 했다.
MIN_INTENSITY = 0.05
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
    trend = close >= ma20
    edge = f["intensity"] - i["intensity"]          # 백테스트 최강 신호 (IC 0.033, t=3.96)
    f_ok = f["streak"] >= MIN_STREAK and f["intensity"] >= MIN_INTENSITY
    i_ok = i["streak"] >= MIN_STREAK and i["intensity"] >= MIN_INTENSITY
    grade = "S" if (f_ok and trend and edge > 0) else "A" if f_ok else "C" if i_ok else "-"
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
        "regime": "trend" if trend else "contra",
        "edge_pct": round(edge * 100, 2),           # 외국인 우위 강도 — 랭킹 기준
        "grade": grade,
        "is_candidate": int(grade != "-" and concentr <= MAX_CONCENTR),
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
    _ORDER = {"S": 0, "A": 1, "C": 2}
    cands.sort(key=lambda r: (_ORDER.get(r["grade"], 9), -r["edge_pct"]))
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
                    "금융투자(ETF LP·차익거래)가 섞여 있다. 초과수익이 20거래일 +0.6% 수준이라 "
                    "거래비용 감안 시 매수 신호가 아니라 관찰 리스트다.",
        },
        # 프론트가 그대로 노출하는 검증 요약 — 근거 없는 등급으로 보이지 않게 한다
        "evidence": {
            "period": "96 신호일 / 28,178 관측 (주간 리밸런싱, 20거래일 성과)",
            "best": "외국인 우위 강도(외인−기관) IC +0.033 (t=3.96)",
            "trend_only": "추세형 한정 외국인 강도 IC +0.031 (t=3.58), 초과수익 +0.60%",
            "inst_negative": "기관 강도 IC −0.029 (t=−2.85) · 기관 지속성 −0.031 (t=−3.14)",
            "streak_weak": "연속일수(무관용·흠집허용, 4·5일) 전부 |t| < 2 — 랭킹은 강도로 한다",
            "both_bad": "쌍끌이(양쪽 스트릭) IC −0.011 — 기관을 AND로 걸면 외국인 신호가 죽는다",
            "caveat": "생존편향(현 상장사 스냅샷) · 기관합계 · 거래비용 미반영",
        },
        "scanned": len(rows),
        "count": len(cands),
        "s_count": sum(1 for r in cands if r["grade"] == "S"),
        "a_count": sum(1 for r in cands if r["grade"] == "A"),
        "c_count": sum(1 for r in cands if r["grade"] == "C"),
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
