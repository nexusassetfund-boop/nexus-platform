"""트레이더 핸드북 전략 백테스트 — 미국시장 5개년 (일봉).

KR판(handbook_backtest.py)과 **동일한 시뮬레이션 엔진**을 import 해 쓰고,
데이터 계층(유니버스·가격·벤치마크·펀더멘털)과 거래비용만 미국으로 교체한다.

  · 유니버스: S&P 500 + S&P MidCap 400 — Wikipedia 변경이력을 역롤백한 월별 PIT
              (KR의 KOSPI200+KOSDAQ150 대응. 대형+중형 조합)
  · 가격: yfinance 일봉 (auto_adjust=True, 배당·분할 반영)
  · 시장 게이트: QQQ (책 원문 그대로) / 벤치마크 비교: SPY·QQQ 둘 다
  · 펀더멘털: SEC XBRL frames API 의 us-gaap:NetIncomeLoss
              분기 YoY +25% ×2분기, 분기말+45일 래그 (KR DART 규칙과 동일)
  · 비용: 슬리피지 0.5%(KR과 동일 강도) + 왕복 수수료 0.02%, 증권거래세 없음

실행:
  HBT_CACHE=~/nexus-web/tmp python scanner/handbook_backtest_us.py --track both
  python scanner/handbook_backtest_us.py --track ema21 --limit-months 6   # 스모크
"""
from __future__ import annotations
import argparse
import io
import json
import logging
import os
import pickle
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))

import handbook_backtest as hb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("handbook_backtest_us")

ROOT = Path(__file__).parent.parent
CACHE = Path(os.environ.get("HBT_CACHE", str(ROOT / "tmp"))).expanduser()
CACHE.mkdir(parents=True, exist_ok=True)

PX_START, PX_END = "2020-01-01", "2026-08-09"
SIM_START, SIM_END = "2021-08-02", "2026-08-08"

UA = {"User-Agent": "nexus-research nexusassetfund@gmail.com"}

# 미국 거래비용으로 교체 (엔진이 hb 모듈 전역을 런타임 조회하므로 패치가 그대로 먹는다)
hb.SELL_TAX = 0.0
hb.COMMISSION_RT = 0.0002
hb.P["min_turnover"] = 3_000_000  # 책 원문 $3M (KR판은 30억으로 환산했었음)


# ── PIT 유니버스 (Wikipedia 변경이력 역롤백) ─────────────
WIKI = {
    "SP500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "SP400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
}


def _norm(t) -> str:
    return str(t).strip().upper().replace(".", "-").replace("\xa0", "")


_SUFFIX = (" INCORPORATED", " CORPORATION", " COMPANY", " HOLDINGS", " HOLDING", " GROUP",
           " INC", " CORP", " CO", " LTD", " LLC", " PLC", " LP", " NV", " SA", " AG",
           " CLASS A", " CLASS B", " CLASS C", " THE")


def _cname(s: str) -> str:
    """회사명 정규화 — 'Exxon Mobil Corporation' → 'EXXONMOBIL'."""
    s = str(s).upper().replace("&", " AND ").replace("/", " ")
    s = "".join(ch if ch.isalnum() or ch == " " else " " for ch in s)
    s = " ".join(s.split())
    if s.startswith("THE "):
        s = s[4:]
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIX:
            if s.endswith(suf):
                s, changed = s[: -len(suf)].strip(), True
    return s.replace(" ", "")


COMPANY_NAMES: dict[str, str] = {}   # 티커 → 회사명 (상폐 종목의 CIK 역추적용)


def _current_and_changes(url: str) -> tuple[set[str], list[tuple[pd.Timestamp, str, str]]]:
    """(현재 구성종목, [(변경일, 편입티커, 편출티커), ...])"""
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    cur: set[str] = set()
    changes: list[tuple[pd.Timestamp, str, str]] = []
    for t in tables:
        multi = isinstance(t.columns, pd.MultiIndex)
        cols = [str(c) for c in (t.columns.get_level_values(0) if multi else t.columns)]
        if not cur and not multi:
            key = next((c for c in ("Symbol", "Ticker symbol", "Ticker") if c in cols), None)
            if key:
                cur = {_norm(x) for x in t[key].dropna()}
                if "Security" in cols:
                    for tk, nm in zip(t[key], t["Security"]):
                        COMPANY_NAMES.setdefault(_norm(tk), str(nm))
        # 변경이력 표: S&P500 은 'Effective Date', S&P400 은 'Date'
        dcol = next((c for c in ("Date", "Effective Date") if c in cols), None)
        if multi and dcol and "Added" in cols and "Removed" in cols:
            for _, row in t.iterrows():
                d = pd.to_datetime(str(row[(dcol, dcol)]), errors="coerce")
                if pd.isna(d):
                    continue
                add = row[("Added", "Ticker")]
                rem = row[("Removed", "Ticker")]
                for tk, nm in ((add, row[("Added", "Security")]), (rem, row[("Removed", "Security")])):
                    if not pd.isna(tk) and not pd.isna(nm):
                        COMPANY_NAMES.setdefault(_norm(tk), str(nm))
                changes.append((d, "" if pd.isna(add) else _norm(add),
                                "" if pd.isna(rem) else _norm(rem)))
    if not cur or not changes:
        raise RuntimeError(f"위키 테이블 파싱 실패 (현재 {len(cur)} / 변경 {len(changes)}): {url}")
    return cur, changes


