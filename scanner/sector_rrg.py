"""섹터 RRG(Relative Rotation Graph) 근사 계산 — 순수 함수 모듈.

방법론 (계획안 v1 → 일간 개정):
  - 일간 종가 기준, 매 거래일 장마감마다 좌표 갱신 (장중 스캔은 직전 값 유지 — run_scan 담당).
    랭킹 표·인사이트가 매일 갱신되는데 맵만 주 1회면 시점이 어긋난다는 피드백 반영.
  - 급변 방지는 갱신 주기가 아니라 스무딩이 담당: 주간 방식과 등가인
    60거래일(≈12주) SMA + 20거래일(≈4주) ROC.
  - 벤치마크 = 섹터 대표 ETF 동일가중 합성지수 (섹터 유니버스 내부 상대강도).
  - RS          = 100 × 섹터 / 벤치마크
  - RS-Ratio    = 100 × RS / SMA(RS, 60일)          (X축)
  - RS-Momentum = 100 + RS-Ratio의 20일 ROC(%)       (Y축)
  - tail = 5거래일(1주) 간격 8개 점 — 회전 궤적 해석 단위는 주간 유지.
  z-score 정규화는 쓰지 않는다(고정 스케일 근사) — 보유 히스토리로 롤링 창을 감당할
  수 없고, 짧은 창은 "좌표 급변" 문제를 재생산하기 때문.

데이터 품질 (Phase 0): |일간 수익률| > OUTLIER_PCT 봉은 데이터 오류로 간주,
해당 종가를 직전 종가로 대체하고 data_flags에 기록한다.

이 모듈은 API 호출 없이 {slug: 종가 Series}만 받는다 — run_scan(운영),
validate_prices(검증), replay_rrg(리플레이)가 공유한다.
"""

import datetime as dt

import pandas as pd

OUTLIER_PCT = 15.0        # 일간 수익률 이상치 임계(%) — ETF 기준
SMA_DAYS = 60             # RS-Ratio 스무딩 창 (≈12주)
ROC_DAYS = 20             # RS-Momentum 변화율 창 (≈4주)
TAIL_STEP = 5             # tail 샘플 간격 (거래일) — 1주
TAIL_POINTS = 8           # 화면에 표시할 궤적 점 수 (8주)
MIN_DAYS = SMA_DAYS + ROC_DAYS + TAIL_STEP * TAIL_POINTS  # 산출에 필요한 최소 일봉 수

QUADRANTS = {
    ("hi", "hi"): ("leading", "주도"),
    ("hi", "lo"): ("weakening", "약화"),
    ("lo", "lo"): ("lagging", "소외"),
    ("lo", "hi"): ("improving", "부상"),
}


MEDIAN_WINDOW = 7  # 국소 중앙값 창 (전후 3일 + 자신)

# Y축 단독 신호 판별 — 2026-08 2차전지 사례(부상·4주 NE인데 x 91대 제자리 + 5일 -7.6%):
# Y(RS-Momentum)는 하락 속도 둔화만으로 오르므로, X가 깊은 소외에 못박힌 채 Y만
# 끌어올린 "우상향"은 상대 지위 개선이 아니라 낙폭 둔화의 통계적 반등이다.
Y_ONLY_X_MAX = 95.0       # 깊은 소외 판정 (x < 95)
Y_ONLY_X_MOVE = 1.5       # 앵커 구간(≈7주) X 이동이 이 미만이면 "제자리"
Y_ONLY_MIN_WEEKS = 3      # 연속 상방 주 수

# 절대가격 게이트 — RRG(상대강도)는 "언제 살 것인가"에 답하지 못하므로,
# 섹터 ETF 자체의 절대 추세 조건을 병기한다 (표시용 체크리스트 — 백테스트 검증 전).
GATE_MA_DAYS = 60         # 추세 기준선
GATE_D5_MIN = -5.0        # 최근 5일 수익률 하한(%) — 급락 중 진입 배제

SIGNAL_LAG_NOTE = "좌표는 60일 SMA·20일 ROC 스무딩 — 최근 2~4주 급변은 아직 미반영"


