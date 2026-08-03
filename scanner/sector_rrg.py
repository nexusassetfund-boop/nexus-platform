"""섹터 RRG(Relative Rotation Graph) 근사 계산 — 순수 함수 모듈.

방법론 (계획안 v1, Phase 1):
  - 주간(금요일 확정 종가) 기준. 진행 중인 주는 제외 → 장중 재실행에도 좌표 멱등.
  - 벤치마크 = 섹터 대표 ETF 동일가중 합성지수 (섹터 유니버스 내부 상대강도).
  - RS        = 100 × 섹터 / 벤치마크
  - RS-Ratio  = 100 × RS / SMA(RS, 12주)          (X축)
  - RS-Momentum = 100 + RS-Ratio의 4주 ROC(%)      (Y축)
  z-score 정규화는 쓰지 않는다 — 보유 히스토리(≈60주)로 롤링 창을 감당할 수 없고,
  짧은 창은 기존의 "좌표 급변" 문제를 재생산하기 때문(고정 스케일 근사).

데이터 품질 (Phase 0): |일간 수익률| > OUTLIER_PCT 봉은 데이터 오류로 간주,
해당 종가를 직전 종가로 대체하고 data_flags에 기록한다.

이 모듈은 API 호출 없이 {slug: 종가 Series}만 받는다 — run_scan(운영),
validate_prices(검증), replay_rrg(리플레이)가 공유한다.
"""

import datetime as dt
import math

import pandas as pd

OUTLIER_PCT = 15.0        # 일간 수익률 이상치 임계(%) — ETF 기준
SMA_WEEKS = 12            # RS-Ratio 스무딩 창
ROC_WEEKS = 4             # RS-Momentum 변화율 창
TAIL_WEEKS = 8            # 화면에 표시할 궤적 길이
MIN_WEEKS = SMA_WEEKS + ROC_WEEKS + TAIL_WEEKS  # 산출에 필요한 최소 주봉 수

QUADRANTS = {
    ("hi", "hi"): ("leading", "주도"),
    ("hi", "lo"): ("weakening", "약화"),
    ("lo", "lo"): ("lagging", "소외"),
    ("lo", "hi"): ("improving", "부상"),
}


def clean_daily(closes: pd.Series) -> tuple[pd.Series, list[str]]:
    """|일간 수익률| > OUTLIER_PCT 봉의 종가를 직전 종가로 대체. (정제 시리즈, 플래그 날짜) 반환."""
    closes = closes.dropna()
    if len(closes) < 2:
        return closes, []
    ret = closes.pct_change().abs() * 100
    bad = ret > OUTLIER_PCT
    flags = [d.strftime("%Y-%m-%d") for d in closes.index[bad]]
    if flags:
        cleaned = closes.copy()
        cleaned[bad] = float("nan")
        cleaned = cleaned.ffill()
        return cleaned, flags
    return closes, flags


def last_confirmed_friday(now: dt.datetime) -> dt.date:
    """직전 '확정된' 금요일 — 금요일 15:30(장마감) 이후여야 그 주가 확정된 것으로 본다."""
    d = now.date()
    # 오늘이 금요일이고 마감 전이면 이번 주는 미확정
    offset = (d.weekday() - 4) % 7  # 4 = Friday
    friday = d - dt.timedelta(days=offset)
    if friday == d and (now.hour, now.minute) < (15, 30):
        friday -= dt.timedelta(days=7)
    return friday


def weekly_confirmed(closes: pd.Series, cutoff: dt.date) -> pd.Series:
    """일간 종가 → 주간(W-FRI) 종가. cutoff(확정 금요일) 이후 주는 버린다."""
    w = closes.resample("W-FRI").last().dropna()
    return w[w.index.date <= cutoff]


def _heading(dx: float, dy: float) -> str:
    if dx == 0 and dy == 0:
        return "flat"
    ns = "N" if dy > 0 else ("S" if dy < 0 else "")
    ew = "E" if dx > 0 else ("W" if dx < 0 else "")
    return (ns + ew) or "flat"


def _quadrant(x: float, y: float) -> tuple[str, str]:
    return QUADRANTS[("hi" if x >= 100 else "lo", "hi" if y >= 100 else "lo")]


