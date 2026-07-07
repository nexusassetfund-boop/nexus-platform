"""
추세추종 스테이지 감지 엔진

Stage 1: 1차 상승 — MA 정배열 + 저점 대비 50%↑ + RS≥70
Stage 2: 조정 베이스 — VCP (변동성·거래량 점진 축소)
Stage 3: 돌파 재상승 — 전고점 돌파 + 거래량 200%↑ (매수 타점)

추가: MTT 필터, 클라이맥스 감지, 포지션 사이징 (2% 룰)
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class StageResult:
    ticker: str
    name: str = ""
    stage: Optional[int] = None          # 1, 2, 3 또는 None(해당없음)
    stage_label: str = ""                 # "Stage 1 - 1차 상승" 등
    confidence: float = 0.0              # 신뢰도 0~100

    # MTT 필터
    mtt_pass: bool = False
    price_above_ma150: bool = False
    price_above_ma200: bool = False
    ma150_above_ma200: bool = False
    ma200_rising: bool = False
    rs_rank: float = 0.0
    rs_momentum: float = 0.0              # RS 변화량 (현재 - 이전)
    rs_new_high: bool = False             # RS 신고점 여부

    # 섹터
    sector: str = ""
    sector_rs: float = 0.0               # 섹터 평균 RS

    # MA 정배열
    ma_aligned: bool = False             # 5 > 20 > 60 > 120

    # 가격 정보
    current_price: float = 0.0
    ma5: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    ma120: float = 0.0
    ma150: float = 0.0
    ma200: float = 0.0

    # Stage 1 지표
    rise_from_low_pct: float = 0.0       # 60일 저점 대비 상승률

    # Stage 2 지표 (VCP)
    vcp_detected: bool = False
    contractions: int = 0                # 변동성 축소 횟수
    vol_drying: bool = False             # 거래량 마름
    range_contraction_pct: float = 0.0   # 최근 변동폭 축소율

    # Stage 3 지표
    breakout_detected: bool = False
    near_high: bool = False              # 전고점 근처
    volume_surge_ratio: float = 0.0      # 거래량 폭증 배수
    gap_up: bool = False                 # 갭상승 여부

    # 클라이맥스 경고
    climax_warning: bool = False

    # 포지션 사이징 (2% 룰)
    suggested_stop_pct: float = -10.0
    position_size_pct: float = 0.0       # 총 자산 대비 비중

    # 메타
    scan_date: str = ""
    signals: list = field(default_factory=list)  # 감지된 시그널 목록

    def to_dict(self) -> dict:
        return asdict(self)


def _sma(series: pd.Series, period: int) -> float:
    """단순이동평균 마지막 값"""
    if len(series) < period:
        return float("nan")
    return float(series.iloc[-period:].mean())


def _sma_series(series: pd.Series, period: int) -> pd.Series:
    """단순이동평균 시리즈"""
    return series.rolling(window=period, min_periods=period).mean()


def _detect_vcp(df: pd.DataFrame, params: dict) -> dict:
    """
    VCP (Volatility Contraction Pattern) 감지

    조정 구간에서 변동폭이 점진적으로 줄어드는 패턴.
    최소 2번의 축소(contraction)가 필요.
    """
    if len(df) < 60:
        return {"detected": False, "contractions": 0, "range_contraction_pct": 0}

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values

    # 최근 60일을 3구간으로 나눠서 변동폭 비교
    seg_len = 20
    ranges = []
    for i in range(3):
        start = -(3 - i) * seg_len
        end = start + seg_len if start + seg_len != 0 else None
        seg_high = highs[start:end] if end else highs[start:]
        seg_low = lows[start:end] if end else lows[start:]
        if len(seg_high) < seg_len:
            continue
        rng = (seg_high.max() - seg_low.min()) / seg_low.min() * 100 if seg_low.min() > 0 else 0
        ranges.append(rng)

    if len(ranges) < 3:
        return {"detected": False, "contractions": 0, "range_contraction_pct": 0}

    # 축소 횟수 카운트
    threshold = 1.0 - params.get("vcp_contraction_threshold", 0.15)  # 기본 15% 축소
    contractions = 0
    for i in range(1, len(ranges)):
        if ranges[i] < ranges[i - 1] * threshold:
            contractions += 1

    # 전체 축소율
    contraction_pct = 0
    if ranges[0] > 0:
        contraction_pct = round((1 - ranges[-1] / ranges[0]) * 100, 1)

    # 거래량도 줄어드는지 확인
    volumes = df["volume"].values
    vol_first = np.mean(volumes[-60:-40]) if len(volumes) >= 60 else 0
    vol_last = np.mean(volumes[-20:]) if len(volumes) >= 20 else 0
    vol_drying = vol_last < vol_first * params.get("volume_dry_ratio", 0.5) if vol_first > 0 else False

    detected = contractions >= params.get("vcp_contractions_min", 2)

    return {
        "detected": detected,
        "contractions": contractions,
        "range_contraction_pct": contraction_pct,
        "vol_drying": vol_drying,
        "ranges": [round(r, 1) for r in ranges],
    }


def _detect_breakout(df: pd.DataFrame, params: dict) -> dict:
    """
    돌파 감지 — Stage 3 진입 시그널

    조건:
    1) 최근 가격이 직전 20~60일 고점 돌파
    2) 거래량이 20일 평균의 200% 이상
    3) (옵션) 갭상승
    """
    if len(df) < 60:
        return {"detected": False, "volume_surge_ratio": 0, "near_high": False, "gap_up": False}

    closes = df["close"].values
    highs = df["high"].values
    volumes = df["volume"].values
    opens = df["open"].values

    cur = closes[-1]
    today_vol = volumes[-1]

    # 전고점: 최근 5일 제외한 20~60일 구간의 최고가
    lookback_high = highs[-60:-5].max() if len(highs) >= 60 else highs[:-5].max()
    near_high = cur >= lookback_high * 0.97
    broke_high = cur > lookback_high

    # 거래량 폭증
    avg_vol_20 = np.mean(volumes[-25:-5]) if len(volumes) >= 25 else np.mean(volumes[:-5])
    surge_ratio = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0

    vol_surge = surge_ratio >= params.get("volume_surge_ratio", 2.0)

    # 갭상승: 오늘 시가 > 어제 고가
    gap_up = False
    if len(opens) >= 2 and len(highs) >= 2:
        gap_up = opens[-1] > highs[-2]

    detected = broke_high and vol_surge

    return {
        "detected": detected,
        "volume_surge_ratio": round(surge_ratio, 2),
        "near_high": near_high,
        "gap_up": gap_up,
        "lookback_high": float(lookback_high),
    }


def _detect_climax(df: pd.DataFrame, params: dict) -> bool:
    """
    클라이맥스 경고 감지

    조건:
    - 거래량이 20일 평균의 3배 이상 + 윗꼬리 음봉
    - 또는 일일 -10% 이상 하락
    """
    if len(df) < 20:
        return False

    row = df.iloc[-1]
    avg_vol = df["volume"].iloc[-20:].mean()
    vol_ratio = row["volume"] / avg_vol if avg_vol > 0 else 0

    # 윗꼬리 음봉 + 역대급 거래량
    is_bearish_candle = row["close"] < row["open"]
    upper_wick = row["high"] - max(row["open"], row["close"])
    body = abs(row["close"] - row["open"])
    long_upper_wick = upper_wick > body * 1.5 if body > 0 else False

    climax_vol = vol_ratio >= params.get("climax_volume_ratio", 3.0)

    # 장대 음봉
    if len(df) >= 2:
        prev_close = df["close"].iloc[-2]
        daily_drop = (row["close"] - prev_close) / prev_close * 100 if prev_close > 0 else 0
        big_drop = daily_drop <= params.get("climax_drop_pct", -10)
    else:
        big_drop = False

    return (climax_vol and is_bearish_candle and long_upper_wick) or big_drop


def analyze_stock(
    ticker: str,
    name: str,
    df: pd.DataFrame,
    rs_rank: float,
    params: dict,
    total_capital: float = 100_000_000,
    rs_momentum: float = 0.0,
    rs_new_high: bool = False,
) -> StageResult:
    """
    단일 종목 스테이지 분석

    Returns: StageResult
    """
    result = StageResult(ticker=ticker, name=name)
    result.scan_date = str(df.index[-1].date()) if len(df) > 0 else ""

    if len(df) < 60:
        result.signals.append("데이터 부족 (60일 미만)")
        return result

    closes = df["close"]
    cur = float(closes.iloc[-1])
    result.current_price = cur

    # ── 이동평균 계산 ──
    result.ma5 = _sma(closes, 5)
    result.ma20 = _sma(closes, 20)
    result.ma60 = _sma(closes, 60)
    result.ma120 = _sma(closes, 120) if len(closes) >= 120 else 0
    result.ma150 = _sma(closes, 150) if len(closes) >= 150 else 0
    result.ma200 = _sma(closes, 200) if len(closes) >= 200 else 0

    # ── MTT 필터 ──
    result.rs_rank = rs_rank
    result.rs_momentum = rs_momentum
    result.rs_new_high = rs_new_high
    result.price_above_ma150 = cur > result.ma150 > 0
    result.price_above_ma200 = cur > result.ma200 > 0

    if result.ma150 > 0 and result.ma200 > 0:
        result.ma150_above_ma200 = result.ma150 > result.ma200

    # MA200 상승 추세 (최근 20일 기울기)
    if len(closes) >= 220:
        ma200_series = _sma_series(closes, 200)
        ma200_recent = ma200_series.iloc[-20:]
        if len(ma200_recent.dropna()) >= 2:
            result.ma200_rising = float(ma200_recent.iloc[-1]) > float(ma200_recent.iloc[0])

    result.mtt_pass = all([
        result.price_above_ma150,
        result.price_above_ma200,
        result.ma150_above_ma200,
        result.ma200_rising,
        rs_rank >= params.get("rs_min", 70),
    ])

    # ── MA 정배열 (5 > 20 > 60 > 120) ──
    if all(v > 0 for v in [result.ma5, result.ma20, result.ma60, result.ma120]):
        result.ma_aligned = (
            result.ma5 > result.ma20 > result.ma60 > result.ma120
        )

    # ── Stage 1 지표: 저점 대비 상승률 ──
    low_60 = float(closes.iloc[-60:].min())
    if low_60 > 0:
        result.rise_from_low_pct = round((cur - low_60) / low_60 * 100, 1)

    # ── Stage 2 지표: VCP ──
    vcp = _detect_vcp(df, params)
    result.vcp_detected = vcp["detected"]
    result.contractions = vcp["contractions"]
    result.vol_drying = vcp.get("vol_drying", False)
    result.range_contraction_pct = vcp["range_contraction_pct"]

    # ── Stage 3 지표: 돌파 ──
    breakout = _detect_breakout(df, params)
    result.breakout_detected = breakout["detected"]
    result.near_high = breakout["near_high"]
    result.volume_surge_ratio = breakout["volume_surge_ratio"]
    result.gap_up = breakout.get("gap_up", False)

    # ── 클라이맥스 경고 ──
    result.climax_warning = _detect_climax(df, params)

    # ── 스테이지 판정 ──
    signals = []
    confidence = 0

    if not result.mtt_pass:
        # MTT 미충족 → 스테이지 판정 불가
        result.stage = None
        result.stage_label = "MTT 미충족"
        if rs_rank < params.get("rs_min", 70):
            signals.append(f"RS {rs_rank:.0f} < {params.get('rs_min', 70)} (약세)")
        if not result.price_above_ma200:
            signals.append("가격 < MA200 (하락 추세)")
        result.signals = signals
        return result

    signals.append("MTT 통과")

    # ── 공통 보너스 계산 ──
    # MA200 기울기 강도 (20일 동안 변화율)
    ma200_slope_bonus = 0
    if len(closes) >= 220:
        ma200_s = _sma_series(closes, 200)
        ma200_now = float(ma200_s.iloc[-1])
        ma200_20ago = float(ma200_s.iloc[-20])
        if ma200_20ago > 0:
            slope_pct = (ma200_now - ma200_20ago) / ma200_20ago * 100
            if slope_pct >= 2.0:
                ma200_slope_bonus = 10  # 강한 상승 기울기
            elif slope_pct >= 0.5:
                ma200_slope_bonus = 5

    # 가격이 MA 위에 얼마나 정돈되어 있는지
    ma_quality_bonus = 0
    if result.ma_aligned:
        # 5일선 위에 있는 최근 10일 비율
        if len(closes) >= 15:
            ma5_s = _sma_series(closes, 5)
            above_count = sum(1 for i in range(-10, 0) if closes.iloc[i] > ma5_s.iloc[i])
            if above_count >= 8:
                ma_quality_bonus = 5

    # RS 보너스 (실제 RS 맵이 있을 때만 유효)
    rs_bonus = 0
    if rs_rank >= 90:
        rs_bonus = 15
        signals.append(f"RS {rs_rank:.0f} — 최상위 강세")
    elif rs_rank >= 80:
        rs_bonus = 10
        signals.append(f"RS {rs_rank:.0f} — 상위 강세")

    # RS 모멘텀 보너스 (main.py에서 부착된 값 사용)
    rs_mom_bonus = 0
    if result.rs_momentum > 10:
        rs_mom_bonus = 5
        signals.append(f"RS 가속 상승 (+{result.rs_momentum:.0f})")
    if result.rs_new_high:
        signals.append("RS 신고점")

    # Stage 3: 돌파 재상승 (최우선)
    if result.breakout_detected:
        result.stage = 3
        result.stage_label = "Stage 3 - 돌파 (매수 타점)"
        confidence = 70
        signals.append(f"전고점 돌파! 거래량 {result.volume_surge_ratio}x")
        if result.gap_up:
            confidence += 10
            signals.append("갭상승 동반")
        if result.vcp_detected:
            confidence += 10
            signals.append(f"VCP 확인 (축소 {result.contractions}회)")
        if result.ma_aligned:
            confidence += 5
            signals.append("MA 정배열")
        # 거래량 폭증 강도 보너스
        if result.volume_surge_ratio >= 3.0:
            confidence += 5
            signals.append(f"거래량 폭증 {result.volume_surge_ratio}x")
        confidence += rs_bonus + ma200_slope_bonus + rs_mom_bonus

    # Stage 2: 조정 베이스 (VCP)
    elif result.vcp_detected and result.ma_aligned:
        result.stage = 2
        result.stage_label = "Stage 2 - 조정 베이스 (대기)"
        confidence = 50
        signals.append(f"VCP 감지: 변동폭 {result.range_contraction_pct}% 축소")
        if result.vol_drying:
            confidence += 10
            signals.append("거래량 마름 확인")
        if result.near_high:
            confidence += 15
            signals.append("전고점 근접 — 돌파 임박 가능")
        if result.contractions >= 3:
            confidence += 5
            signals.append(f"강한 VCP ({result.contractions}회 축소)")
        if result.range_contraction_pct >= 50:
            confidence += 5
            signals.append("변동폭 50%+ 축소 — 타이트 베이스")
        confidence += rs_bonus + ma200_slope_bonus + rs_mom_bonus + ma_quality_bonus

    # Stage 1: 1차 상승
    elif result.ma_aligned and result.rise_from_low_pct >= params.get("stage1_rise_min_pct", 50):
        result.stage = 1
        result.stage_label = "Stage 1 - 1차 상승"
        confidence = 45
        signals.append(f"60일 저점 대비 +{result.rise_from_low_pct}% 상승")
        signals.append("MA 정배열 확인")
        # 상승률 크기별 보너스
        rise = result.rise_from_low_pct
        if rise >= 100:
            confidence += 15
            signals.append("100%↑ 강한 추세")
        elif rise >= 80:
            confidence += 10
            signals.append("80%↑ 견고한 상승")
        elif rise >= 60:
            confidence += 5
        confidence += rs_bonus + ma200_slope_bonus + rs_mom_bonus + ma_quality_bonus

    # MA 정배열이지만 아직 50% 미달
    elif result.ma_aligned:
        result.stage = 1
        result.stage_label = "Stage 1 - 초기 상승"
        confidence = 30
        signals.append(f"MA 정배열, 저점 대비 +{result.rise_from_low_pct}%")
        if result.rise_from_low_pct >= 30:
            confidence += 10
            signals.append("30%↑ 초기 모멘텀 확인")
        elif result.rise_from_low_pct >= 15:
            confidence += 5
        confidence += rs_bonus + ma200_slope_bonus + rs_mom_bonus

    else:
        result.stage = None
        result.stage_label = "MTT 통과, 스테이지 미분류"
        confidence = 15
        signals.append("MA 정배열 아님")

    # 클라이맥스 경고
    if result.climax_warning:
        confidence = max(confidence - 20, 0)
        signals.append("⚠ 클라이맥스 경고: 과열 징후")

    result.confidence = min(confidence, 100)
    result.signals = signals

    # ── 포지션 사이징 (2% 룰) — 초기 손절 = 트레일링 스탑과 동일 (-10%)
    initial_stop = params.get("trail_stop_pct", params.get("stop_loss_pct", -10))
    stop_pct = abs(initial_stop) / 100
    risk_amount = total_capital * 0.02  # 총 자산의 2%
    loss_per_share = cur * stop_pct
    if loss_per_share > 0:
        shares = int(risk_amount / loss_per_share)
        position_value = shares * cur
        result.position_size_pct = round(position_value / total_capital * 100, 1)
    result.suggested_stop_pct = initial_stop

    return result
