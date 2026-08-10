"""
데이터 제공자 — pykrx + KIS Open API + FinanceDataReader 통합
"""

import asyncio
import datetime as dt
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
from pykrx import stock as krx

logger = logging.getLogger(__name__)

# ── config ──────────────────────────────────────────────
# 저장소 루트의 config.json + 환경변수(KIS_APP_KEY / KIS_APP_SECRET) 병합.
# 키는 GitHub Secrets → 환경변수로만 주입되고 config.json에는 절대 넣지 않는다.
import os

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    kis = cfg.setdefault("kis", {})
    kis.setdefault("base_url", "https://openapi.koreainvestment.com:9443")
    if os.environ.get("KIS_APP_KEY"):
        kis["app_key"] = os.environ["KIS_APP_KEY"]
    if os.environ.get("KIS_APP_SECRET"):
        kis["app_secret"] = os.environ["KIS_APP_SECRET"]
    return cfg


# ── KIS Open API 토큰 관리 ──────────────────────────────
_kis_token: Optional[str] = None
_kis_token_expires: float = 0
_kis_token_fail_until: float = 0  # 발급 실패 시 재시도 금지 시각 (1분당 1회 제한 준수)
_kis_token_lock: asyncio.Lock | None = None  # 이벤트 루프 생성 후 초기화

# KIS 토큰 발급 실패(403 EGW00133 "1분당 1회") 후 재시도까지 대기 (초)
_KIS_TOKEN_FAIL_COOLDOWN = 65


