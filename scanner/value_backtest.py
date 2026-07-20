"""
가치투자 자동 발굴 후보(value_screen.py) 기준 월간 리밸런싱 백테스트.

전략 (실서비스 value_screen.build 1·2차 파이프라인과 동일 기준):
  유니버스: 코스피200+코스닥150 — 각 신호일 시점 구성(KRX 포인트인타임 덤프 krx_pit_*.json)
  필터: 시총>=3,000억, EPS·BPS 양수, PER<=15, PBR<=1.5,
        안전마진 = (적정가-현재가)/적정가 >= 15% (적정가 = RIM·Graham 보수값)
  퀄리티: 마진 상위 40종목만 Piotroski F-Score (포인트인타임 사업연도), <5 탈락(미산출 통과)
  선정: (F-Score, 마진) 정렬 상위 20 동일비중

시뮬레이션:
  신호 = 전월 마지막 거래일 종가 기준 스크린 → 익월 첫 거래일 시가 체결
  체결 비용 = backtest.py와 동일 (±0.5% 슬리피지, 매도세 0.23%, 왕복 수수료 0.03%는 매도 시 부과)
  드리프트 모드(기본): 상위 20 잔류 종목 보유 유지, 탈락 매도, 신규 NAV/20 매수
  후보<20 → 잔여 현금 보유(무이자)

포인트인타임:
  - 펀더멘털/시총/지수구성: KRX 정보데이터시스템 덤프 (로그인 브라우저에서 사전 수집, ~/krx_pit_*.json)
    각 신호일 당시 공시 기준 EPS/BPS — look-ahead 없음. pykrx는 KRX 로그인 의무화로 사용 불가.
  - F-Score 사업연도: 신호일이 report_month(기본 5월) 이후면 직전 연도, 이전이면 전전 연도
  - 모든 데이터 조회일 <= 신호일 assert

실서비스와 차이 (리포트 명시):
  - 기존 유니버스/포트폴리오 제외(_existing_codes) 미적용 — 순수 전략 성과
  - 주간 스크린 → 월간 리밸런싱, 매도 규칙(스크린 이탈 시 매도)은 백테스트 전용
  - KIS 폴백·기술적 지표 미사용
  - 상폐 종목은 FDR에서 시세 미제공 → "선정됐지만 체결 불가"로 정량화 (생존편향 상방)

실행:
  python scanner/value_backtest.py --probe          # 0단계: KRX 덤프 커버리지 확인
  python scanner/value_backtest.py                  # 기본 백테스트 (2021-07 ~ 어제)
  python scanner/value_backtest.py --grid           # OFAT robustness 배치
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest import BUY_SLIP, SELL_SLIP, SELL_TAX, COMMISSION_RT, CACHE_DIR, load_kospi
from run_scan import _calc_fair_value, _fnum
from value_screen import (MIN_CAP, PER_MAX, PBR_MAX, MARGIN_MIN, FSCORE_MIN, TOP_FSCORE, TOP_OUT,
                          MOM_WIN, MOM_SKIP)
import fetch_value

logger = logging.getLogger("value_backtest")
ROOT = Path(__file__).parent.parent

FSCORE_CACHE_PATH = CACHE_DIR / "vbt_fscore.json"
KRX_DUMP_GLOB = str(Path.home() / "krx_pit_*.json")

DART_SLEEP = 0.15   # DART 호출 간격 (분당 한도 보호)


def _base_params() -> dict:
    return {
        "min_cap": MIN_CAP, "per_max": PER_MAX, "pbr_max": PBR_MAX,
        "margin_min": MARGIN_MIN, "fscore_min": FSCORE_MIN,
        "top_fscore": TOP_FSCORE, "top": TOP_OUT,
        "report_month": 5, "use_fscore": True,
        # sort: fscore_margin(실서비스 원본) | momentum(밸류 관문 + 모멘텀 정렬, F-Score는 컷 전용)
        # mom_win/skip은 실서비스(value_screen.py)의 12-1 모멘텀과 동일하게 맞춤 —
        # --sort momentum 시 별도 플래그 없이도 라이브 전략을 그대로 재현.
        "sort": "fscore_margin", "mom_win": MOM_WIN, "mom_skip": MOM_SKIP,
    }


# ── 캘린더 ───────────────────────────────────────────────
def build_calendar(start: str, end: str):
    """FDR KS11 거래일 → (전체 거래일, [(신호일=전월 마지막 거래일, 체결일=익월 첫 거래일)])"""
    k = load_kospi(start, end)
    days = list(k.index)
    rebals = []
    for i in range(1, len(days)):
        if days[i].month != days[i - 1].month:
            rebals.append((days[i - 1], days[i]))
    return days, rebals


# ── KRX 포인트인타임 덤프 (유니버스·펀더멘털·시총) ───────
def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _knum(s, allow_zero: bool = False):
    """KRX 덤프 셀 → float. 콤마 제거, ''/'-'/0(allow_zero=False) → None."""
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v == 0 and not allow_zero:
        return None
    return v


_krx_dumps: dict | None = None


def load_krx_dumps(pattern: str | None = None) -> dict:
    """~/krx_pit_*.json (로그인 브라우저에서 수집한 KRX 신호일별 덤프) 병합 로드.
    구조: {YYYYMMDD: {fund:[[code,EPS,BPS,PER,PBR,DIV]], cap:[[code,name,mkt,close,mktcap]],
                     k200:[code], q150:[code]}}  — 일부 파일은 이중 인코딩(json.loads 2회)."""
    global _krx_dumps
    if _krx_dumps is not None:
        return _krx_dumps
    import glob as _glob
    dumps = {}
    for fp in sorted(_glob.glob(pattern or KRX_DUMP_GLOB)):
        if "test" in Path(fp).stem:
            continue
        try:
            d = json.loads(Path(fp).read_text(encoding="utf-8"))
            if isinstance(d, str):
                d = json.loads(d)
        except Exception as e:
            logger.warning("KRX 덤프 파싱 실패 %s: %s", fp, e)
            continue
        for k, v in d.items():
            if not k.startswith("_") and isinstance(v, dict):
                dumps[k] = v
    if not dumps:
        raise RuntimeError(f"KRX 덤프 없음: {pattern or KRX_DUMP_GLOB}")
    _krx_dumps = dumps
    logger.info("KRX 덤프 %d개 신호일 로드 (%s ~ %s)", len(dumps), min(dumps), max(dumps))
    return dumps


def _dump_key_for(sig_date: pd.Timestamp) -> str:
    """신호일에 대응하는 덤프 키. 정확 일치 우선, 없으면 7일 이내 과거로 소급(look-ahead 없음)."""
    ds = sig_date.strftime("%Y%m%d")
    dumps = load_krx_dumps()
    if ds in dumps:
        return ds
    cands = [k for k in dumps if k <= ds and (sig_date - pd.Timestamp(k)).days <= 7]
    if cands:
        return max(cands)
    raise RuntimeError(f"신호일 {ds} 대응 KRX 덤프 없음")


def fetch_universe_pit(sig_date: pd.Timestamp):
    """신호일 시점 코스피200+코스닥150 구성 + 당시 종목명. 반환: (constituents[(code,name)], src)"""
    v = load_krx_dumps()[_dump_key_for(sig_date)]
    names = {str(r[0]).zfill(6): str(r[1]) for r in v["cap"]}
    tickers, seen = [], set()
    for t in list(v["k200"]) + list(v["q150"]):
        t = str(t).zfill(6)
        if t not in seen:
            seen.add(t)
            tickers.append((t, names.get(t, t)))
    if len(tickers) < 100:
        raise RuntimeError(f"{sig_date.date()} 유니버스 {len(tickers)}종목 — 덤프 손상 의심")
    return tickers, "pit"


def fetch_snapshot(sig_date: pd.Timestamp):
    """신호일 기준 전 종목 (fund, cap) 스냅샷 — KRX 덤프에서 로드."""
    key = _dump_key_for(sig_date)
    v = load_krx_dumps()[key]
    fund = {str(r[0]).zfill(6): {
        "eps": _knum(r[1]), "bps": _knum(r[2]),
        "per": _knum(r[3]), "pbr": _knum(r[4]),
        "div": _knum(r[5], allow_zero=True),
    } for r in v["fund"]}
    cap = {str(r[0]).zfill(6): {
        "close": _knum(r[3]), "cap": _knum(r[4]),
    } for r in v["cap"]}
    valid = sum(1 for x in fund.values() if x["eps"])
    if valid < 100:
        raise RuntimeError(f"스냅샷 {key} 유효 EPS {valid} — 덤프 손상 의심")
    return fund, cap, key


# ── F-Score (포인트인타임) ───────────────────────────────
def fiscal_year_for(sig_date: pd.Timestamp, report_month: int = 5) -> int:
    """신호일 시점에 공시가 확정된 최근 사업연도(12월 결산, 제출기한 3/31 + 1개월 여유)."""
    return sig_date.year - 1 if sig_date.month >= report_month else sig_date.year - 2


class FScoreStore:
    def __init__(self):
        self.cache = _load_json(FSCORE_CACHE_PATH)
        self.corp_map = fetch_value._corp_map() if fetch_value.DART_KEY else {}
        self.calls = 0

    def get(self, code: str, year: int):
        k = f"{code}:{year}"
        if k in self.cache:
            return self.cache[k]
        corp = self.corp_map.get(code)
        fs = None
        if corp:
            fs = fetch_value._fscore(corp, year)
            self.calls += 1
            time.sleep(DART_SLEEP)
        self.cache[k] = fs
        return fs

    def save(self):
        FSCORE_CACHE_PATH.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")


# ── 스크리닝 (value_screen.build 1·2차 이식) ─────────────
def _value_gate(universe, fund, cap, p) -> list[dict]:
    """1차 밸류 관문 — 시총·흑자·PER/PBR·안전마진. 정렬 없이 통과 종목 반환."""
    prelim = []
    for code, name in universe:
        f = fund.get(code)
        c = cap.get(code, {})
        if not f or not c.get("close"):
            continue
        if c.get("cap") is None or c["cap"] < p["min_cap"]:
            continue
        per, pbr, eps, bps = f["per"], f["pbr"], f["eps"], f["bps"]
        if not eps or not bps or eps <= 0 or bps <= 0:  # 적자·자본잠식 명시 배제(라이브 _fnum과 동일)
            continue
        price = c["close"]
        per = per or round(price / eps, 1)
        pbr = pbr or round(price / bps, 2)
        if per > p["per_max"] or pbr > p["pbr_max"]:
            continue
        roe = round(eps / bps * 100, 1)
        fair, _, _ = _calc_fair_value(eps, bps, roe)
        if not fair:
            continue
        margin = round((fair - price) / fair * 100, 1)
        if margin < p["margin_min"]:
            continue
        prelim.append({"code": code, "name": name, "margin": margin, "per": per, "pbr": pbr})
    return prelim


def _apply_fscore_cut(sig_date, shortlist, p, fstore: FScoreStore | None) -> list[dict]:
    """F-Score < fscore_min 탈락 (미산출 통과). rec['f_score'] 부여."""
    if p["use_fscore"] and fstore and fstore.corp_map:
        fy = fiscal_year_for(sig_date, p["report_month"])
        survivors = []
        for rec in shortlist:
            fs = fstore.get(rec["code"], fy)
            rec["f_score"] = fs["score"] if fs else None
            if fs and fs["score"] < p["fscore_min"]:
                continue
            survivors.append(rec)
        return survivors
    for rec in shortlist:
        rec["f_score"] = None
    return shortlist


def screen_at(sig_date, universe, fund, cap, p, fstore: FScoreStore | None, mom=None):
    """신호일 스냅샷 스크린 → (선정 리스트, 밸류 통과 수).
    sort=fscore_margin: 실서비스 원본 — 마진 상위 40 F-Score 컷 → (F-Score, 마진) 정렬.
    sort=momentum: 모멘텀 상위 40 F-Score 컷(컷 전용) → 모멘텀 정렬. mom(code)->float|None 필요."""
    prelim = _value_gate(universe, fund, cap, p)

    if p["sort"] == "momentum":
        assert mom is not None, "momentum 정렬에는 mom 조회 함수 필요"
        for rec in prelim:
            rec["mom"] = mom(rec["code"])
        ranked = sorted((r for r in prelim if r["mom"] is not None),
                        key=lambda r: r["mom"], reverse=True)
        survivors = _apply_fscore_cut(sig_date, ranked[:p["top_fscore"]], p, fstore)
        return survivors[:p["top"]], len(prelim)

    if p["sort"] == "qvm":
        # 복합 랭크 — 밸류(안전마진)·퀄리티(F-Score)·모멘텀(12-1) 백분위 평균. 컷 없음, 결측은 중립(0.5).
        assert mom is not None, "qvm 정렬에는 mom 조회 함수 필요"
        for rec in prelim:
            rec["mom"] = mom(rec["code"])
        pool = [r for r in prelim if r["mom"] is not None]
        pool = _apply_fscore_cut(sig_date, pool, {**p, "fscore_min": -10}, fstore)  # 점수 부여만
        def _pct(key):
            vals = sorted(r[key] for r in pool if r[key] is not None)
            n = len(vals)
            return lambda r: 0.5 if r[key] is None or n < 2 else vals.index(r[key]) / (n - 1)
        pm, pf, pmo = _pct("margin"), _pct("f_score"), _pct("mom")
        for r in pool:
            r["qvm"] = round((pm(r) + pf(r) + pmo(r)) / 3, 4)
        pool.sort(key=lambda r: r["qvm"], reverse=True)
        return pool[:p["top"]], len(prelim)

    prelim.sort(key=lambda x: x["margin"], reverse=True)
    survivors = _apply_fscore_cut(sig_date, prelim[:p["top_fscore"]], p, fstore)
    if p["sort"] != "margin":  # margin: F-Score는 컷 전용, 마진 순 유지
        survivors.sort(key=lambda r: (r["f_score"] if r["f_score"] is not None else -1, r["margin"]),
                       reverse=True)
    return survivors[:p["top"]], len(prelim)


def run_screens(rebals, p, fstore, mom_closes: pd.DataFrame | None = None):
    """전체 리밸런싱 신호일에 대해 스크린 실행 → [{sig, ex, selected, n_prelim, uni_src}]
    mom_closes: momentum 정렬용 종가 (신호일 이전 구간만 사용 — look-ahead 없음)."""
    out = []
    for sig, ex in rebals:
        assert sig < ex, "신호일이 체결일보다 늦음"
        uni, src = fetch_universe_pit(sig)
        fund, cap, snap_d = fetch_snapshot(sig)
        assert snap_d <= sig.strftime("%Y%m%d"), "look-ahead: 스냅샷이 신호일 이후"
        mom = _make_mom(mom_closes, sig, p) if p["sort"] in ("momentum", "qvm") else None
        sel, n_prelim = screen_at(sig, uni, fund, cap, p, fstore, mom)
        out.append({"sig": sig, "ex": ex, "selected": sel, "n_prelim": n_prelim, "uni_src": src})
        if fstore:
            fstore.save()
        logger.info("%s 스크린: 밸류통과 %d → 선정 %d (유니버스 %s)",
                    sig.date(), n_prelim, len(sel), src)
    return out


def _make_mom(closes: pd.DataFrame, sig: pd.Timestamp, p):
    """신호일 기준 모멘텀 조회 함수. mom = P(t-skip)/P(t-skip-win) - 1 (해당 종목 거래일 기준).
    이력 부족(신규상장 등)·시세 없음 → None (정렬 대상에서 제외)."""
    win, skip = p["mom_win"], p["mom_skip"]
    upto = closes[closes.index <= sig]

    def mom(code: str):
        if code not in upto.columns:
            return None
        s = upto[code].dropna()
        if len(s) < win + skip + 1:
            return None
        a, b = float(s.iloc[-1 - skip]), float(s.iloc[-1 - skip - win])
        return a / b - 1 if b > 0 else None

    return mom


def collect_prelim_codes(rebals, p) -> set[str]:
    """momentum 정렬 사전 단계 — 전체 신호일의 밸류 관문 통과 종목 합집합 (가격 다운로드 대상)."""
    codes = set()
    for sig, _ in rebals:
        uni, _src = fetch_universe_pit(sig)
        fund, cap, _d = fetch_snapshot(sig)
        for rec in _value_gate(uni, fund, cap, p):
            codes.add(rec["code"])
    return codes


# ── 가격 (FDR, 상폐 정량화) ──────────────────────────────
def load_prices(tickers: set[str], start: str, end: str):
    """선정된 적 있는 종목만 FDR 다운로드 (증분 pkl 캐시).
    반환: (opens, closes, missing) — missing = 시세 자체가 없는 종목(대부분 상폐)."""
    cache = CACHE_DIR / f"vbt_px_{start}_{end}.pkl"
    data = {}
    if cache.exists():
        with open(cache, "rb") as f:
            data = pickle.load(f)
    new = [t for t in tickers if t not in data]
    if new:
        import FinanceDataReader as fdr
        for i, t in enumerate(new):
            try:
                df = fdr.DataReader(t, start, end)
                if df is not None and len(df) >= 20:
                    df = df.rename(columns=str.lower)[["open", "close"]].astype(float)
                    data[t] = df
                else:
                    data[t] = None
            except Exception:
                data[t] = None
            if (i + 1) % 25 == 0:
                logger.info("가격 다운로드 %d/%d", i + 1, len(new))
        with open(cache, "wb") as f:
            pickle.dump(data, f)
    have = {t: d for t, d in data.items() if t in tickers and d is not None}
    missing = sorted(t for t in tickers if data.get(t) is None)
    opens = pd.DataFrame({t: d["open"] for t, d in have.items()})
    closes = pd.DataFrame({t: d["close"] for t, d in have.items()})
    return opens, closes, missing


def load_bench(start: str, end: str) -> pd.Series:
    cache = CACHE_DIR / f"vbt_bench_{start}_{end}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    import FinanceDataReader as fdr
    for sym in ("KS200", "069500"):
        try:
            s = fdr.DataReader(sym, start, end)["Close"].astype(float)
            if len(s) > 100:
                s.name = sym
                s.to_pickle(cache)
                return s
        except Exception:
            continue
    raise RuntimeError("벤치마크(KS200/KODEX200) 로드 실패")


# ── 시뮬레이션 ───────────────────────────────────────────
def simulate(days, screens, opens, closes, p, slip_mult=1.0, full_rebalance=False):
    buy_slip, sell_slip = BUY_SLIP * slip_mult, SELL_SLIP * slip_mult
    sell_cost = SELL_TAX + COMMISSION_RT  # 매도세 + 왕복 수수료(매도 시 일괄)
    top_n = p["top"]

    closes_ff = closes.ffill()
    last_valid = {t: closes[t].last_valid_index() for t in closes.columns}
    rebal_map = {s["ex"]: s for s in screens}
    first_ex = screens[0]["ex"] if screens else None

    cash, pos = 1.0, {}   # pos: code -> {shares, cost, entry, name}
    nav_hist, hold_hist, cash_hist = [], [], []
    trades, traded_notional = [], 0.0
    n_forced = n_unpriced = n_unfilled = 0

    def px_open(code, d):
        if code in opens.columns and d in opens.index:
            v = opens.at[d, code]
            if not np.isnan(v) and v > 0:
                return v
        return None

    def px_last_close(code, d):
        if code in closes_ff.columns and d in closes_ff.index:
            v = closes_ff.at[d, code]
            if not np.isnan(v) and v > 0:
                return v
        return None

    def sell(code, fill, d, reason):
        nonlocal cash, traded_notional
        h = pos.pop(code)
        net = fill * (1 - sell_cost)
        cash += h["shares"] * net
        traded_notional += h["shares"] * fill
        trades.append({"ticker": code, "name": h["name"],
                       "entry_date": str(h["entry"].date()), "exit_date": str(d.date()),
                       "ret": round((net / h["cost"] - 1) * 100, 2),
                       "days": (d - h["entry"]).days, "reason": reason})

    for d in days:
        if first_ex is None or d < first_ex:
            continue
        # 0) 상폐/시세 단절 강제 청산 (마지막 유효 종가로)
        for code in list(pos):
            lv = last_valid.get(code)
            if lv is not None and d > lv:
                sell(code, closes_ff.at[lv, code] * (1 - sell_slip), d, "delisted")
                n_forced += 1

        s = rebal_map.get(d)
        if s:
            target = {r["code"]: r for r in s["selected"]}
            # 1) 스크린 이탈 종목 매도 (체결일 시가)
            for code in list(pos):
                if code in target:
                    continue
                o = px_open(code, d) or px_last_close(code, d)
                if o:
                    sell(code, o * (1 - sell_slip), d, "screen_exit")
                else:
                    n_unfilled += 1  # 시가·종가 모두 없음 — 다음 기회/상폐 처리로 이월
            # 2) 체결 시점 NAV (시가 기준)
            nav_open = cash + sum(h["shares"] * (px_open(c, d) or px_last_close(c, d) or h["cost"])
                                  for c, h in pos.items())
            # 3) full-rebalance: 잔류 종목도 NAV/top_n으로 재조정
            if full_rebalance:
                for code in list(pos):
                    o = px_open(code, d)
                    if not o:
                        continue
                    h = pos[code]
                    tgt_shares = (nav_open / top_n) / o
                    diff = tgt_shares - h["shares"]
                    if diff < 0:  # 초과분 매도
                        fill = o * (1 - sell_slip)
                        cash += -diff * fill * (1 - sell_cost)
                        traded_notional += -diff * fill
                        h["shares"] = tgt_shares
                    elif diff > 0:  # 부족분 매수
                        fill = o * (1 + buy_slip)
                        cost_add = diff * fill
                        if cost_add <= cash:
                            h["cost"] = (h["cost"] * h["shares"] + fill * diff) / tgt_shares
                            h["shares"] = tgt_shares
                            cash -= cost_add
                            traded_notional += cost_add
            # 4) 신규 편입 매수 — 종목당 NAV/top_n, 현금 부족 시 비례 축소
            buys = []
            for code, rec in target.items():
                if code in pos:
                    continue
                if code not in closes.columns:
                    n_unpriced += 1  # FDR 시세 없음(상폐 등) — 선정됐지만 체결 불가
                    continue
                o = px_open(code, d)
                if not o:
                    n_unfilled += 1
                    continue
                buys.append((code, rec, o))
            budget_each = nav_open / top_n
            need = budget_each * len(buys)
            scale = min(1.0, cash / need) if need > 0 else 0.0
            for code, rec, o in buys:
                fill = o * (1 + buy_slip)
                shares = budget_each * scale / fill
                if shares <= 0:
                    continue
                cash -= shares * fill
                traded_notional += shares * fill
                pos[code] = {"shares": shares, "cost": fill, "entry": d, "name": rec["name"]}

        # 5) 일일 평가 (종가)
        nav = cash + sum(h["shares"] * (px_last_close(c, d) or h["cost"]) for c, h in pos.items())
        nav_hist.append((d, nav))
        hold_hist.append((d, len(pos)))
        cash_hist.append((d, cash / nav if nav > 0 else 0.0))

    # 종료 시점 청산 없이 평가 유지 (보유 중 포지션은 open trade로 별도 카운트)
    nav = pd.Series(dict(nav_hist)).sort_index()
    aux = {
        "holdings": pd.Series(dict(hold_hist)).sort_index(),
        "cash_w": pd.Series(dict(cash_hist)).sort_index(),
        "traded_notional": traded_notional,
        "n_forced": n_forced, "n_unpriced": n_unpriced, "n_unfilled": n_unfilled,
        "open_positions": len(pos),
    }
    return nav, trades, aux


# ── 지표 ─────────────────────────────────────────────────
def metrics(nav: pd.Series, trades, bench: pd.Series, screens, aux, missing):
    daily = nav.pct_change().dropna()
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 1e-9)
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    mdd = (nav / nav.cummax() - 1).min()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0.0

    b = bench.reindex(nav.index).ffill().dropna()
    b_ret = b.iloc[-1] / b.iloc[0] - 1
    b_cagr = (1 + b_ret) ** (1 / years) - 1

    m_nav = nav.resample("ME").last()
    monthly = (m_nav.pct_change().dropna() * 100).round(2)
    y_nav = nav.resample("YE").last()
    y_first = nav.iloc[0]
    yearly = {}
    prev = y_first
    for d, v in y_nav.items():
        yearly[d.year] = round((v / prev - 1) * 100, 1)
        prev = v

    tdf = pd.DataFrame(trades) if trades else pd.DataFrame(columns=["ret", "days", "reason"])
    wins = tdf[tdf.ret > 0] if len(tdf) else tdf
    losses = tdf[tdf.ret <= 0] if len(tdf) else tdf
    n_short = sum(1 for s in screens if len(s["selected"]) < s.get("top", 20))
    uni_srcs = pd.Series([s["uni_src"] for s in screens]).value_counts().to_dict()

    return {
        "period": f"{nav.index[0].date()} ~ {nav.index[-1].date()} ({years:.1f}y)",
        "total_return_pct": round((nav.iloc[-1] / nav.iloc[0] - 1) * 100, 1),
        "cagr_pct": round(cagr * 100, 2),
        "mdd_pct": round(mdd * 100, 1),
        "sharpe": round(sharpe, 2),
        "bench_total_pct": round(b_ret * 100, 1),
        "bench_cagr_pct": round(b_cagr * 100, 2),
        "excess_cagr_pct": round((cagr - b_cagr) * 100, 2),
        "turnover_annual_pct": round(aux["traded_notional"] / 2 / nav.mean() / years * 100, 1),
        "avg_holdings": round(float(aux["holdings"].mean()), 1),
        "avg_cash_pct": round(float(aux["cash_w"].mean()) * 100, 1),
        "closed_trades": len(tdf),
        "open_positions": aux["open_positions"],
        "win_rate": round(len(wins) / len(tdf) * 100, 1) if len(tdf) else None,
        "avg_win": round(wins.ret.mean(), 2) if len(wins) else 0,
        "avg_loss": round(abs(losses.ret.mean()), 2) if len(losses) else 0,
        "avg_days": round(tdf.days.mean(), 0) if len(tdf) else None,
        "exit_reasons": tdf.reason.value_counts().to_dict() if len(tdf) else {},
        "yearly_pct": yearly,
        "monthly_pct": {str(k.date()): float(v) for k, v in monthly.items()},
        "months_under_top": n_short,
        "n_rebalances": len(screens),
        "bias": {
            "universe_sources": uni_srcs,
            "selected_unpriced": aux["n_unpriced"],
            "unfilled": aux["n_unfilled"],
            "forced_liquidations": aux["n_forced"],
            "fdr_missing_tickers": missing,
        },
    }


# ── 프로브 (0단계: KRX 덤프 커버리지 확인) ───────────────
def probe():
    import FinanceDataReader as fdr
    print("=== KRX 덤프 커버리지 ===", flush=True)
    dumps = load_krx_dumps()
    keys = sorted(dumps)
    first_uni, last_uni = set(), set()
    for k in keys:
        v = dumps[k]
        fund, cap = v.get("fund", []), v.get("cap", [])
        valid = sum(1 for r in fund if _knum(r[1]))
        n_idx = len(v.get("k200", [])) + len(v.get("q150", []))
        ok = "OK" if (valid >= 100 and n_idx >= 300 and len(cap) >= 1000) else "!!"
        print(f"  {k}: fund {len(fund)} (유효EPS {valid}) cap {len(cap)} 지수 {n_idx} [{ok}]", flush=True)
        uni = {str(t).zfill(6) for t in v.get("k200", []) + v.get("q150", [])}
        if k == keys[0]:
            first_uni = uni
        if k == keys[-1]:
            last_uni = uni
    gone = sorted(first_uni - last_uni)
    print(f"  {keys[0]} 구성 중 {keys[-1]} 이탈: {len(gone)}종목", flush=True)

    print("=== FDR 이탈 종목 시세 프로브 (상폐 여부) ===", flush=True)
    for t in gone[:3]:
        try:
            df = fdr.DataReader(t, "2021-07-01", "2026-07-01")
            print(f"  FDR {t}: {len(df)}행 ({df.index[0].date()} ~ {df.index[-1].date()})", flush=True)
        except Exception as e:
            print(f"  FDR {t}: ERR {e}", flush=True)
    print(f"=== DART 키: {'있음' if fetch_value.DART_KEY else '없음'} ===", flush=True)


# ── 실행 ─────────────────────────────────────────────────
def run_one(days, rebals, p, fstore, start, end, bench,
            slip_mult=1.0, full_rebalance=False, label="base"):
    if p["sort"] in ("momentum", "qvm"):
        # 사전 단계: 밸류 관문 통과 전 종목 가격 확보 (모멘텀 룩백만큼 과거로 연장)
        prelim_codes = collect_prelim_codes(rebals, p)
        mom_start = (pd.Timestamp(start) - pd.Timedelta(days=int((p["mom_win"] + p["mom_skip"]) * 1.6) + 30)
                     ).strftime("%Y-%m-%d")
        logger.info("[%s] momentum 가격 로드: %d종목 (%s ~ %s)", label, len(prelim_codes), mom_start, end)
        opens, closes, missing = load_prices(prelim_codes, mom_start, end)
        screens = run_screens(rebals, p, fstore, mom_closes=closes)
    else:
        screens = run_screens(rebals, p, fstore)
        all_codes = {r["code"] for s in screens for r in s["selected"]}
        opens, closes, missing = load_prices(all_codes, start, end)
    for s in screens:
        s["top"] = p["top"]
    nav, trades, aux = simulate(days, screens, opens, closes, p, slip_mult, full_rebalance)
    m = metrics(nav, trades, bench, screens, aux, missing)
    logger.info("[%s] CAGR %.2f%% (벤치 %.2f%%) MDD %.1f%% 샤프 %.2f 거래 %d",
                label, m["cagr_pct"], m["bench_cagr_pct"], m["mdd_pct"], m["sharpe"], m["closed_trades"])
    return m, screens, trades, nav


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--top", type=int, default=TOP_OUT)
    ap.add_argument("--per-max", type=float, default=PER_MAX)
    ap.add_argument("--pbr-max", type=float, default=PBR_MAX)
    ap.add_argument("--margin-min", type=float, default=MARGIN_MIN)
    ap.add_argument("--fscore-min", type=int, default=FSCORE_MIN)
    ap.add_argument("--no-fscore", action="store_true")
    ap.add_argument("--sort", choices=["fscore_margin", "margin", "momentum", "qvm"], default="fscore_margin")
    ap.add_argument("--mom-win", type=int, default=MOM_WIN, help="모멘텀 룩백 거래일 (기본=라이브 12-1)")
    ap.add_argument("--mom-skip", type=int, default=MOM_SKIP, help="최근 N거래일 제외 (기본=라이브 12-1)")
    ap.add_argument("--full-rebalance", action="store_true")
    ap.add_argument("--slip-mult", type=float, default=1.0)
    ap.add_argument("--report-month", type=int, default=5)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--limit-months", type=int, default=0, help="테스트용: 앞 N개월만")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.probe:
        probe()
        return

    end = args.end or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    days, rebals = build_calendar(args.start, end)
    if args.limit_months:
        rebals = rebals[:args.limit_months]
    logger.info("거래일 %d, 리밸런싱 %d회 (%s ~ %s)", len(days), len(rebals), args.start, end)

    p = _base_params()
    p.update({"top": args.top, "per_max": args.per_max, "pbr_max": args.pbr_max,
              "margin_min": args.margin_min, "fscore_min": args.fscore_min,
              "report_month": args.report_month, "use_fscore": not args.no_fscore,
              "sort": args.sort, "mom_win": args.mom_win, "mom_skip": args.mom_skip})
    if p["use_fscore"] and not fetch_value.DART_KEY:
        logger.warning("DART_API_KEY 없음 — F-Score 없이 진행 (--no-fscore와 동일)")
        p["use_fscore"] = False

    fstore = FScoreStore() if p["use_fscore"] else None
    bench = load_bench(args.start, end)

    m, screens, trades, nav = run_one(days, rebals, p, fstore, args.start, end, bench,
                                      args.slip_mult, args.full_rebalance)
    result = {
        "params": p, "slip_mult": args.slip_mult, "full_rebalance": args.full_rebalance,
        "metrics": m,
        "screens": [{"sig": str(s["sig"].date()), "n_prelim": s["n_prelim"],
                     "uni_src": s["uni_src"],
                     "selected": [{k: r.get(k) for k in ("code", "name", "margin", "per", "pbr", "f_score", "mom", "qvm")}
                                  for r in s["selected"]]} for s in screens],
        "trades": trades,
        "nav": {str(k.date()): round(float(v), 5) for k, v in nav.items()},
    }
    out = CACHE_DIR / f"vbt_result{args.tag}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(m, ensure_ascii=False, indent=1, default=str), flush=True)
    logger.info("저장: %s", out)

    if args.grid:
        grid = {"base": _grid_row(m)}
        variants = [
            ("per12", {"per_max": 12.0}), ("per18", {"per_max": 18.0}),
            ("pbr1.0", {"pbr_max": 1.0}), ("pbr2.0", {"pbr_max": 2.0}),
            ("margin10", {"margin_min": 10.0}), ("margin20", {"margin_min": 20.0}),
            ("fscore_off", {"use_fscore": False}),
            ("fscore4", {"fscore_min": 4}), ("fscore6", {"fscore_min": 6}),
            ("top10", {"top": 10}), ("top30", {"top": 30}),
        ]
        for name, over in variants:
            vp = {**p, **over}
            vf = fstore if vp["use_fscore"] else None
            vm, *_ = run_one(days, rebals, vp, vf, args.start, end, bench, label=name)
            grid[name] = _grid_row(vm)
        vm, *_ = run_one(days, rebals, p, fstore, args.start, end, bench,
                         slip_mult=2.0, label="slip_x2")
        grid["slip_x2"] = _grid_row(vm)
        vm, *_ = run_one(days, rebals, p, fstore, args.start, end, bench,
                         full_rebalance=True, label="full_rebal")
        grid["full_rebal"] = _grid_row(vm)
        (CACHE_DIR / f"vbt_grid{args.tag}.json").write_text(
            json.dumps(grid, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("grid 저장: vbt_grid%s.json", args.tag)


def _grid_row(m: dict) -> dict:
    return {k: m.get(k) for k in ("cagr_pct", "total_return_pct", "mdd_pct", "sharpe",
                                  "excess_cagr_pct", "closed_trades", "win_rate",
                                  "turnover_annual_pct", "avg_holdings", "avg_cash_pct")}


if __name__ == "__main__":
    main()