def build_pit_universe(month_keys: list[str]) -> tuple[dict[str, set], dict[str, str]]:
    """월별 PIT 구성종목. 현재 명단에서 변경이력을 시간 역순으로 되감는다."""
    cache = CACHE / "hbt_us_universe_v2.json"
    if cache.exists():
        d = json.loads(cache.read_text(encoding="utf-8"))
        if set(d["members"]) == set(month_keys):
            COMPANY_NAMES.update(d.get("names", {}))
            return {k: set(v) for k, v in d["members"].items()}, d["markets"]

    members: dict[str, set] = {k: set() for k in month_keys}
    markets: dict[str, str] = {}
    for tag, url in WIKI.items():
        cur, changes = _current_and_changes(url)
        changes.sort(key=lambda x: x[0], reverse=True)
        state, ci = set(cur), 0
        for k in sorted(month_keys, reverse=True):   # 최신 → 과거로 되감기
            kd = pd.Timestamp(k)
            while ci < len(changes) and changes[ci][0] > kd:
                _, add, rem = changes[ci]
                if add:
                    state.discard(add)
                if rem:
                    state.add(rem)
                ci += 1
            members[k] |= set(state)
        for t in set().union(*members.values()):
            markets.setdefault(t, tag)
        logger.info("%s: 현재 %d · 변경이력 %d건", tag, len(cur), len(changes))
    cache.write_text(json.dumps({"members": {k: sorted(v) for k, v in members.items()},
                                 "markets": markets, "names": COMPANY_NAMES}), encoding="utf-8")
    return members, markets


# ── 가격 (yfinance) ──────────────────────────────────────
def load_ohlcv_us(tickers: list[str]) -> dict[str, pd.DataFrame]:
    cache = CACHE / f"hbt_us_px_{len(tickers)}.pkl"
    if cache.exists():
        with open(cache, "rb") as f:
            return pickle.load(f)
    import yfinance as yf
    out: dict[str, pd.DataFrame] = {}
    chunk = 80
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = yf.download(part, start=PX_START, end=PX_END, auto_adjust=True,
                         progress=False, group_by="ticker", threads=True)
        for t in part:
            try:
                sub = df[t].dropna(how="all")
            except Exception:
                continue
            if sub is None or len(sub) < 260:
                continue
            sub = sub.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            sub.index = pd.to_datetime(sub.index).tz_localize(None)
            out[t] = sub
        logger.info("가격 %d/%d (확보 %d)", min(i + chunk, len(tickers)), len(tickers), len(out))
        time.sleep(0.5)
    with open(cache, "wb") as f:
        pickle.dump(out, f)
    return out