def clean_daily(closes: pd.Series) -> tuple[pd.Series, list[str]]:
    """데이터 오류 봉 필터 — '일간 수익률 크기'가 아니라 '전후 7일 중앙값 대비 이탈'로 판정.

    변동성 장세의 진짜 ±15%+ 급등락은 새 가격 수준이 유지되어 중앙값이 따라가므로
    보존된다. 반면 소스 오류는 (a) 하루짜리 왕복 스파이크든 (b) 며칠짜리 잘못된
    구간이든 국소 중앙값에서 크게 벗어나므로 잡힌다 — 실측 사례: 반도체 ETF가
    3일간 -25% 잘못된 가격 후 +29% '복귀'한 구간(오류는 하락 3일 쪽).
    한계: 극단적 V자 폭락의 최저점 1~2봉이 오탐될 수 있음 — 배지로 노출되므로 검증 가능.
    마지막 2봉은 미래 창이 없어 판정 불가 — 보수적으로 보존(다음 날 재판정).
    """
    closes = closes.dropna()
    if len(closes) < MEDIAN_WINDOW + 2:
        return closes, []
    med = closes.rolling(MEDIAN_WINDOW, center=True, min_periods=4).median()
    dev = (closes / med - 1).abs() * 100
    bad = dev > OUTLIER_PCT
    bad.iloc[-2:] = False  # 판정 유보 (미래 데이터 부족)
    flags = [d.strftime("%Y-%m-%d") for d in closes.index[bad]]
    if flags:
        cleaned = closes.copy()
        cleaned[bad] = float("nan")
        cleaned = cleaned.ffill().bfill()
        return cleaned, flags
    return closes, flags


def last_confirmed_close(now: dt.datetime) -> dt.date:
    """마지막 '확정된' 종가 날짜 — 당일 15:30(장마감) 이후면 오늘, 아니면 전일 이전.
    주말은 금요일로 되돌린다 (휴장일 정밀 판정은 하지 않음 — 데이터에 없는 날짜는 자연 제외)."""
    d = now.date()
    if (now.hour, now.minute) < (15, 30):
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:  # 토(5)/일(6) → 금요일
        d -= dt.timedelta(days=1)
    return d


def _heading(dx: float, dy: float) -> str:
    if dx == 0 and dy == 0:
        return "flat"
    ns = "N" if dy > 0 else ("S" if dy < 0 else "")
    ew = "E" if dx > 0 else ("W" if dx < 0 else "")
    return (ns + ew) or "flat"


def _quadrant(x: float, y: float) -> tuple[str, str]:
    return QUADRANTS[("hi" if x >= 100 else "lo", "hi" if y >= 100 else "lo")]


def _y_only(quadrant: str, heading: str, streak: int, x: float, x_move: float) -> bool:
    """"N주 연속 우상향"이 Y축 혼자 만든 신호인지 판별 (순수 함수 — 단위 테스트 대상).

    조건: 부상 사분면 + 상방 heading(N 포함) 연속 ≥3주인데,
    X가 깊은 소외(<95)에서 앵커 구간 내 사실상 제자리(|이동| < 1.5)."""
    return (quadrant == "improving" and "N" in heading and streak >= Y_ONLY_MIN_WEEKS
            and x < Y_ONLY_X_MAX and abs(x_move) < Y_ONLY_X_MOVE)


def _entry_gate(closes: pd.Series, quadrant: str, heading: str, y_only: bool) -> dict:
    """절대가격 게이트 체크리스트 — 관찰 서술만, 판단어 없음 (백테스트 검증 전).

    trend_ok: RRG상 부상/주도 + 상방 heading + Y축 단독 아님 (상대강도 조건)
    ma60_ok : 종가 > 60일 이동평균 (절대 추세 조건)
    d5_ok   : 최근 5일 수익률 > -5% (급락 중 아님)
    """
    ma_ok = d5_ok = None
    d5 = None
    if len(closes) >= GATE_MA_DAYS + 1:
        cur = float(closes.iloc[-1])
        ma_ok = cur > float(closes.iloc[-GATE_MA_DAYS:].mean())
        d5 = (cur / float(closes.iloc[-6]) - 1) * 100
        d5_ok = d5 > GATE_D5_MIN
    trend_ok = quadrant in ("improving", "leading") and "N" in heading and not y_only
    checks = [trend_ok, ma_ok, d5_ok]
    return {
        "trend_ok": trend_ok, "ma60_ok": ma_ok, "d5_ok": d5_ok,
        "d5": None if d5 is None else round(d5, 2),
        "passed": all(c is True for c in checks),
        "n_ok": sum(1 for c in checks if c is True),
    }


