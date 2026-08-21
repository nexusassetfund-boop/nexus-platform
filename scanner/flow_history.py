"""수급(외국인·기관) 일별 이력 수집 + 지표 계산 — 수급 감지기의 공용 코어.

라이브 스크리너(flow_screener.py)와 백테스트(flow_backtest.py)가 **같은 함수**로
지표를 계산한다. 라이브/백테스트 규칙이 갈라지는 게 이 바닥 최대의 자기기만이라
계산은 여기 한 곳에만 둔다.

## 데이터 소스: 네이버 금융 (pykrx 아님)
KRX(data.krx.co.kr) 투자자별 엔드포인트는 IP에 따라 상습 차단된다(SETUP.md §3-7).
실측: 이 저장소 개발 환경에서 get_market_net_purchases_of_equities /
get_market_cap_by_ticker 전부 비JSON 응답. 그래서 종목별 네이버 frgn 페이지
(`item/frgn.naver`, EUC-KR, 20행/페이지)를 단일 출처로 쓴다.

한계 (반드시 인지):
  · 네이버 '기관'은 **기관합계**다. 금융투자(증권사 자기매매·ETF LP·차익거래)를
    분리할 수 없다 — 이게 기관 수급 노이즈의 최대 원인인데 지금은 못 걷어낸다.
    KRX 접근이 살아나면 `기관합계 - 금융투자`로 교체할 것. 그때 IC 재측정 필수.
  · 순매매 단위가 **주수**다(거래대금 아님). 그래서 정규화는 거래대금 대비가 아니라
    거래량(주수) 대비로 한다 — 같은 날 같은 종목 안에서 비교하므로 등가다.

## 지표 (window W 거래일)
  intensity  = Σ순매수주수 / Σ거래량        기간 매집 강도 (-1 ~ 1). 시총·주가 무관 비교 가능
  persist    = 순매수일수 / W                지속성 0~1. '연속'이 아니라 '비율' — 하루 매도로 0이 되지 않는다
  streak     = 흠집 허용 연속 순매수일        아래 규칙
  concentr   = max(일별 |순매수|) / Σ|순매수| 1일 집중도. 지수 리밸런싱·블록딜 탐지용

### 흠집 허용 스트릭 (tolerance streak)
"4일 연속 10만주씩 사다가 하루 1만주 팔면 리셋" 문제를 없앤다. 오늘부터 거꾸로 걸으며
순매도일을 만나면:
  |순매도| < TOL_RATIO × (현재 스트릭의 평균 순매수)  이고  흠집 ≤ MAX_BLEMISH  → 스트릭 유지
  그 외 → 중단
추가로 스트릭 구간 누적 순매수 > 0 을 강제한다(흠집 허용이 만드는 가짜 스트릭 차단).
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("flow_history")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
       "Referer": "https://finance.naver.com/"}
_ROWS_PER_PAGE = 20
_FETCH_SLEEP = 0.15

# ── 지표 파라미터 (백테스트로 검증하는 노브는 전부 여기) ──
# WINDOW는 streak 과 창이 다르다(streak은 오늘부터 거꾸로 걷는 가변 구간). 이 '불일치'를
# 맞추자는 제안이 있었으나 검증 결과 맞추면 IC가 떨어진다 — 10일 0.0388(t=3.55) →
# 스트릭구간 0.0267(t=2.57). 근거는 flow_screener 모듈 docstring '3차 검증' 참조.
WINDOW = 10          # 집계 거래일
TOL_RATIO = 0.3      # 흠집 판정: 직전 평균 순매수의 30% 미만 매도는 노이즈로 본다
MAX_BLEMISH = 2      # 창 안에서 허용하는 흠집 횟수


MIN_CAP = 100_000_000_000     # 시총 1,000억 — 페니·초소형 배제
MIN_AMOUNT = 1_000_000_000    # 일 거래대금 10억 — 정규화 분모가 튀는 종목 배제


# ── 유니버스 ────────────────────────────────────────────
def universe(limit: int = 300, cache_path: Path | None = None) -> dict[str, str]:
    """시총 상위 {code: name}. FDR KRX 상장목록 단일 출처(KRX JSON API는 차단 상습)."""
    if cache_path and Path(cache_path).exists():
        try:
            cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if len(cached) >= limit:
                return dict(list(cached.items())[:limit])
        except Exception as e:                       # noqa: BLE001
            logger.warning("유니버스 캐시 무시: %s", e)
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])]
    df = df[(df["Marcap"] >= MIN_CAP) & (df["Amount"] >= MIN_AMOUNT)]
    df = df[~df["Name"].str.contains("스팩", na=False)]
    df = df[df["Code"].str.endswith("0")]            # 우선주 코드 배제
    df = df.sort_values("Marcap", ascending=False)
    out = {r.Code: r.Name for r in df.itertuples()}
    if cache_path:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return dict(list(out.items())[:limit])


# ── 수집 ────────────────────────────────────────────────
def _get(url: str, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("euc-kr", "replace")
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"fetch 실패 {url}: {last}")


_NUM = re.compile(r"-?[\d,]+")


def _int(v) -> int | None:
    m = _NUM.search(str(v).replace("\xa0", " "))
    if not m:
        return None
    try:
        return int(m.group().replace(",", ""))
    except ValueError:
        return None


def _parse_page(html: str) -> list[dict]:
    """frgn 페이지 → [{date, close, volume, inst, frgn}] (그 페이지의 행들)."""
    import pandas as pd
    for tb in pd.read_html(io.StringIO(html)):
        cols = [str(c) for c in tb.columns]
        if tb.shape[1] == 9 and any("날짜" in c for c in cols):
            break
    else:
        return []
    out = []
    for _, r in tb.iterrows():
        raw = list(r)
        d = str(raw[0]).strip()
        if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", d):
            continue
        close, vol, inst, frgn = (_int(raw[1]), _int(raw[4]), _int(raw[5]), _int(raw[6]))
        if close is None or vol is None or inst is None or frgn is None:
            continue
        out.append({"date": d.replace(".", ""), "close": close,
                    "volume": vol, "inst": inst, "frgn": frgn})
    return out


def fetch_flow(code: str, pages: int = 2) -> list[dict]:
    """종목 일별 수급 — 오름차순(과거→최근). pages×20 거래일."""
    rows: dict[str, dict] = {}
    for p in range(1, pages + 1):
        html = _get(f"https://finance.naver.com/item/frgn.naver?code={code}&page={p}")
        page_rows = _parse_page(html)
        if not page_rows:
            break                                  # 상장 초기 등 — 더 파도 빈 페이지
        for r in page_rows:
            rows[r["date"]] = r
        time.sleep(_FETCH_SLEEP)
    return [rows[d] for d in sorted(rows)]


class FlowCache:
    """종목별 수급 이력 디스크 캐시. 백테스트 재실행이 네트워크를 다시 타지 않게 한다."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, list[dict]] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:                  # noqa: BLE001
                logger.warning("캐시 로드 실패(무시): %s", e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    def need(self, codes: list[str], pages: int) -> list[str]:
        want = pages * _ROWS_PER_PAGE * 0.9         # 상장 초기 종목은 못 채우니 여유
        return [c for c in codes if len(self.data.get(c, [])) < want]

    def warm(self, codes: list[str], pages: int, workers: int = 8) -> None:
        todo = self.need(codes, pages)
        if not todo:
            return
        logger.info("수급 이력 수집 %d종목 × %d페이지", len(todo), pages)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for code, rows in zip(todo, ex.map(lambda c: _safe_fetch(c, pages), todo)):
                if rows:
                    self.data[code] = rows
                done += 1
                if done % 50 == 0:
                    logger.info("  %d/%d", done, len(todo))
                    self.save()
        self.save()

    def rows(self, code: str, end_date: str | None = None) -> list[dict]:
        """end_date 이하(포함) 행만 — 백테스트 look-ahead 차단."""
        rs = self.data.get(code, [])
        return [r for r in rs if r["date"] <= end_date] if end_date else rs


def _safe_fetch(code: str, pages: int) -> list[dict]:
    try:
        return fetch_flow(code, pages)
    except Exception as e:                          # noqa: BLE001
        logger.warning("수급 %s 실패: %s", code, e)
        return []


# ── 지표 ────────────────────────────────────────────────
def tolerance_streak(nets: list[float], tol: float = TOL_RATIO,
                     max_blemish: int = MAX_BLEMISH) -> tuple[int, int]:
    """(스트릭 길이, 흠집 수). nets는 오름차순(마지막이 최근)."""
    streak = blemish = 0
    pos_sum = pos_cnt = 0
    total = 0.0
    for v in reversed(nets):
        if v > 0:
            streak += 1
            pos_sum += v
            pos_cnt += 1
            total += v
            continue
        avg = (pos_sum / pos_cnt) if pos_cnt else 0.0
        if pos_cnt and blemish < max_blemish and abs(v) < tol * avg and total + v > 0:
            streak += 1
            blemish += 1
            total += v
            continue
        break
    return (streak, blemish) if total > 0 else (0, 0)


def _hard_streak(nets: list[float]) -> int:
    s = 0
    for v in reversed(nets):
        if v > 0:
            s += 1
        else:
            break
    return s


REV_MAX_FLIP = 3     # 순매수 전환 인정 일수 — 이보다 길면 '전환 시점'이 아니라 스트릭 후보


def reversal(nets: list[float]) -> tuple[int, int]:
    """매도→매수 전환 감지: (전환 순매수일수, 직전 흠집허용 연속 순매도일수).

    최근 1~REV_MAX_FLIP일 순매수이고 그 직전이 연속 순매도(흠집 허용 — 매수 방향과
    같은 규칙을 부호만 뒤집어 적용)면 전환. 조건 미충족이면 (0, 0)."""
    flip = _hard_streak(nets)
    if not 1 <= flip <= REV_MAX_FLIP:
        return (0, 0)
    sell_streak, _ = tolerance_streak([-v for v in nets[:-flip]])
    return (flip, sell_streak)


def side_metrics(rows: list[dict], side: str, window: int = WINDOW) -> dict:
    """한 주체(frgn|inst)의 창 지표. rows는 오름차순, 마지막이 기준일."""
    w = rows[-window:]
    nets = [float(r[side]) for r in w]
    vols = [float(r["volume"]) for r in w]
    tot_vol = sum(vols) or 1.0
    net_sum = sum(nets)
    abs_sum = sum(abs(n) for n in nets) or 1.0
    streak, blemish = tolerance_streak(nets)
    rev_flip, rev_sell = reversal(nets)
    return {
        "net": net_sum,
        "intensity": net_sum / tot_vol,
        "persist": sum(1 for n in nets if n > 0) / len(nets),
        "streak": streak,
        "blemish": blemish,
        "concentr": max(abs(n) for n in nets) / abs_sum,
        "hard_streak": _hard_streak(nets),          # 비교용 — 옛 방식(무관용 연속)
        "rev_flip": rev_flip,                       # 매도→매수 전환: 전환 후 순매수일수
        "rev_sell": rev_sell,                       # 전환 직전 연속 순매도일수
    }


# ── KRX 투자자 상세 (금융투자·연기금 분리) ──────────────
# 네이버 '기관'은 기관합계라 금융투자(ETF LP·차익거래)를 못 걷어낸다. KRX 상세가
# 닿을 때만 기관합계−금융투자와 연기금을 얹는다. KRX는 클라우드 IP 상습 차단이고
# 지금은 로그인(KRX_ID/KRX_PW)까지 요구하므로 probe 실패 시 전체 폴백(기관합계 유지).
_detail_ok: bool | None = None


def detail_available() -> bool:
    """런당 1회 삼성전자로 KRX 상세 접근 probe. 실패하면 이번 런은 상세 없이 간다."""
    global _detail_ok
    if _detail_ok is None:
        import datetime as _dt
        end = _dt.date.today()
        start = end - _dt.timedelta(days=14)
        _detail_ok = bool(fetch_detail("005930", start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
        logger.info("KRX 투자자 상세: %s", "사용" if _detail_ok else "차단 — 기관합계로 폴백")
    return _detail_ok


def fetch_detail(code: str, start: str, end: str) -> dict[str, dict] | None:
    """{date: {"fin": 금융투자, "pension": 연기금}} 순매수 주수. 실패·빈 응답이면 None."""
    try:
        from pykrx import stock
        df = stock.get_market_trading_volume_by_date(start, end, code, detail=True)
        if df is None or df.empty:
            return None
        fin_col = next(c for c in df.columns if "금융투자" in str(c))
        pen_col = next(c for c in df.columns if "연기금" in str(c))
        return {d.strftime("%Y%m%d"): {"fin": int(r[fin_col]), "pension": int(r[pen_col])}
                for d, r in df.iterrows()}
    except Exception as e:                          # noqa: BLE001
        logger.debug("KRX 상세 %s 실패: %s", code, e)
        return None


def join_detail(rows: list[dict], detail: dict[str, dict], window: int = WINDOW) -> bool:
    """네이버 rows에 inst_xf(기관합계−금융투자)·pension을 날짜로 병합.
    창 안에 구멍이 하나라도 있으면 섞인 지표가 되므로 병합하지 않고 False."""
    w = rows[-window:]
    if any(r["date"] not in detail for r in w):
        return False
    for r in w:
        d = detail[r["date"]]
        r["inst_xf"] = r["inst"] - d["fin"]
        r["pension"] = d["pension"]
    return True


def metrics(rows: list[dict], window: int = WINDOW) -> dict | None:
    """양 주체 지표 + 합산. 데이터가 창을 못 채우면 None."""
    if len(rows) < window:
        return None
    f = side_metrics(rows, "frgn", window)
    i = side_metrics(rows, "inst", window)
    w = rows[-window:]
    tot_vol = sum(float(r["volume"]) for r in w) or 1.0
    return {
        "date": w[-1]["date"],
        "close": w[-1]["close"],
        "frgn": f,
        "inst": i,
        "both_intensity": (f["net"] + i["net"]) / tot_vol,
        "avg_volume": tot_vol / len(w),
    }