# ── KIS 전역 속도 제한 (초당 20건 한도 → 여유 있게 12건/초) ──
class _RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._next_at = 0.0
        self._lock: asyncio.Lock | None = None

    async def wait(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if now < self._next_at:
                await asyncio.sleep(self._next_at - now)
                now = self._next_at
            self._next_at = now + self.interval


_kis_rate = _RateLimiter(12)


def _get_token_lock() -> asyncio.Lock:
    """이벤트 루프 안에서 Lock을 지연 생성 (동시 토큰 발급 방지)"""
    global _kis_token_lock
    if _kis_token_lock is None:
        _kis_token_lock = asyncio.Lock()
    return _kis_token_lock


# 실행(러너) 간 토큰 공유 — Worker KV. KIS 토큰은 24시간 유효 + 재발급 시 기존 무효화될 수 있어
# 하루 1회 발급이 원칙인데, Actions는 실행마다 새 프로세스라 KV에 보관해 재사용한다.
_NEXUS_BASE = "https://nexus-platform.nexusassetfund.workers.dev"


def _nexus_admin_token() -> Optional[str]:
    return os.environ.get("NEXUS_ADMIN_TOKEN") or None


async def _kis_token_remote_get() -> Optional[dict]:
    tk = _nexus_admin_token()
    if not tk:
        return None
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{_NEXUS_BASE}/api/kis/token", headers={"authorization": f"Bearer {tk}"})
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


async def _kis_token_remote_put(token: str, expires_at: float) -> None:
    tk = _nexus_admin_token()
    if not tk:
        return
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            await client.put(f"{_NEXUS_BASE}/api/kis/token", headers={"authorization": f"Bearer {tk}"},
                             json={"access_token": token, "expires_at": expires_at})
    except Exception:
        pass


async def _get_kis_token(cfg: dict) -> Optional[str]:
    global _kis_token, _kis_token_expires, _kis_token_fail_until
    kis = cfg.get("kis", {})
    if not kis.get("app_key") or not kis.get("app_secret"):
        return None
    now = dt.datetime.now().timestamp()
    # 유효한 토큰이 있으면 바로 반환 (락 없이)
    if _kis_token and now < _kis_token_expires - 60:
        return _kis_token
    # 동시 발급 요청이 여러 개여도 하나만 실제로 KIS에 요청 (분당 1회 제한 준수)
    async with _get_token_lock():
        # 락 획득 후 재확인 (다른 코루틴이 이미 발급했을 수 있음)
        now = dt.datetime.now().timestamp()
        if _kis_token and now < _kis_token_expires - 60:
            return _kis_token
        # 실행 간 공유 토큰(Worker KV) — 다른 러너가 이미 발급한 24시간 토큰 재사용
        remote = await _kis_token_remote_get()
        if remote and now < float(remote.get("expires_at") or 0) - 3600:
            _kis_token = remote["access_token"]
            _kis_token_expires = float(remote["expires_at"])
            logger.info("KIS 토큰 공유 저장소에서 재사용 (만료: %.0f초 후)", _kis_token_expires - now)
            return _kis_token
        # 직전 발급이 실패했으면 쿨다운 동안 재요청하지 않음 — 토큰 요청 폭주 방지.
        # KIS는 토큰 발급을 1분당 1회로 제한(403 EGW00133)하므로, 실패 직후
        # 모든 코루틴이 즉시 재요청하면 1분 윈도우가 영구히 막혀 토큰을 못 받는다.
        if now < _kis_token_fail_until:
            return None
        try:
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                resp = await client.post(
                    f"{kis['base_url']}/oauth2/tokenP",
                    json={
                        "grant_type": "client_credentials",
                        "appkey": kis["app_key"],
                        "appsecret": kis["app_secret"],
                    },
                )
                data = resp.json()
                token = data.get("access_token")
                if not token:
                    # 403 등 에러 응답 — 쿨다운 설정 후 다음 호출까지 재요청 차단
                    err_code = data.get("error_code", "")
                    err_msg = data.get("error_description", data.get("msg1", "unknown"))
                    _kis_token_fail_until = now + _KIS_TOKEN_FAIL_COOLDOWN
                    logger.warning(
                        "KIS 토큰 발급 실패 [%s]: %s — %.0f초 후 재시도",
                        err_code, err_msg, _KIS_TOKEN_FAIL_COOLDOWN,
                    )
                    return None
                _kis_token = token
                _kis_token_expires = now + int(data.get("expires_in") or 86400)
                _kis_token_fail_until = 0
                logger.info("KIS 토큰 발급 성공 (만료: %.0f초 후)", _kis_token_expires - now)
                await _kis_token_remote_put(token, _kis_token_expires)  # 다음 러너가 재사용
        except Exception as e:
            # 빈 응답/타임아웃 등도 동일하게 쿨다운 — 재요청 폭주 방지
            _kis_token_fail_until = now + _KIS_TOKEN_FAIL_COOLDOWN
            logger.warning("KIS 토큰 발급 실패: %r — %.0f초 후 재시도", e, _KIS_TOKEN_FAIL_COOLDOWN)
            return None
    return _kis_token


async def kis_get(cfg: dict, path: str, tr_id: str, params: dict) -> Optional[dict]:
    """KIS Open API GET 요청"""
    kis = cfg.get("kis", {})
    token = await _get_kis_token(cfg)
    if not token:
        return None
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": kis["app_key"],
        "appsecret": kis["app_secret"],
        "tr_id": tr_id,
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        async with httpx.AsyncClient(verify=False, timeout=8.0) as client:
            resp = await client.get(
                f"{kis['base_url']}{path}",
                headers=headers,
                params=params,
            )
            data = resp.json()
            if data.get("rt_cd") == "0":
                return data
            logger.warning("KIS error: %s", data.get("msg1"))
    except Exception as e:
        logger.warning("KIS request failed: %s", e)
    return None


# ── KIS 일봉 OHLCV 조회 (비동기, 빠름) ──────────────────
async def _fetch_ohlcv_kis(ticker: str, start: str, end: str) -> pd.DataFrame:
    """KIS Open API로 일봉 OHLCV 가져오기 — 100건씩 페이징"""
    cfg = load_config()
    kis = cfg.get("kis", {})
    token = await _get_kis_token(cfg)
    if not token:
        return pd.DataFrame()

    headers = {
        "authorization": f"Bearer {token}",
        "appkey": kis["app_key"],
        "appsecret": kis["app_secret"],
        "tr_id": "FHKST01010400",
        "Content-Type": "application/json; charset=utf-8",
    }
    base_url = kis["base_url"]
    all_rows = []
    cursor_date = end  # YYYYMMDD

    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        for _ in range(5):  # 최대 5페이지 (500일)
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": cursor_date,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            }
            try:
                await _kis_rate.wait()
                resp = await client.get(
                    f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                    headers=headers, params=params,
                )
                if resp.status_code != 200:
                    logger.debug("KIS OHLCV HTTP %s for %s", resp.status_code, ticker)
                    break
                data = resp.json()
                if data.get("rt_cd") != "0":
                    logger.debug("KIS OHLCV rt_cd=%s for %s: %s",
                                 data.get("rt_cd"), ticker, data.get("msg1", ""))
                    break
                items = data.get("output2", [])
                if not items:
                    break
                for item in items:
                    d = item.get("stck_bsop_date", "")
                    o = int(item.get("stck_oprc") or 0)
                    h = int(item.get("stck_hgpr") or 0)
                    l = int(item.get("stck_lwpr") or 0)
                    c = int(item.get("stck_clpr") or 0)
                    v = int(item.get("acml_vol") or 0)
                    if d and c > 0:
                        all_rows.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
                # 다음 페이지: 마지막 날짜 - 1일
                last_date = items[-1].get("stck_bsop_date", "")
                if not last_date or last_date <= start:
                    break
                prev = dt.datetime.strptime(last_date, "%Y%m%d") - dt.timedelta(days=1)
                cursor_date = prev.strftime("%Y%m%d")
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning("KIS OHLCV failed for %s: %s", ticker, e)
                break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.drop_duplicates(subset="date").sort_values("date").set_index("date")
    return df[["open", "high", "low", "close", "volume"]]


# ── pykrx 래퍼 (블로킹 → async, fallback용) ─────────────
def _fetch_ohlcv_sync(ticker: str, start: str, end: str) -> pd.DataFrame:
    """pykrx에서 일봉 OHLCV 가져오기 (동기)"""
    try:
        df = krx.get_market_ohlcv_by_date(start, end, ticker)
        if df is None or df.empty:
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        col_map = {"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"}
        df = df.rename(columns=col_map)
        df = df[["open", "high", "low", "close", "volume"]]
        return df.dropna()
    except Exception as e:
        logger.warning("pykrx fetch failed for %s: %s", ticker, e)
        return pd.DataFrame()


def _fetch_ohlcv_fdr(ticker: str, start: str, end: str) -> pd.DataFrame:
    """FinanceDataReader로 일봉 OHLCV 가져오기 (동기, pykrx 대체)"""
    try:
        import FinanceDataReader as fdr
        start_dt = dt.datetime.strptime(start, "%Y%m%d").date()
        end_dt   = dt.datetime.strptime(end,   "%Y%m%d").date()
        df = fdr.DataReader(ticker, start_dt, end_dt)
        if df is None or df.empty:
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        col_map = {"Open": "open", "High": "high", "Low": "low",
                   "Close": "close", "Volume": "volume"}
        df = df.rename(columns=col_map)
        needed = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        if "close" not in needed:
            return pd.DataFrame()
        return df[needed].dropna()
    except Exception as e:
        logger.debug("FDR OHLCV failed for %s: %s", ticker, e)
        return pd.DataFrame()


# 소스별 서킷 브레이커: 연속 실패가 임계치를 넘으면 해당 소스를 건너뛴다.
# 죽은 소스에 종목마다 15~20초 타임아웃을 반복 지불하는 것을 방지.
_src_consec_fail = {"pykrx": 0, "fdr": 0}
_SRC_FAIL_LIMIT = 12


def _src_alive(name: str) -> bool:
    return _src_consec_fail[name] < _SRC_FAIL_LIMIT


def _src_report(name: str, ok: bool):
    if ok:
        _src_consec_fail[name] = 0
    else:
        _src_consec_fail[name] += 1
        if _src_consec_fail[name] == _SRC_FAIL_LIMIT:
            logger.warning("%s 연속 %d회 실패 — 이번 실행에서는 건너뜀", name, _SRC_FAIL_LIMIT)


async def fetch_ohlcv(ticker: str, days: int = 400) -> pd.DataFrame:
    """비동기 OHLCV 조회: KIS → pykrx (20s timeout) → FDR fallback (서킷 브레이커 적용)"""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    end_str = end.strftime("%Y%m%d")
    start_str = start.strftime("%Y%m%d")

    # 1) KIS API (비동기, 빠름) — 토큰 실패 시 내부 쿨다운이 폭주 방지
    try:
        df = await _fetch_ohlcv_kis(ticker, start_str, end_str)
        if not df.empty and len(df) > 50:
            return df
    except Exception as e:
        logger.debug("KIS OHLCV fallthrough for %s: %s", ticker, e)

    loop = asyncio.get_event_loop()

    # 2) pykrx fallback (동기) — 20초 timeout
    if _src_alive("pykrx"):
        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_ohlcv_sync, ticker, start_str, end_str),
                timeout=20.0,
            )
            if not df.empty and len(df) > 50:
                _src_report("pykrx", True)
                return df
            _src_report("pykrx", False)
        except asyncio.TimeoutError:
            _src_report("pykrx", False)
            logger.warning("pykrx OHLCV timeout (20s) for %s — FDR fallback", ticker)
        except Exception as e:
            _src_report("pykrx", False)
            logger.debug("pykrx OHLCV failed for %s: %s", ticker, e)

    # 3) FDR fallback
    if _src_alive("fdr"):
        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_ohlcv_fdr, ticker, start_str, end_str),
                timeout=15.0,
            )
            # KIS·pykrx와 동일하게 >50행 요구 — 부분 응답(20~40행)이 52주 범위·추세 계산을
            # 왜곡하지 않도록. 상장 이력이 짧은 종목은 여기서 빈 프레임 반환(계산 불가라 정상).
            if not df.empty and len(df) > 50:
                _src_report("fdr", True)
                return df
            _src_report("fdr", False)
        except asyncio.TimeoutError:
            _src_report("fdr", False)
            logger.warning("FDR OHLCV timeout (15s) for %s", ticker)
        except Exception as e:
            _src_report("fdr", False)
            logger.debug("FDR OHLCV failed for %s: %s", ticker, e)

    return pd.DataFrame()