def daily_xy(daily_closes: dict[str, pd.Series], cutoff: dt.date,
             sma_days: int = SMA_DAYS, roc_days: int = ROC_DAYS
             ) -> tuple[dict[str, pd.DataFrame], pd.Series, dict[str, list[str]]]:
    """이상치 정제 → cutoff까지의 일봉 → RS-Ratio(x)/RS-Momentum(y) 일간 시계열.

    반환: ({slug: DataFrame[x, y]}, 벤치마크(동일가중, 시작=1) 일간 시계열, data_flags)
    compute_rrg(운영 스냅샷)과 replay_rrg(Phase 4 검증)가 공유한다.
    """
    cleaned_map: dict[str, pd.Series] = {}
    data_flags: dict[str, list[str]] = {}
    for slug, closes in daily_closes.items():
        cleaned, flags = clean_daily(closes)
        if flags:
            data_flags[slug] = flags
        cleaned = cleaned[cleaned.index.date <= cutoff]
        if len(cleaned) >= MIN_DAYS:
            cleaned_map[slug] = cleaned

    if len(cleaned_map) < 4:  # 섹터가 너무 적으면 상대강도 의미 없음
        return {}, pd.Series(dtype=float), data_flags

    # 공통 일간 인덱스 (모든 섹터가 존재하는 날만) — 동일가중 합성의 전제
    common = None
    for s in cleaned_map.values():
        common = s.index if common is None else common.intersection(s.index)
    common = common.sort_values()
    if len(common) < MIN_DAYS:
        return {}, pd.Series(dtype=float), data_flags

    aligned = pd.DataFrame({slug: s.reindex(common) for slug, s in cleaned_map.items()})
    # 시작점 1로 정규화 후 평균 = 동일가중 합성지수
    benchmark = (aligned / aligned.iloc[0]).mean(axis=1)

    out: dict[str, pd.DataFrame] = {}
    for slug in aligned.columns:
        norm = aligned[slug] / aligned[slug].iloc[0]
        rs = 100.0 * norm / benchmark
        ratio = 100.0 * rs / rs.rolling(sma_days).mean()
        mom = 100.0 + ratio.pct_change(roc_days) * 100.0
        xy = pd.DataFrame({"x": ratio, "y": mom}).dropna()
        if len(xy) >= TAIL_STEP * TAIL_POINTS:
            out[slug] = xy
    return out, benchmark, data_flags