def weekly_xy(daily_closes: dict[str, pd.Series], cutoff: dt.date
              ) -> tuple[dict[str, pd.DataFrame], pd.Series, dict[str, list[str]]]:
    """이상치 정제 → 확정 주봉 → RS-Ratio(x)/RS-Momentum(y) 주간 시계열.

    반환: ({slug: DataFrame[x, y]}, 벤치마크(동일가중, 시작=1) 주간 시계열, data_flags)
    compute_rrg(운영 스냅샷)과 replay_rrg(Phase 4 검증)가 공유한다.
    """
    weekly_map: dict[str, pd.Series] = {}
    data_flags: dict[str, list[str]] = {}
    for slug, closes in daily_closes.items():
        cleaned, flags = clean_daily(closes)
        if flags:
            data_flags[slug] = flags
        w = weekly_confirmed(cleaned, cutoff)
        if len(w) >= MIN_WEEKS:
            weekly_map[slug] = w

    if len(weekly_map) < 4:  # 섹터가 너무 적으면 상대강도 의미 없음
        return {}, pd.Series(dtype=float), data_flags

    # 공통 주간 인덱스 (모든 섹터가 존재하는 주만) — 동일가중 합성의 전제
    common = None
    for w in weekly_map.values():
        common = w.index if common is None else common.intersection(w.index)
    common = common.sort_values()
    if len(common) < MIN_WEEKS:
        return {}, pd.Series(dtype=float), data_flags

    aligned = pd.DataFrame({slug: w.reindex(common) for slug, w in weekly_map.items()})
    # 시작점 1로 정규화 후 평균 = 동일가중 합성지수
    benchmark = (aligned / aligned.iloc[0]).mean(axis=1)

    out: dict[str, pd.DataFrame] = {}
    for slug in aligned.columns:
        norm = aligned[slug] / aligned[slug].iloc[0]
        rs = 100.0 * norm / benchmark
        ratio = 100.0 * rs / rs.rolling(SMA_WEEKS).mean()
        mom = 100.0 + ratio.pct_change(ROC_WEEKS) * 100.0
        xy = pd.DataFrame({"x": ratio, "y": mom}).dropna()
        if len(xy) >= TAIL_WEEKS:
            out[slug] = xy
    return out, benchmark, data_flags


def compute_rrg(daily_closes: dict[str, pd.Series], now: dt.datetime) -> dict:
    """{slug: 일간 종가 Series} → RRG 결과.

    반환: {
      "as_of": "YYYY-MM-DD",            # 마지막 확정 금요일
      "benchmark": "sector-eq",         # 섹터 동일가중 합성지수
      "sectors": {slug: {x, y, quadrant, quadrant_ko, heading, tail[], comment, ...}},
      "data_flags": {slug: [날짜, ...]},  # 이상치 봉 (좌표 계산에서 제외됨)
    }
    """
    cutoff = last_confirmed_friday(now)
    xy_map, _benchmark, data_flags = weekly_xy(daily_closes, cutoff)

    sectors: dict[str, dict] = {}
    for slug, xy in xy_map.items():
        tail_df = xy.iloc[-TAIL_WEEKS:]
        tail = [{"d": d.strftime("%m-%d"), "x": round(r.x, 2), "y": round(r.y, 2)}
                for d, r in tail_df.iterrows()]
        x, y = tail[-1]["x"], tail[-1]["y"]
        px, py = tail[-2]["x"], tail[-2]["y"]
        quadrant, quadrant_ko = _quadrant(x, y)
        prev_q, prev_q_ko = _quadrant(px, py)
        heading = _heading(x - px, y - py)
        # heading 지속 주 수 (같은 방향이 몇 주째인가)
        streak = 1
        for i in range(len(tail) - 2, 0, -1):
            h = _heading(tail[i]["x"] - tail[i - 1]["x"], tail[i]["y"] - tail[i - 1]["y"])
            if h != heading:
                break
            streak += 1
        sectors[slug] = {
            "x": x, "y": y,
            "quadrant": quadrant, "quadrant_ko": quadrant_ko,
            "prev_quadrant": prev_q,
            "heading": heading, "heading_weeks": streak,
            "tail": tail,
            "comment": _comment(quadrant_ko, prev_q_ko, quadrant != prev_q, heading, streak),
        }

    return {"as_of": cutoff.isoformat(), "benchmark": "sector-eq",
            "sectors": sectors, "data_flags": data_flags}