def _fetch_kospi_ohlcv_sync(start: dt.date, end: dt.date) -> pd.DataFrame:
    """FinanceDataReader로 KOSPI 지수 일봉 조회 (동기)"""
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader("KS11", start, end)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"Close": "close"})
        return df[["close"]].dropna()
    except Exception as e:
        logger.warning("KOSPI FDR 조회 실패: %s", e)
        return pd.DataFrame()


async def fetch_kospi_status(slope_days: int = 10) -> dict:
    """KOSPI 지수 MA200/MA50 상태 반환 (백테스트 v9 로직)
    - entry_allowed: KOSPI > MA200 (진입 허용)
    - exit_signal:   KOSPI < MA50 AND MA50 10일 기울기 하향 (전체청산 신호)
    """
    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=310)
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, _fetch_kospi_ohlcv_sync, start, end)
        if df.empty or len(df) < 60:
            return {"error": "KOSPI 데이터 부족", "entry_allowed": True, "exit_signal": False}

        close = df["close"]
        price = float(close.iloc[-1])

        ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        ma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None

        above_ma200 = (price > ma200) if ma200 is not None else None
        above_ma50  = (price > ma50)  if ma50  is not None else None

        ma50_declining = False
        if ma50 is not None and len(close) >= 50 + slope_days:
            ma50_s    = close.rolling(50).mean()
            ma50_now  = float(ma50_s.iloc[-1])
            ma50_prev = float(ma50_s.iloc[-1 - slope_days])
            if ma50_prev > 0:
                ma50_declining = (ma50_now - ma50_prev) / ma50_prev < 0

        exit_signal   = bool((not above_ma50) and ma50_declining) if above_ma50 is not None else False
        entry_allowed = bool(above_ma200) if above_ma200 is not None else True

        return {
            "price":          round(price, 2),
            "ma200":          round(ma200, 2) if ma200 is not None else None,
            "ma50":           round(ma50, 2)  if ma50  is not None else None,
            "above_ma200":    above_ma200,
            "above_ma50":     above_ma50,
            "ma50_declining": ma50_declining,
            "exit_signal":    exit_signal,
            "entry_allowed":  entry_allowed,
            "updated":        dt.datetime.now(tz=ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        }
    except Exception as e:
        logger.warning("KOSPI 상태 조회 실패: %s", e)
        return {"error": str(e), "entry_allowed": True, "exit_signal": False}


def _fetch_market_cap_sync(date_str: str) -> pd.DataFrame:
    """전 종목 시가총액 (동기)"""
    try:
        df = krx.get_market_cap_by_ticker(date_str)
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception:
        return pd.DataFrame()


async def fetch_market_cap() -> pd.DataFrame:
    """비동기 시가총액"""
    today = dt.date.today().strftime("%Y%m%d")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_market_cap_sync, today)