def compute_rrg(daily_closes: dict[str, pd.Series], now: dt.datetime) -> dict:
    """{slug: 일간 종가 Series} → RRG 결과.

    반환: {
      "as_of": "YYYY-MM-DD",            # 마지막 확정 종가 날짜 (매 거래일 장마감 갱신)
      "benchmark": "sector-eq",         # 섹터 동일가중 합성지수
      "sectors": {slug: {x, y, quadrant, quadrant_ko, heading, tail[], comment, ...}},
      "data_flags": {slug: [날짜, ...]},  # 이상치 봉 (좌표 계산에서 제외됨)
    }
    """
    cutoff = last_confirmed_close(now)
    xy_map, _benchmark, data_flags = daily_xy(daily_closes, cutoff)

    sectors: dict[str, dict] = {}
    as_of = cutoff.isoformat()
    for slug, xy in xy_map.items():
        # tail 앵커 = 완결된 주(W-FRI)의 마지막 거래일 — 금요일 고정 앵커라서
        # 어느 요일에 갱신해도 heading·"N주 연속" 카운트가 흔들리지 않는다(위상 안정).
        weekly = xy.resample("W-FRI").last().dropna()
        completed = weekly[weekly.index.date <= cutoff]
        if len(completed) < 2:
            continue
        anchors = completed.iloc[-(TAIL_POINTS - 1):]
        tail = [{"d": d.strftime("%m-%d"), "x": round(r.x, 2), "y": round(r.y, 2)}
                for d, r in anchors.iterrows()]
        # 현재점(최신 확정 종가)은 주중이면 앵커와 별도로 마지막에 추가
        cur_d = xy.index[-1]
        x, y = round(float(xy["x"].iloc[-1]), 2), round(float(xy["y"].iloc[-1]), 2)
        if cur_d.strftime("%m-%d") != tail[-1]["d"]:
            tail.append({"d": cur_d.strftime("%m-%d"), "x": x, "y": y})
        as_of = cur_d.strftime("%Y-%m-%d")
        # heading·연속 카운트는 주간 앵커 점들로만 계산 (현재점 제외 → 요일 무관 안정)
        ax = [{"x": round(r.x, 2), "y": round(r.y, 2)} for _, r in anchors.iterrows()]
        heading = _heading(ax[-1]["x"] - ax[-2]["x"], ax[-1]["y"] - ax[-2]["y"])
        streak = 1
        for i in range(len(ax) - 2, 0, -1):
            h = _heading(ax[i]["x"] - ax[i - 1]["x"], ax[i]["y"] - ax[i - 1]["y"])
            if h != heading:
                break
            streak += 1
        quadrant, quadrant_ko = _quadrant(x, y)
        # 전이 비교 기준 = 현재점 직전의 주간 앵커 (현재점이 금요일 앵커 자신이면 그 전 앵커)
        prev_pt = ax[-2] if cur_d.date() == anchors.index[-1].date() else ax[-1]
        prev_q, prev_q_ko = _quadrant(prev_pt["x"], prev_pt["y"])
        x_move = round(ax[-1]["x"] - ax[0]["x"], 2)  # 앵커 구간(≈7주) X 순이동
        y_only = _y_only(quadrant, heading, streak, x, x_move)
        closes_g, _ = clean_daily(daily_closes[slug])
        closes_g = closes_g[closes_g.index.date <= cutoff]
        gate = _entry_gate(closes_g, quadrant, heading, y_only)
        comment = _comment(quadrant_ko, prev_q_ko, quadrant != prev_q, heading, streak)
        if y_only:
            comment += " · Y축 단독(상대 지위 개선 없는 낙폭 둔화)"
        sectors[slug] = {
            "x": x, "y": y,
            "quadrant": quadrant, "quadrant_ko": quadrant_ko,
            "prev_quadrant": prev_q,
            "heading": heading, "heading_weeks": streak,
            "x_move": x_move, "y_only": y_only,
            "gate": gate,
            "tail": tail,
            "comment": comment,
        }

    return {"as_of": as_of, "benchmark": "sector-eq",
            "signal_lag": SIGNAL_LAG_NOTE,
            "sectors": sectors, "data_flags": data_flags}