# 판단어("매수/매도/관심/비중" 등)는 쓰지 않는다 — 상태 서술만 (Phase 3, 리플레이 검증 전).
_HEADING_KO = {"NE": "우상향", "SE": "우하향", "SW": "좌하향", "NW": "좌상향",
               "N": "상승", "S": "하락", "E": "우측 이동", "W": "좌측 이동", "flat": "정체"}


def build_insight(rrg: dict, names: dict[str, str], generated_at: str) -> dict:
    """장마감 시 1회 생성하는 규칙 기반 로테이션 인사이트 (장중 스캔은 직전 값 유지).

    관찰 언어만 사용한다 — 리플레이 검증(Phase 4)에서 사분면 진입 단독의 초과수익이
    확인되지 않았으므로 "매수/매도/비중" 등 판단어는 금지.
    """
    secs = rrg.get("sectors", {})
    nm = lambda slug: names.get(slug, slug)

    def pick(pred):
        return sorted((s for s in secs.items() if pred(s[1])),
                      key=lambda kv: -kv[1]["heading_weeks"])

    lines: list[str] = []
    leading_n = sum(1 for v in secs.values() if v["quadrant"] == "leading")
    transitions = [(k, v) for k, v in secs.items() if v["quadrant"] != v["prev_quadrant"]]
    lines.append(f"주도 사분면 {leading_n}개 섹터, 이번 주 사분면 전환 {len(transitions)}건.")

    new_lead = pick(lambda v: v["quadrant"] == "leading" and v["prev_quadrant"] == "improving")
    if new_lead:
        d = ", ".join(f"{nm(k)}({v['heading_weeks']}주 연속 우상향)" if v["heading"] == "NE" and v["heading_weeks"] >= 2
                      else nm(k) for k, v in new_lead)
        lines.append(f"부상→주도 진입: {d} — 신규 리더십 후보, 지속 여부 관찰.")

    exits = pick(lambda v: v["prev_quadrant"] == "leading" and v["quadrant"] in ("weakening", "lagging"))
    if exits:
        lines.append(f"주도 이탈: {', '.join(nm(k) for k, _ in exits)} — 상대 모멘텀 둔화 진행.")

    rising = pick(lambda v: v["quadrant"] == "improving" and v["heading"] == "NE" and v["heading_weeks"] >= 3)
    if rising:
        d = ", ".join(f"{nm(k)}({v['heading_weeks']}주 연속 우상향)" for k, v in rising)
        lines.append(f"부상 지속: {d} — 소외→부상 회전 진행 중, 주도 진입 여부가 관찰 포인트.")

    weakest = pick(lambda v: v["quadrant"] == "lagging" and v["heading"] == "SW" and v["heading_weeks"] >= 3)
    if weakest:
        d = ", ".join(f"{nm(k)}({v['heading_weeks']}주 연속 좌하향)" for k, v in weakest)
        lines.append(f"소외 지속: {d} — 상대 약세 추세 유지.")

    flags = rrg.get("data_flags", {})
    if flags:
        lines.append(f"데이터 이상치 필터 적용 섹터 {len(flags)}개({', '.join(nm(k) for k in flags)}) — 해당 봉은 좌표 계산에서 제외됨.")

    return {"generated_at": generated_at, "as_of": rrg.get("as_of"), "lines": lines[:6]}


def _comment(q_ko: str, prev_ko: str, transitioned: bool, heading: str, streak: int) -> str:
    h_ko = _HEADING_KO.get(heading, heading)
    if transitioned:
        base = f"{prev_ko}→{q_ko} 진입"
    else:
        base = f"{q_ko} 유지"
    if streak >= 2:
        return f"{base}, {streak}주 연속 {h_ko}"
    return f"{base}, 이번 주 {h_ko}"