def _fetch_all_tickers_sync() -> list[tuple[str, str]]:
    """KOSPI + KOSDAQ 전 종목 (ticker, name) 리스트"""
    tickers = []
    today = dt.date.today().strftime("%Y%m%d")
    for market in ["KOSPI", "KOSDAQ"]:
        try:
            codes = krx.get_market_ticker_list(today, market=market)
            for code in codes:
                name = krx.get_market_ticker_name(code)
                tickers.append((code, name))
        except Exception:
            pass
    return tickers


async def fetch_all_tickers() -> list[tuple[str, str]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_all_tickers_sync)


_recent_trading_date_cache: Optional[str] = None


def _find_recent_trading_date() -> Optional[str]:
    """pykrx 최근 거래일 — 전용 함수 1회 호출 + 프로세스 캐시
    (기존: 전종목 스냅샷을 최대 7회 조회 — KRX 차단 유발 요인)"""
    global _recent_trading_date_cache
    if _recent_trading_date_cache:
        return _recent_trading_date_cache
    try:
        _recent_trading_date_cache = krx.get_nearest_business_day_in_a_week()
    except Exception:
        _recent_trading_date_cache = None
    return _recent_trading_date_cache


# 구성종목 캐시 — KRX/FDR이 러너 IP를 차단하는 시간대에도 스캔이 완주하도록.
# 지수 구성은 반기 리밸런싱이라 하루 이틀 묵은 캐시로 충분하다.
_UNIVERSE_CACHE = Path(__file__).resolve().parents[1] / "docs" / "data" / "universe_cache.json"