def build_insight(rrg: dict, names: dict[str, str], generated_at: str,
                  d5: dict[str, float] | None = None, kospi: dict | None = None,
                  stats: dict | None = None) -> dict:
    """장마감 시 1회 생성하는 규칙 기반 로테이션 인사이트 (장중 스캔은 직전 값 유지).

    관찰 언어만 사용한다 — 리플레이·백테스트에서 사분면 전환 추종의 초과수익이
    확인되지 않았으므로 "매수/매도/비중" 등 판단어는 금지. 대신 과거 통계를 병기해
    각 서술이 실제로 어떤 의미였는지 스스로 판단할 근거를 준다.

    d5: {slug: 최근 5일 수익률 %} (선택) / kospi: fetch_kospi_status 결과 (선택)
    stats: 리플레이 전이 통계 (선택 — rrg["stats"]와 동일 구조)
    """
    secs = rrg.get("sectors", {})
    nm = lambda slug: names.get(slug, slug)

    def pick(pred):
        return sorted((s for s in secs.items() if pred(s[1])),
                      key=lambda kv: -kv[1]["heading_weeks"])

    def stat_note(key):
        s = ((stats or {}).get("transitions") or {}).get(key) or {}
        if s.get("fwd8w_mean") is None:
            return ""
        return f" [과거 이 전이 후 8주 평균 {s['fwd8w_mean']:+.1f}%p·승률 {s['fwd8w_win']}%]"

    lines: list[str] = []

    # ① 시장 맥락 + 사분면 구도
    leading_n = sum(1 for v in secs.values() if v["quadrant"] == "leading")
    transitions = [(k, v) for k, v in secs.items() if v["quadrant"] != v["prev_quadrant"]]
    ctx = ""
    if kospi and kospi.get("above_ma200") is not None:
        ctx = ("KOSPI 200일선 위 — 상대 강세 = 실제 강세로 읽을 수 있는 국면. "
               if kospi["above_ma200"] else
               "KOSPI 200일선 아래 — 지금 '주도'는 덜 빠지는 섹터라는 뜻이므로 절대수익 기대는 금물. ")
    lines.append(f"{ctx}주도 {leading_n}개 / 전체 {len(secs)}개 섹터, 최근 1주 사분면 전환 {len(transitions)}건.")

    # ② 신규 진입 (과거 통계 병기 — "쫓아가기 전에 확인" 근거)
    new_lead = pick(lambda v: v["quadrant"] == "leading" and v["prev_quadrant"] == "improving")
    if new_lead:
        d = ", ".join(f"{nm(k)}({v['heading_weeks']}주 연속 우상향)" if v["heading"] == "NE" and v["heading_weeks"] >= 2
                      else nm(k) for k, v in new_lead)
        lines.append(f"부상→주도 진입: {d} — 신규 리더십 후보.{stat_note('improving>leading')} "
                     f"과거엔 진입 직후 추격이 불리했던 전이 — 눌림 후 재확인이 관찰 포인트.")

    # ③ 주도 이탈
    exits = pick(lambda v: v["prev_quadrant"] == "leading" and v["quadrant"] in ("weakening", "lagging"))
    if exits:
        lines.append(f"주도 이탈: {', '.join(nm(k) for k, _ in exits)} — 상대 모멘텀 둔화 진행.{stat_note('leading>weakening')}")

    # ④ 경계 근접 관찰 포인트 — 다음 주 전환 후보를 미리 짚는다
    near_lead = [(k, v) for k, v in secs.items()
                 if v["quadrant"] == "improving" and 100 - 1.5 <= v["x"] < 100 and v["heading"] in ("NE", "E")]
    near_exit = [(k, v) for k, v in secs.items()
                 if v["quadrant"] == "leading" and 100 <= v["x"] < 100 + 1.5 and v["heading"] in ("SW", "W", "S")]
    if near_lead or near_exit:
        parts = []
        if near_lead:
            d_near = ", ".join(f"{nm(k)}(x={v['x']:.1f})" for k, v in near_lead)
            parts.append(f"주도 진입 직전: {d_near}")
        if near_exit:
            parts.append(f"주도 이탈 임박: {', '.join(nm(k) for k, _ in near_exit)}")
        lines.append("경계 관찰: " + " / ".join(parts) + " — 다음 주 전환 가능성이 높은 자리.")

    # ⑤ 회전 진행/약세 지속
    rising = pick(lambda v: v["quadrant"] == "improving" and v["heading"] == "NE"
                  and v["heading_weeks"] >= 3 and not v.get("y_only"))
    if rising:
        d = ", ".join(f"{nm(k)}({v['heading_weeks']}주 연속 우상향)" for k, v in rising)
        lines.append(f"부상 지속: {d} — 소외→부상 회전 진행 중, 주도 진입 여부가 관찰 포인트.")
    # ⑤-b Y축 단독 신호 — "N주 연속 우상향"이 X 제자리 + Y(낙폭 둔화) 혼자 만든 경우
    y_only_secs = pick(lambda v: v.get("y_only"))
    if y_only_secs:
        d = ", ".join(f"{nm(k)}(x {v['x']:.1f} 제자리, {v['heading_weeks']}주 우상향)"
                      for k, v in y_only_secs)
        lines.append(f"Y축 단독 신호: {d} — 상대 지위 개선 없는 낙폭 둔화. "
                     f"X축 동반 상승 전까지는 회전으로 보기 어려움.")
    weakest = pick(lambda v: v["quadrant"] == "lagging" and v["heading"] == "SW" and v["heading_weeks"] >= 3)
    if weakest:
        d = ", ".join(f"{nm(k)}({v['heading_weeks']}주 연속 좌하향)" for k, v in weakest)
        lines.append(f"소외 지속: {d} — 상대 약세 추세 유지.{stat_note('weakening>lagging')}")

    # ⑥ 4주 순위 급변 — 랭킹 테이블의 ▲▼를 문장으로
    def rank_map(get):
        order = sorted(secs, key=get, reverse=True)
        return {k: i + 1 for i, k in enumerate(order)}
    now_r = rank_map(lambda k: secs[k]["x"])
    old_r = rank_map(lambda k: secs[k]["tail"][0]["x"] if secs[k]["tail"] else secs[k]["x"])
    movers = sorted(((k, old_r[k] - now_r[k]) for k in secs), key=lambda t: -abs(t[1]))
    big = [(k, d) for k, d in movers if abs(d) >= 4][:3]
    if big:
        d = ", ".join(f"{nm(k)} {'+' if d_ > 0 else ''}{d_}계단" for k, d_ in big)
        lines.append(f"순위 급변(약 {len(secs[list(secs)[0]]['tail'])}주 전 대비): {d} — 로테이션의 실제 이동 경로.")

    # ⑦ 맵 위치와 단기 수익률의 괴리 — 추세와 단기 흐름이 반대인 섹터.
    # 사분면 무관: 상방 heading ≥3주 + 5일 급락(2026-08 2차전지: 부상·4주 NE + 5일 -7.6%)도 포함.
    if d5:
        div = [(k, v, d5[k]) for k, v in secs.items() if k in d5
               and ((v["quadrant"] == "leading" and d5[k] <= -5)
                    or (v["quadrant"] == "lagging" and d5[k] >= 5)
                    or ("N" in v["heading"] and v["heading_weeks"] >= 3 and d5[k] <= -5))]
        if div:
            d = ", ".join(f"{nm(k)}({v['quadrant_ko']} 사분면인데 5일 {r:+.1f}%)"
                          for k, v, r in div[:3])
            lines.append(f"추세·단기 괴리: {d} — 좌표는 스무딩으로 2~4주 후행하므로 "
                         f"급락이 아직 미반영. 추세 전환인지 일시 반락인지 다음 주 확인 필요.")

    # ⑧ 데이터 품질
    flags = rrg.get("data_flags", {})
    if flags:
        lines.append(f"데이터 이상치 필터 적용 {len(flags)}개 섹터({', '.join(nm(k) for k in flags)}) — 해당 봉은 좌표 계산에서 제외됨.")

    return {"generated_at": generated_at, "as_of": rrg.get("as_of"), "lines": lines[:10]}


# 판단어("매수/매도/관심/비중" 등)는 쓰지 않는다 — 상태 서술만 (Phase 3, 리플레이 검증 전).
_HEADING_KO = {"NE": "우상향", "SE": "우하향", "SW": "좌하향", "NW": "좌상향",
               "N": "상승", "S": "하락", "E": "우측 이동", "W": "좌측 이동", "flat": "정체"}


def _comment(q_ko: str, prev_ko: str, transitioned: bool, heading: str, streak: int) -> str:
    h_ko = _HEADING_KO.get(heading, heading)
    if transitioned:
        base = f"{prev_ko}→{q_ko} 진입"
    else:
        base = f"{q_ko} 유지"
    if streak >= 2:
        return f"{base}, {streak}주 연속 {h_ko}"
    return f"{base}, 이번 주 {h_ko}"