# ── 펀더멘털 (SEC XBRL frames) ───────────────────────────
class SECStore:
    """분기/연간 NetIncomeLoss 를 CY 프레임 단위로 통째 받아 캐싱 (요청 ~30회)."""

    def __init__(self):
        self.dir = CACHE / "sec_frames"
        self.dir.mkdir(exist_ok=True)
        self.cik = self._ticker_cik()
        self._frames: dict[str, dict[int, float]] = {}
        self._names: dict[str, int] | None = None
        self._resolved: dict[str, list[int]] = {}

    def _get(self, url: str, path: Path):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        r = requests.get(url, headers=UA, timeout=90)
        if r.status_code != 200:
            logger.warning("SEC %s → %s", url.rsplit("/", 1)[-1], r.status_code)
            path.write_text("{}", encoding="utf-8")
            return {}
        path.write_text(r.text, encoding="utf-8")
        time.sleep(0.3)
        return r.json()

    def _ticker_cik(self) -> dict[str, int]:
        d = self._get("https://www.sec.gov/files/company_tickers.json",
                      self.dir / "company_tickers.json")
        return {_norm(v["ticker"]): int(v["cik_str"]) for v in d.values()}

    def _name_index(self) -> dict[str, int]:
        """프레임의 entityName → cik. company_tickers.json 에 없는 상폐·구CIK 종목 보완용."""
        if self._names is not None:
            return self._names
        idx: dict[str, int] = {}
        for tag in ("NetIncomeLoss", "ProfitLoss"):
            for f in sorted(self.dir.glob(f"{tag}_CY*Q*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for r in d.get("data", []):
                    idx.setdefault(_cname(r["entityName"]), int(r["cik"]))
        self._names = idx
        return idx

    def candidates(self, ticker: str) -> list[int]:
        """티커 → CIK 후보. 공식 매핑 + 회사명 역추적(상폐·CIK 변경 기업 보완)."""
        if ticker in self._resolved:
            return self._resolved[ticker]
        out: list[int] = []
        c = self.cik.get(ticker)
        if c:
            out.append(c)
        nm = COMPANY_NAMES.get(ticker)
        if nm:
            key = _cname(nm)
            idx = self._name_index()
            alt = idx.get(key)
            if alt is None and len(key) >= 6:     # 접두 일치가 유일할 때만 채택
                hits = {v for k, v in idx.items() if k.startswith(key)}
                alt = next(iter(hits)) if len(hits) == 1 else None
            if alt is not None and alt not in out:
                out.append(alt)
        self._resolved[ticker] = out
        return out

    def _raw(self, tag: str, cy: str) -> dict[int, float]:
        d = self._get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{cy}.json",
                      self.dir / f"{tag}_{cy}.json")
        return {int(r["cik"]): float(r["val"]) for r in d.get("data", [])}

    def frame(self, cy: str) -> dict[int, float]:
        """cy = 'CY2023Q1'(분기) 또는 'CY2023'(연간) → {cik: net income}

        · NetIncomeLoss 우선, 미태깅 기업은 ProfitLoss 로 보완
        · 회계 4분기는 대부분 10-K 안에만 있어 분기 프레임이 비어 있음
          → 연간 − (Q1+Q2+Q3) 으로 유도 (KR판이 누적공시를 차분한 것과 동일 처리)
        """
        if cy in self._frames:
            return self._frames[cy]
        m = dict(self._raw("ProfitLoss", cy))
        m.update(self._raw("NetIncomeLoss", cy))
        if cy.endswith("Q4"):
            y = cy[:6]
            ann = self.frame(y)
            q13: dict[int, float] = {}
            counts: dict[int, int] = {}
            for qi in ("Q1", "Q2", "Q3"):
                for k, v in self.frame(y + qi).items():
                    q13[k] = q13.get(k, 0.0) + v
                    counts[k] = counts.get(k, 0) + 1
            for k, a in ann.items():
                if k not in m and counts.get(k) == 3:
                    m[k] = a - q13[k]
        self._frames[cy] = m
        logger.info("SEC frame %s: %d개사", cy, len(m))
        return m

    def prefetch(self):
        """필요한 CY 프레임을 먼저 전부 받아둔다 (이름→CIK 역인덱스가 전 구간을 보게)."""
        for y in range(2019, 2027):
            self.frame(f"CY{y}")
            for q in range(1, 5):
                if (y, q) <= (2026, 2):
                    self.frame(f"CY{y}Q{q}")

    def yoy(self, ticker: str, cy: str) -> float | None:
        cs = self.candidates(ticker)
        if not cs:
            return None
        fa = self.frame(cy)
        fb = self.frame(f"CY{int(cy[2:6]) - 1}{cy[6:]}")
        a = next((fa[c] for c in cs if c in fa), None)
        b = next((fb[c] for c in cs if c in fb), None)
        if a is None or b is None or b <= 0:      # 적자→흑자 전환은 YoY% 무의미 → 제외
            return None
        return (a - b) / b * 100


def cy_quarter_for(sig: pd.Timestamp) -> str:
    """신호일 기준, 분기말+45일이 지나 공시가 나왔을 최신 캘린더 분기."""
    d = sig - pd.Timedelta(days=45)
    y, q = d.year, (d.month - 1) // 3 - 1      # 진행 중 분기의 직전(완료) 분기
    if q < 0:
        y, q = y - 1, 3
    return f"CY{y}Q{q + 1}"


def cy_year_for(sig: pd.Timestamp) -> str:
    """연간보고서는 회계연도말+90일 래그."""
    return f"CY{(sig - pd.Timedelta(days=90)).year - 1}"


def fund_flags_us(store: SECStore, t: str, sig: pd.Timestamp) -> tuple[bool, bool]:
    cq = cy_quarter_for(sig)
    y, q = int(cq[2:6]), int(cq[-1])
    py, pq = (y, q - 1) if q > 1 else (y - 1, 4)
    g1, g2 = store.yoy(t, cq), store.yoy(t, f"CY{py}Q{pq}")
    ok = g1 is not None and g2 is not None and g1 >= hb.P["q_yoy"] and g2 >= hb.P["q_yoy"]
    ga = store.yoy(t, cy_year_for(sig))
    return ok, bool(ga is not None and ga >= hb.P["q_yoy"])


# ── 메인 ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", default="both", choices=["ema21", "sma50", "both"])
    ap.add_argument("--limit-months", type=int, default=0)
    ap.add_argument("--no-fund", action="store_true")
    ap.add_argument("--slip-mult", type=float, default=1.0)
    ap.add_argument("--gate", default="book", choices=["book", "simple"])
    args = ap.parse_args()
    hb.GATE_MODE = args.gate

    # 월별 신호일 — KR판의 krx_pit 덤프 키와 같은 역할
    month_keys = [pd.Timestamp(PX_START).strftime("%Y%m%d")] + \
                 [d.strftime("%Y%m%d") for d in pd.date_range(SIM_START, SIM_END, freq="MS")]

    members, markets = build_pit_universe(month_keys)
    all_t = sorted(set().union(*members.values()))
    logger.info("PIT 유니버스: 월 %d개, 누적 %d종목", len(month_keys), len(all_t))

    raw = load_ohlcv_us(all_t)
    ind = {t: hb.precompute(df) for t, df in raw.items()}
    logger.info("가격 확보 %d/%d (260봉 미달·상폐 %d 제외)", len(ind), len(all_t), len(all_t) - len(ind))

    import yfinance as yf
    bmk = {}
    for sym in ("QQQ", "SPY"):
        b = yf.download(sym, start=PX_START, end=PX_END, auto_adjust=True, progress=False)
        b.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in b.columns]
        b.index = pd.to_datetime(b.index).tz_localize(None)
        bmk[sym] = b

    q = bmk["QQQ"]
    bench_ind = pd.DataFrame({
        "close": q["close"],
        "low": q["low"],
        "ema21": q["close"].ewm(span=21, adjust=False).mean(),
        "sma50": q["close"].rolling(50).mean(),
    })
    for m in ("ema21", "sma50"):
        bench_ind[f"{m}_prev"] = bench_ind[m].shift(5)

    end = SIM_END
    if args.limit_months:
        end = str((pd.Timestamp(SIM_START) + pd.DateOffset(months=args.limit_months)).date())
    days = [d for d in bench_ind.index if SIM_START <= str(d.date()) <= end]

    keys = sorted(members.keys())
    used_keys = sorted({hb.key_for(keys, d) for d in days})
    rs_by_key = {k: hb.make_rs(ind, members[k], markets, pd.Timestamp(k)) for k in used_keys}
    logger.info("RS 계산 완료 (%d개 신호일)", len(used_keys))

    if args.no_fund:
        fund_by_key = {k: {t: (True, False) for t in members[k]} for k in used_keys}
    else:
        store, fund_by_key = SECStore(), {}
        store.prefetch()
        for n, k in enumerate(used_keys):
            sig, rs, fk = pd.Timestamp(k), rs_by_key[k], {}
            for t in members[k]:
                fk[t] = (False, False) if rs.get(t, 0) < hb.P["pre_rs"] else fund_flags_us(store, t, sig)
            fund_by_key[k] = fk
            logger.info("SEC %d/%d (%s) 통과 %d", n + 1, len(used_keys), k,
                        sum(1 for v in fk.values() if v[0]))

    names = {t: t for t in all_t}
    for tr in (["ema21", "sma50"] if args.track == "both" else [args.track]):
        nav, trades, open_pos = hb.simulate(tr, days, ind, bench_ind, keys, members,
                                            names, markets, rs_by_key, fund_by_key,
                                            slip_mult=args.slip_mult)
        m = hb.metrics(nav, trades, bmk["SPY"]["close"], open_pos)
        qq = bmk["QQQ"]["close"].reindex(nav.index).ffill().dropna()
        m["bench_spy_pct"] = m.pop("bench_total_pct")
        m["bench_qqq_pct"] = round((qq.iloc[-1] / qq.iloc[0] - 1) * 100, 1)
        tag = (f"_{args.gate}") + (f"_{args.slip_mult}" if args.slip_mult != 1.0 else "")
        out = CACHE / f"hbt_us_result_{tr}{tag}.json"
        out.write_text(json.dumps({"metrics": m, "trades": trades,
                                   "nav": {str(d.date()): round(v, 6) for d, v in nav.items()}},
                                  ensure_ascii=False), encoding="utf-8")
        logger.info("[US %s] 거래 %s건 승률 %s%% 총수익 %s%% MDD %s%% (SPY %s%% / QQQ %s%%) → %s",
                    tr, m["trades"], m["win_rate"], m["total_return_pct"], m["mdd_pct"],
                    m["bench_spy_pct"], m["bench_qqq_pct"], out.name)


if __name__ == "__main__":
    main()