def _load_universe_cache() -> list[tuple[str, str]]:
    try:
        d = json.loads(_UNIVERSE_CACHE.read_text("utf-8"))
        return [(str(x[0]), str(x[1])) for x in d.get("tickers", [])]
    except Exception:
        return []


def _save_universe_cache(tickers: list[tuple[str, str]]) -> None:
    try:
        _UNIVERSE_CACHE.write_text(json.dumps(
            {"updated": dt.datetime.now().isoformat(timespec="seconds"), "tickers": tickers},
            ensure_ascii=False), "utf-8")
    except Exception as e:
        logger.warning("유니버스 캐시 저장 실패: %s", e)


def _fetch_index_constituents_sync() -> list[tuple[str, str]]:
    """코스피200 + 코스닥150 지수 구성종목 (ticker, name) 리스트"""
    tickers = []
    seen = set()

    date_str = _find_recent_trading_date()

    if date_str:
        # 종목명 일괄 맵 — 시장당 1회 조회 (기존: 구성종목당 개별 KRX 호출 ~350회 → 차단 유발)
        name_map = {}
        for market in ["KOSPI", "KOSDAQ"]:
            try:
                s = krx.get_market_ticker_and_name(date_str, market=market)
                name_map.update({str(c): str(n) for c, n in s.items()})
            except Exception:
                pass
        # 코스피200: 지수 코드 "1028", 코스닥150: 지수 코드 "2203"
        index_map = {"1028": "코스피200", "2203": "코스닥150"}
        for idx_code, idx_name in index_map.items():
            try:
                df = krx.get_index_portfolio_deposit_file(idx_code, date_str)
                if df is not None and len(df) > 0:
                    for ticker in df:
                        if ticker not in seen:
                            seen.add(ticker)
                            tickers.append((ticker, name_map.get(ticker, ticker)))
                    logger.info("%s 구성종목 %d개 로드", idx_name, len(df))
            except Exception as e:
                logger.warning("%s 구성종목 조회 실패: %s", idx_name, e)
    else:
        logger.warning("pykrx 거래일 감지 실패 — FDR fallback으로 전환")

    # pykrx 지수 예탁파일에서 직접 얻었는가 — 이 경우만 "진짜 지수 구성"으로 신뢰
    from_pykrx = len(tickers) >= 100

    if not from_pykrx:
        # pykrx 실패: FDR 근사(시총 상위 350)보다 마지막 성공 캐시가 정확하므로 캐시 우선.
        # (FDR 폴백은 진짜 코스피200/코스닥150 편입이 아닌 시총 근사치라 캐시를 오염시키면 안 됨)
        logger.info("pykrx 지수 조회 부족 (%d개) — 캐시 우선 확인", len(tickers))
        cached = _load_universe_cache()
        if len(cached) >= 100:
            logger.warning("pykrx 실패 — 유니버스 캐시 %d종목 사용 (FDR 근사보다 정확)", len(cached))
            return cached
        # 캐시도 없을 때만 최후 수단으로 FDR 시총 근사 사용 (캐시에 저장하지 않음)
        logger.warning("pykrx·캐시 모두 실패 — FDR 시총 근사로 완주 (진짜 지수 구성 아님, 캐시 미갱신)")
        tickers, seen = _fetch_constituents_fdr_fallback()

    # 종목명 보강 — name_map 실패 시 (code, code)로 남은 항목을 캐시·FDR 리스팅으로 해결
    tickers = _backfill_names(tickers)

    # 진짜 지수 구성(pykrx)일 때만 캐시 저장 — FDR 근사가 last-known-good을 덮어쓰지 못하게
    # 종목명이 대부분 미해결이면 저장 생략 (코드-이름 오염 캐시가 last-known-good을 덮으면 안 됨)
    unresolved = sum(1 for c, n in tickers if n == c)
    if from_pykrx and unresolved <= len(tickers) // 10:
        _save_universe_cache(tickers)
    elif from_pykrx:
        logger.warning("종목명 미해결 %d/%d — 유니버스 캐시 저장 생략", unresolved, len(tickers))

    logger.info("지수 구성종목 총 %d개 (중복 제거, 소스=%s)", len(tickers), "pykrx" if from_pykrx else "fdr근사")
    return tickers


