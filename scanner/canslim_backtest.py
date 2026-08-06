"""CANSLIM 임계치 완화 검증 백테스트 (value_backtest 엔진 재사용).

질문: "오닐 원전 임계치(ROE 17% 등)를 국내시장에 맞게 완화하는 것이 더 유효한가?"
라이브 첫 실행에서 42종목 중 ROE 17% 통과가 5종목(12%)뿐이라 7요건 충족이 0종목이었다.
완화하면 후보가 느는 건 자명하나 수익률이 나아지는지는 별개다 — 그래서 측정한다.

설계 핵심은 스윕이 아니라 **대조군**이다. 완화 축(orig* vs loose*)을 대조군
(bench, rs_only) 사이에 끼워 넣어야 결과를 해석할 수 있다. 특히 rs_only(요건 전부
무시, RS 상위 20)를 이기지 못하면 임계치 논쟁 자체가 무의미하다.

데이터·PIT:
  · 유니버스/종가/시총: ~/krx_pit_*.json (60개 신호일, 2021-07 ~ 2026-06). 전 종목.
    S1(상장주식수) = 시가총액 / 종가 로 복원 — 추가 API 없음.
  · N·L·S2: FDR 일봉 (신호일 이전 구간만 슬라이싱)
  · A(연간 NI YoY·ROE): DART, fiscal_year_for(신호일) — 라이브 _financials()는 "작년"
    하드코딩이라 백테스트에서 호출 금지
  · C(분기 NI YoY): DART, quarter_fiscal_for(신호일) — 라이브 quarter_ni_yoy(today=)는
    연도 해상도라 최대 ~11개월 look-ahead가 뚫린다. 여기서는 절대 쓰지 않는다.

한계(리포트에 반드시 명시): 생존편향(_corp_map이 현 상장사만 매핑 → 상폐 종목 재무
결측 → 미충족 처리, 편향은 상방), 거래대금 미고려, RS는 라이브 정의 재현이나 스냅샷
소스가 달라 미세 오차 가능.

실행:
  python scanner/canslim_backtest.py --probe            # DART 없이 유니버스·RS·N·S2 점검
  python scanner/canslim_backtest.py --warm --limit-months 12   # DART 캐시 워밍업(분할)
  python scanner/canslim_backtest.py --controls         # 대조군 표
  python scanner/canslim_backtest.py --grid             # OFAT + 강건성 (캐시 히트라 빠름)
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import backtest as _bt
import value_backtest as vb
import fetch_value
import canslim_screener as cs
from value_backtest import (
    build_calendar, load_krx_dumps, _dump_key_for, _knum,
    load_prices, load_bench, simulate, metrics, _grid_row, fiscal_year_for,
)

logger = logging.getLogger("canslim_backtest")
ROOT = Path(__file__).parent.parent

# 캐시는 저장소 tmp/ 에 둔다(gitignore됨). backtest.CACHE_DIR 기본값은 $CLAUDE_JOB_DIR/tmp 라
# 잡이 지워지면 같이 날아가는데, DART 워밍업만 2~3시간짜리라 재수집 비용이 너무 크다.
CBT_CACHE = ROOT / "tmp"
CBT_CACHE.mkdir(parents=True, exist_ok=True)
_bt.CACHE_DIR = CBT_CACHE
vb.CACHE_DIR = CBT_CACHE

NI_CACHE_PATH = CBT_CACHE / "cbt_ni_v1.json"
DART_SLEEP = 0.15

# 가격은 항상 전 구간을 한 번만 받고 기간분할은 days 필터로 자른다 —
# load_prices 캐시 키가 start/end 문자열이라 구간을 바꾸면 통째로 재다운로드된다.
PX_START = "2020-05-01"    # 첫 신호일(2021-07-30) 기준 252거래일 워밍업 확보
PX_END = "2026-06-30"

# 완화 세트 — 국내 분포에 맞춘 가정치. 이 값이 옳은지를 검증하는 것이 목적이다.
LOOSE_TH = {"C": 10.0, "A_growth": 10.0, "A_roe": 10.0,
            "N": 0.80, "S1": 100_000_000, "L": 70}

_Q_END = {"11013": (3, 31), "11012": (6, 30), "11014": (9, 30)}   # 사업보고서는 연간이라 C에서 제외


def _base_params() -> dict:
    return {
        "min_cap": 100_000_000_000,   # 라이브 MIN_MARKET_CAP과 동일 (시총 1천억)
        "min_price": 1_000,
        "top": 20,
        "min_score": 5,
        "th": dict(cs.THRESHOLDS),    # 원전 임계치 (라이브와 동일)
        "report_month": 5,            # 연간 재무 확정 여유 (fiscal_year_for)
        "quarter_lag_days": 45,       # 분기보고서 법정 제출기한
        # DART 호출 전 사전컷 — 가장 느슨한 변형(L=70, N=0.80)보다 느슨해야 편향이 없다.
        "pre_rs": 50,
        "pre_prox": 0.70,
    }


def quarter_fiscal_for(sig_date: pd.Timestamp, lag_days: int = 45):
    """신호일까지 법정 제출기한(분기말+45일)이 지난 가장 최근 분기 → (연도, reprt_code).

    라이브의 '최신 제출본 탐색'은 연도 해상도라 백테스트에선 미래를 본다. 여기서는
    달력만으로 확정한다 — 실제 제출이 늦은 종목은 조회가 비어 결측(미충족) 처리된다.
    """
    cands = []
    for y in (sig_date.year, sig_date.year - 1, sig_date.year - 2):
        for reprt, (m, d) in _Q_END.items():
            qend = pd.Timestamp(year=y, month=m, day=d)
            if qend + pd.Timedelta(days=lag_days) <= sig_date:
                cands.append((qend, y, reprt))
    if not cands:
        return None
    _, y, reprt = max(cands)
    return (y, reprt)


def market_pool(sig_date: pd.Timestamp) -> list[dict]:
    """RS 백분위의 모수 — 시총 필터 이전의 전 시장(코스피·코스닥).

    라이브 build()는 pykrx 전종목 스냅샷으로 _pct_rank_map을 돌린다. 여기서 시총 1천억
    유니버스만 넣으면 같은 종목도 백분위가 달라져 L(RS≥80)이 다른 척도가 된다.
    """
    v = load_krx_dumps()[_dump_key_for(sig_date)]
    out = []
    for r in v["cap"]:
        code = str(r[0]).zfill(6)
        mkt = str(r[2] or "")
        if not _knum(r[3]) or mkt.startswith("KONEX"):
            continue
        out.append({"code": code, "market": "KOSDAQ" if "KOSDAQ" in mkt else "KOSPI"})
    return out


def pit_universe(sig_date: pd.Timestamp, p: dict) -> list[dict]:
    """신호일 시점 KRX 덤프 → 라이브와 같은 시총 1천억 유니버스. shares = 시총/종가."""
    v = load_krx_dumps()[_dump_key_for(sig_date)]
    out = []
    for r in v["cap"]:
        code = str(r[0]).zfill(6)
        name = str(r[1])
        mkt = str(r[2] or "")
        close, cap = _knum(r[3]), _knum(r[4])
        if not close or not cap:
            continue
        if not code.endswith("0"):        # 우선주·신형 파생 배제 (라이브와 동일)
            continue
        if "스팩" in name or mkt.startswith("KONEX"):
            continue
        if cap < p["min_cap"] or close < p["min_price"]:
            continue
        out.append({"code": code, "name": name,
                    "market": "KOSDAQ" if "KOSDAQ" in mkt else "KOSPI",
                    "close": close, "cap": cap, "shares": cap / close})
    return out


def make_rs(closes: pd.DataFrame, sig: pd.Timestamp, universe: list[dict]) -> dict[str, int]:
    """깡토 RS 재현 — 0.5*3M + 0.3*6M + 0.2*12M 의 시장별 백분위(1~99).

    라이브 pullback_screener._pct_rank_map 과 같은 식. _make_mom(12-1)은 정의가 달라 쓰지 않는다.
    """
    upto = closes[closes.index <= sig]
    if len(upto) < 253:
        return {}
    by_mkt: dict[str, dict[str, float]] = {}
    for u in universe:
        c = u["code"]
        if c not in upto.columns:
            continue
        s = upto[c].dropna()
        if len(s) < 253:
            continue
        cur = float(s.iloc[-1])
        p60, p120, p252 = float(s.iloc[-61]), float(s.iloc[-121]), float(s.iloc[-253])
        if not (p60 > 0 and p120 > 0 and p252 > 0 and cur > 0):
            continue
        by_mkt.setdefault(u["market"], {})[c] = (
            (cur / p60 - 1) * 0.5 + (cur / p120 - 1) * 0.3 + (cur / p252 - 1) * 0.2)
    rs: dict[str, int] = {}
    for vals in by_mkt.values():
        items = sorted(vals.items(), key=lambda x: x[1])
        n = len(items)
        for i, (c, _) in enumerate(items):
            rs[c] = round(i / (n - 1) * 98 + 1) if n > 1 else 50
    return rs


def make_prox(closes: pd.DataFrame, sig: pd.Timestamp) -> dict[str, float]:
    """N — 종가 / 직전 252거래일 종가 고점 (라이브 _detail도 종가 기준 52주 고가를 쓴다)."""
    upto = closes[closes.index <= sig]
    out = {}
    for c in upto.columns:
        s = upto[c].dropna()
        if len(s) < 120:
            continue
        w52 = float(s.iloc[-252:].max())
        cur = float(s.iloc[-1])
        if w52 > 0:
            out[c] = cur / w52
    return out


def make_vol2x(volumes: pd.DataFrame, closes: pd.DataFrame, sig: pd.Timestamp) -> dict[str, int]:
    """S2 — 최근 20거래일 중 '거래량 > 직전 20일 평균의 2배 & 종가 상승' 일이 있었는가.

    라이브 _detail 과 동일한 20일 창 플래그다(그날의 트리거가 아니라서 월간 리밸런싱과 맞는다).
    """
    v_upto = volumes[volumes.index <= sig]
    c_upto = closes[closes.index <= sig]
    out = {}
    for c in v_upto.columns:
        v = v_upto[c].dropna()
        s = c_upto[c].dropna() if c in c_upto.columns else None
        if s is None or len(v) < 41 or len(s) < 21:
            continue
        v20 = v.rolling(20).mean().shift(1)          # 당일 제외한 직전 20일 평균
        up = s.diff() > 0
        hit = (v > 2 * v20) & up.reindex(v.index).fillna(False)
        out[c] = int(bool(hit.iloc[-20:].any()))
    return out


class NIStore:
    """(종목, 연도, 보고서) → 순이익 / (종목, 사업연도) → 연간지표 캐시.

    결측도 캐시한다 — 미제출·비상장 종목 재조회가 최대 낭비다(quality_backtest 선례).
    """

    def __init__(self):
        try:
            self.cache = json.loads(NI_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}
        self.corp_map = fetch_value._corp_map() if fetch_value.DART_KEY else {}
        self.calls = 0
        self._dirty = 0
        self.no_corp = set()

    def save(self):
        if self._dirty:
            NI_CACHE_PATH.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            self._dirty = 0

    def _ni_q(self, code: str, year: int, reprt: str):
        k = f"q:{code}:{year}:{reprt}"
        if k in self.cache:
            return self.cache[k]
        corp = self.corp_map.get(code)
        if not corp:
            self.no_corp.add(code)
        v = cs._acnt_all_ni(corp, year, reprt) if corp else None
        if corp:
            self.calls += 1
            time.sleep(DART_SLEEP)
        self.cache[k] = v
        self._dirty += 1
        return v

    def q_yoy(self, code: str, year: int, reprt: str):
        """C — 같은 분기 전년 대비. 전년 적자면 YoY 무의미(None) — 라이브와 동일 규칙."""
        cur = self._ni_q(code, year, reprt)
        if cur is None:
            return None
        prev = self._ni_q(code, year - 1, reprt)
        if prev is None or prev <= 0:
            return None
        return round((cur - prev) / prev * 100, 1)

    def annual(self, code: str, fy: int):
        """A — 사업연도 fy 기준 연간 순이익 YoY·ROE. _acnt는 1회에 3개년을 준다."""
        k = f"a:{code}:{fy}"
        if k in self.cache:
            return self.cache[k]
        corp = self.corp_map.get(code)
        out = None
        if corp:
            acc = fetch_value._acnt(corp, fy)
            self.calls += 1
            time.sleep(DART_SLEEP)
            if acc:
                ni = fetch_value._pick(acc, ["당기순이익"], fy)
                ni_p = fetch_value._pick(acc, ["당기순이익"], fy - 1)
                eq = fetch_value._pick(acc, ["자본총계"], fy)
                out = {
                    "ni_growth": (round((ni - ni_p) / abs(ni_p) * 100, 1)
                                  if (ni is not None and ni_p and ni_p > 0) else None),
                    "roe": round(ni / eq * 100, 1) if (ni is not None and eq) else None,
                }
        else:
            self.no_corp.add(code)
        self.cache[k] = out
        self._dirty += 1
        return out


def screen_at(sig: pd.Timestamp, p: dict, store: NIStore | None,
              closes: pd.DataFrame, volumes: pd.DataFrame):
    """신호일 크로스섹션 스크린 → (selected, 사전컷 통과수, 전체 rows)."""
    uni = pit_universe(sig, p)
    rs = make_rs(closes, sig, market_pool(sig))   # 백분위 모수는 전 시장 (라이브와 동일)
    prox = make_prox(closes, sig)
    v2x = make_vol2x(volumes, closes, sig)

    pre = [u for u in uni
           if rs.get(u["code"], 0) >= p["pre_rs"] and prox.get(u["code"], 0) >= p["pre_prox"]]
    pre.sort(key=lambda u: -rs.get(u["code"], 0))

    # min_score<=0 은 rs_only 대조군 — 요건을 아예 안 보므로 DART를 부르지 않는다.
    use_dart = store is not None and p["min_score"] > 0
    fy = fiscal_year_for(sig, p["report_month"])
    q = quarter_fiscal_for(sig, p["quarter_lag_days"])

    rows = []
    for u in pre:
        rec = {"shares": u["shares"], "proximity_52w": prox.get(u["code"]),
               "rs_kkangto": rs.get(u["code"]), "vol_2x_bo": v2x.get(u["code"], 0)}
        if use_dart:
            if q:
                rec["q_ni_yoy"] = store.q_yoy(u["code"], q[0], q[1])
            a = store.annual(u["code"], fy)
            if a:
                rec.update(a)
        sc, bits = cs.score(rec, p["th"])
        rows.append({**u, **rec, "score": sc, "bits": bits})

    sel = [r for r in rows if r["score"] >= p["min_score"]]
    sel.sort(key=lambda r: -(r.get("rs_kkangto") or 0))
    return sel[:p["top"]], len(pre), rows


def run_screens(rebals, p, store, closes, volumes, log_bits=False):
    out, bit_log = [], []
    for sig, ex in rebals:
        assert sig < ex, "신호일이 체결일보다 늦음"
        assert closes[closes.index <= sig].index.max() <= sig, "look-ahead: 가격이 신호일 이후"
        sel, n_pre, rows = screen_at(sig, p, store, closes, volumes)
        out.append({"sig": sig, "ex": ex, "selected": sel,
                    "n_prelim": n_pre, "uni_src": "pit", "top": p["top"]})
        if store:
            store.save()
        if log_bits and rows:
            rate = {k: round(np.mean([r["bits"][k] for r in rows]) * 100, 1)
                    for k in rows[0]["bits"]}
            bit_log.append({"sig": str(sig.date()), "n": len(rows), "pass_rate_pct": rate})
            logger.info("%s 사전컷 %d → 선정 %d | 요건 통과율 %s",
                        sig.date(), n_pre, len(sel), rate)
        else:
            logger.info("%s 사전컷 %d → 선정 %d", sig.date(), n_pre, len(sel))
    return out, bit_log


def run_one(days, rebals, p, store, closes, opens, volumes, bench,
            slip_mult=1.0, full_rebalance=False, label="base", log_bits=False):
    screens, bit_log = run_screens(rebals, p, store, closes, volumes, log_bits=log_bits)
    nav, trades, aux = simulate(days, screens, opens, closes, p, slip_mult, full_rebalance)
    m = metrics(nav, trades, bench, screens, aux, [])
    m["label"] = label
    logger.info("[%s] CAGR %.2f%% (초과 %.2f%%p) MDD %.1f%% 승률 %s%% 거래 %d 현금 %.1f%%",
                label, m["cagr_pct"], m["excess_cagr_pct"], m["mdd_pct"],
                m["win_rate"], m["closed_trades"], m["avg_cash_pct"])
    return m, screens, nav, trades, bit_log


def _load_market_data(start: str, end: str, p: dict):
    """전 신호일 유니버스 합집합의 가격·거래량을 한 번만 받는다."""
    days, rebals = build_calendar(start, end)
    codes = set()
    for sig, _ in rebals:
        codes |= {u["code"] for u in pit_universe(sig, p)}
    logger.info("유니버스 합집합 %d종목 — 가격 다운로드", len(codes))
    opens, closes, missing, volumes = load_prices(codes, PX_START, PX_END, with_volume=True)
    logger.info("시세 확보 %d종목 (결측 %d — 대부분 상폐, 생존편향 근거)",
                closes.shape[1], len(missing))
    return days, rebals, opens, closes, volumes, missing


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--tag", default="")
    ap.add_argument("--probe", action="store_true", help="DART 없이 유니버스·RS·N·S2 점검")
    ap.add_argument("--warm", action="store_true", help="DART 캐시 워밍업만")
    ap.add_argument("--limit-months", type=int, default=0, help="앞에서부터 N개 신호일만")
    ap.add_argument("--controls", action="store_true", help="대조군 표")
    ap.add_argument("--grid", action="store_true", help="OFAT + 강건성")
    args = ap.parse_args()

    p = _base_params()
    days, rebals, opens, closes, volumes, missing = _load_market_data(args.start, args.end, p)
    if args.limit_months:
        rebals = rebals[:args.limit_months]

    if args.probe:
        sig = rebals[-1][0]
        uni = pit_universe(sig, p)
        pool = market_pool(sig)
        rs = make_rs(closes, sig, pool)
        prox = make_prox(closes, sig)
        v2x = make_vol2x(volumes, closes, sig)
        top = sorted(uni, key=lambda u: -rs.get(u["code"], 0))[:20]
        out = {"sig": str(sig.date()), "universe": len(uni), "rs_pool": len(pool),
               "rs_covered": len(rs), "prox_covered": len(prox),
               "vol2x_true": sum(v2x.values()), "missing_prices": len(missing),
               "top20_rs": [{"code": u["code"], "name": u["name"],
                             "rs": rs.get(u["code"]), "prox": round(prox.get(u["code"], 0), 3),
                             "shares_만주": round(u["shares"] / 1e4)} for u in top]}
        (CBT_CACHE / f"cbt_probe{args.tag}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    store = NIStore()
    bench = load_bench(PX_START, PX_END)

    if args.warm:
        _, bit_log = run_screens(rebals, p, store, closes, volumes, log_bits=True)
        store.save()
        (CBT_CACHE / f"cbt_bits{args.tag}.json").write_text(
            json.dumps(bit_log, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("워밍업 완료 — DART 호출 %d, corp_code 결측 %d종목(생존편향)",
                    store.calls, len(store.no_corp))
        return

    results = {}
    if args.controls or not args.grid:
        controls = [
            ("rs_only", {"min_score": 0, "pre_prox": 0.0}),
            ("orig7", {"min_score": 7}), ("orig5", {}), ("orig4", {"min_score": 4}),
            ("loose7", {"min_score": 7, "th": {**cs.THRESHOLDS, **LOOSE_TH}}),
            ("loose5", {"th": {**cs.THRESHOLDS, **LOOSE_TH}}),
            ("loose4", {"min_score": 4, "th": {**cs.THRESHOLDS, **LOOSE_TH}}),
        ]
        for name, over in controls:
            m, screens, nav, trades, bits = run_one(
                days, rebals, {**p, **over}, store, closes, opens, volumes, bench,
                label=name, log_bits=(name == "orig5"))
            results[name] = _grid_row(m)
            if name == "orig5":
                (CBT_CACHE / f"cbt_result{args.tag}.json").write_text(json.dumps(
                    {"params": {k: v for k, v in p.items()}, "metrics": m,
                     "bit_log": bits, "missing_prices": len(missing),
                     "no_corp": len(store.no_corp)},
                    ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        (CBT_CACHE / f"cbt_controls{args.tag}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.grid:
        TH = cs.THRESHOLDS
        variants = [
            ("roe12", {"th": {**TH, "A_roe": 12.0}}), ("roe8", {"th": {**TH, "A_roe": 8.0}}),
            ("roe_off", {"th": {**TH, "A_roe": -1e9}}),
            ("c10", {"th": {**TH, "C": 10.0}}), ("c0", {"th": {**TH, "C": 0.0}}),
            ("a10", {"th": {**TH, "A_growth": 10.0}}), ("a_off", {"th": {**TH, "A_growth": -1e9}}),
            ("n80", {"th": {**TH, "N": 0.80}}), ("n90", {"th": {**TH, "N": 0.90}}),
            ("l70", {"th": {**TH, "L": 70}}), ("l90", {"th": {**TH, "L": 90}}),
            ("s1_1e8", {"th": {**TH, "S1": 100_000_000}}), ("s1_off", {"th": {**TH, "S1": 1e18}}),
            ("cut3", {"min_score": 3}), ("cut6", {"min_score": 6}),
            ("top10", {"top": 10}), ("top30", {"top": 30}),
            ("cap3000", {"min_cap": 300_000_000_000}),
        ]
        for name, over in variants:
            m, *_ = run_one(days, rebals, {**p, **over}, store, closes, opens, volumes,
                            bench, label=name)
            results[name] = _grid_row(m)
        for name, kw in (("slip_x2", {"slip_mult": 2.0}), ("full_rebal", {"full_rebalance": True})):
            m, *_ = run_one(days, rebals, p, store, closes, opens, volumes, bench,
                            label=name, **kw)
            results[name] = _grid_row(m)
        (CBT_CACHE / f"cbt_grid{args.tag}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    store.save()
    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