def _backfill_names(tickers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(code, code)로 남은 미해결 종목명을 캐시(last-known-good) → FDR 리스팅 순으로 보강.
    pykrx get_market_ticker_and_name이 통째로 실패해도 후보가 코드명으로 게시되지 않게 한다."""
    unresolved = {c for c, n in tickers if n == c}
    if not unresolved:
        return tickers
    fix_map = {c: n for c, n in _load_universe_cache() if n != c and c in unresolved}
    if unresolved - set(fix_map):
        try:
            import FinanceDataReader as fdr
            for market in ["KOSPI", "KOSDAQ"]:
                df = fdr.StockListing(market)
                for _, row in df.iterrows():
                    code = str(row.get("Code", "")).strip()
                    if code in unresolved and code not in fix_map:
                        name = str(row.get("Name", "")).strip()
                        if name and name != code:
                            fix_map[code] = name
        except Exception as e:
            logger.warning("FDR 종목명 보강 실패: %s", e)
    logger.info("종목명 보강: 미해결 %d개 중 %d개 해결", len(unresolved), len(fix_map))
    return [(c, fix_map.get(c, n)) for c, n in tickers]


def _fetch_constituents_fdr_fallback() -> tuple[list[tuple[str, str]], set]:
    """FinanceDataReader로 KOSPI+KOSDAQ 종목 리스트를 가져와 시가총액 상위 350개 선별"""
    import FinanceDataReader as fdr

    tickers = []
    seen = set()
    all_stocks = []

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = fdr.StockListing(market)
            if df is not None and not df.empty:
                # 시총 컬럼(Marcap/Amount)이 있으면 시총 내림차순 정렬 — 리스팅 순서(무작위)보다
                # 지수 구성(대형주)에 훨씬 근접. 실제 코스피200/코스닥150 편입은 아니므로 근사치.
                capcol = next((c for c in ("Marcap", "MarketCap", "Amount") if c in df.columns), None)
                if capcol:
                    df = df.sort_values(capcol, ascending=False)
                for _, row in df.iterrows():
                    code = str(row.get("Code", "")).strip().upper()
                    name = str(row.get("Name", code)).strip()
                    if re.fullmatch(r"\d[0-9A-Z]{5}", code) and code not in seen:
                        all_stocks.append((code, name, market))
                        seen.add(code)
                logger.info("FDR %s: %d 종목 로드 (%s)", market, len(df),
                            f"시총순 {capcol}" if capcol else "리스팅순")
        except Exception as e:
            logger.warning("FDR %s 로드 실패: %s", market, e)

    # 시총 상위 KOSPI 200 + KOSDAQ 150 근사 (정렬됐으면 시총 상위, 아니면 리스팅 순서)
    kospi = [(c, n) for c, n, m in all_stocks if m == "KOSPI"][:200]
    kosdaq = [(c, n) for c, n, m in all_stocks if m == "KOSDAQ"][:150]

    result = kospi + kosdaq
    result_seen = {c for c, _ in result}
    logger.info("FDR fallback: KOSPI %d + KOSDAQ %d = %d 종목", len(kospi), len(kosdaq), len(result))
    return result, result_seen


async def fetch_index_constituents() -> list[tuple[str, str]]:
    """비동기: 코스피200 + 코스닥150 구성종목 조회"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_index_constituents_sync)


# ── RS (상대강도) 계산 ──────────────────────────────────
def calc_rs_rank(returns_map: dict[str, float]) -> dict[str, float]:
    """
    종목별 수익률 딕셔너리 → RS 백분위(0~99) 딕셔너리
    returns_map: {ticker: 기간수익률}
    """
    if not returns_map:
        return {}
    tickers = list(returns_map.keys())
    rets = np.array([returns_map[t] for t in tickers])
    # 백분위 랭킹 (0~99)
    ranks = np.argsort(np.argsort(rets)) / max(len(rets) - 1, 1) * 99
    return {t: round(float(r), 1) for t, r in zip(tickers, ranks)}


# ── 장 운영시간 체크 ─────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")


def is_market_hours() -> bool:
    """한국 주식시장 장중 여부 (평일 09:00~15:30 KST)"""
    now = dt.datetime.now(tz=KST)
    if now.weekday() >= 5:  # 토, 일
        return False
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


# ── 배치 실시간 가격 조회 (KIS) ──────────────────────────
async def fetch_realtime_prices_batch(
    cfg: dict, tickers: list[str]
) -> dict[str, dict]:
    """
    KIS inquire-price로 여러 종목의 장중 OHLCV를 배치 조회.
    rate limit 준수: Semaphore(20) + 50ms 간격.
    반환: {ticker: {open, high, low, close, volume}}
    """
    sem = asyncio.Semaphore(20)
    results: dict[str, dict] = {}
    kis = cfg.get("kis", {})
    token = await _get_kis_token(cfg)
    if not token:
        logger.warning("KIS 토큰 없음 — 배치 실시간 조회 불가")
        return results

    headers = {
        "authorization": f"Bearer {token}",
        "appkey": kis["app_key"],
        "appsecret": kis["app_secret"],
        "tr_id": "FHKST01010100",
        "Content-Type": "application/json; charset=utf-8",
    }
    base_url = kis["base_url"]

    async def _fetch_one(client: httpx.AsyncClient, ticker: str):
        async with sem:
            try:
                await _kis_rate.wait()
                resp = await client.get(
                    f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
                    headers=headers,
                    params={
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": ticker,
                    },
                    timeout=10,
                )
                data = resp.json()
                if data.get("rt_cd") == "0" and data.get("output"):
                    out = data["output"]
                    o = int(out.get("stck_oprc") or 0)
                    h = int(out.get("stck_hgpr") or 0)
                    l = int(out.get("stck_lwpr") or 0)
                    c = int(out.get("stck_prpr") or 0)
                    v = int(out.get("acml_vol") or 0)
                    if c > 0:
                        results[ticker] = {
                            "open": o, "high": h, "low": l,
                            "close": c, "volume": v,
                        }
            except Exception as e:
                logger.debug("실시간 조회 실패 %s: %s", ticker, e)
            await asyncio.sleep(0.05)  # 50ms 간격

    async with httpx.AsyncClient(verify=False) as client:
        tasks = [_fetch_one(client, t) for t in tickers]
        await asyncio.gather(*tasks)

    logger.info("배치 실시간 조회 완료: %d/%d 성공", len(results), len(tickers))
    return results


# ── KIS 실시간 현재가 ────────────────────────────────────
async def fetch_fundamental_kis(cfg: dict, ticker: str) -> Optional[dict]:
    """KIS 현재가 시세(FHKST01010100)에서 밸류 지표 추출 — pykrx(KRX) 차단 시 폴백.
    반환: {price, per, pbr, eps, bps, cap(원), div(None — KIS 응답에 배당수익률 없음)}"""
    data = await kis_get(
        cfg,
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    if not (data and data.get("output")):
        return None
    out = data["output"]

    def f(k):
        try:
            v = float(out.get(k) or 0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    cap = f("hts_avls")  # HTS 시가총액 (억원)
    return {
        "price": f("stck_prpr"),
        "per": f("per"), "pbr": f("pbr"), "eps": f("eps"), "bps": f("bps"),
        "cap": cap * 1e8 if cap else None,
        "div": None,
    }


async def fetch_realtime_price(cfg: dict, ticker: str) -> Optional[dict]:
    """KIS로 현재가 조회"""
    data = await kis_get(
        cfg,
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    if data and data.get("output"):
        out = data["output"]
        return {
            "price": int(out.get("stck_prpr") or 0),
            "volume": int(out.get("acml_vol") or 0),
            "change_pct": float(out.get("prdy_ctrt") or 0),
        }
    return None


# ── 투자자별 매매동향 (KIS) ──────────────────────────────
async def fetch_investor_flow(cfg: dict, ticker: str) -> Optional[dict]:
    """외국인/기관 순매수 조회"""
    data = await kis_get(
        cfg,
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        "FHKST01010900",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        },
    )
    if data and data.get("output"):
        items = data["output"]
        if isinstance(items, list) and len(items) > 0:
            row = items[0]
            return {
                "foreign_net": int(row.get("frgn_ntby_qty") or 0),
                "institution_net": int(row.get("orgn_ntby_qty") or 0),
            }
    return None
