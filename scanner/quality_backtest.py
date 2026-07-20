"""
퀄리티 성장주 발굴 전략 — 월간 리밸런싱 5개년 백테스트 (value_backtest 엔진 재사용).

가설: 포인트인타임 재무로 계산한 '퀄리티 종합 Z' 상위 종목은 코스피200+코스닥150 유니버스에서
      벤치마크(KODEX200) 대비 초과수익을 낸다. (밸류·성장은 관문, 정렬은 순수 퀄리티)

전략 (제로 재량):
  유니버스: 각 신호일 시점 코스피200+코스닥150 (KRX 포인트인타임 덤프)
  관문: 시총>=3,000억, EPS·BPS 양수(적자·자본잠식 배제), PER<=40(극단 고평가만 컷),
        DART 연결재무 산출 가능(매출총이익·자산총계 존재 — 금융/지주 자연 배제)
  퀄리티 Z (신호일 크로스섹션 표준화, winsorize 5/95, 부호 적용 후 가용 지표 평균):
        +ROE(EPS/BPS) +GPA(매출총이익/자산총계) +영업이익률 −부채비율(부채/자본) −accruals((순이익−영업CF)/자산)
  선정: 종합 Z 상위 20, 동일비중
  리밸런싱/체결/비용: value_backtest와 동일 (전월말 신호 → 익월초 시가, ±0.5% 슬리피지 등)

포인트인타임:
  - 유니버스·시총·EPS/BPS/PER: KRX 덤프 (fetch_snapshot/fetch_universe_pit 재사용)
  - 재무: DART fnlttSinglAcntAll, 사업연도 = fiscal_year_for(신호일) — look-ahead 없음
  - 재무 캐시: CACHE_DIR/qbt_fin.json (재실행 가속)

실행:
  python scanner/quality_backtest.py --probe        # 재무 커버리지 프로브(앞 3개월 표본)
  python scanner/quality_backtest.py                # 기본 (2021-07 ~ 어제)
  python scanner/quality_backtest.py --grid         # OFAT robustness 배치
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).parent))

from backtest import CACHE_DIR
import fetch_value
from value_backtest import (
    build_calendar, fetch_universe_pit, fetch_snapshot, fiscal_year_for,
    load_prices, load_bench, simulate, metrics, _grid_row,
)

logger = logging.getLogger("quality_backtest")
ROOT = Path(__file__).parent.parent

FIN_CACHE_PATH = CACHE_DIR / "qbt_fin.json"
DART_SLEEP = 0.15
_DART = "https://opendart.fss.or.kr/api"

# ── 기본 파라미터 ─────────────────────────────────────────
MIN_CAP = 300_000_000_000   # 시총 3,000억 (마이크로캡 배제)
PER_MAX = 40.0              # 극단 고평가만 컷 (정렬엔 미사용)
TOP_OUT = 20
# 퀄리티 Z 구성요소: (키, 부호). 부호 +는 높을수록 좋음, −는 낮을수록 좋음.
Z_COMPONENTS = [("roe", +1), ("gpa", +1), ("opm", +1), ("debt", -1), ("accruals", -1)]


def _base_params() -> dict:
    return {"min_cap": MIN_CAP, "per_max": PER_MAX, "top": TOP_OUT,
            "report_month": 5, "drop_component": None}


# ── DART 포인트인타임 재무 (퀄리티 지표 원시항목) ─────────
def _dart_rows(corp: str, year: int):
    """fnlttSinglAcntAll(연결 우선, 없으면 별도) 사업연도 리스트. 실패 시 None."""
    for fs in ("CFS", "OFS"):
        url = (f"{_DART}/fnlttSinglAcntAll.json?crtfc_key={fetch_value.DART_KEY}&corp_code={corp}"
               f"&bsns_year={year}&reprt_code=11011&fs_div={fs}")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            logger.warning("재무 조회 실패 %s: %s", corp, e)
            return None
        if d.get("status") == "000" and d.get("list"):
            return d["list"]
    return None


def _quality_fin(corp: str, year: int) -> dict | None:
    """신호일 사업연도 재무 → 퀄리티 원시지표. 반환 dict 또는 None(핵심결측/실패).
    gpa=매출총이익/자산총계, opm=영업이익/매출, debt=부채총계/자본총계, accruals=(순이익−영업CF)/자산총계."""
    rows = _dart_rows(corp, year)
    if not rows:
        return None

    def get(nm, sj):
        nmz = nm.replace(" ", "")
        for x in rows:
            if x.get("sj_div") != sj:
                continue
            a = (x.get("account_nm", "") or "").replace(" ", "")
            if a == nmz or a == nmz + "(손실)" or a.startswith(nmz):
                return fetch_value._num(x.get("thstrm_amount"))
        return None

    assets = get("자산총계", "BS")
    liab = get("부채총계", "BS")
    equity = get("자본총계", "BS")
    rev = get("매출액", "CIS") or get("매출액", "IS")
    gp = get("매출총이익", "CIS") or get("매출총이익", "IS")
    op = get("영업이익", "CIS") or get("영업이익", "IS")
    ni = get("당기순이익", "CIS") or get("당기순이익", "IS")
    cfo = get("영업활동 현금흐름", "CF") or get("영업활동으로 인한 현금흐름", "CF")

    def ratio(n, d):
        return (n / d) if (n is not None and d not in (None, 0)) else None

    gpa = ratio(gp, assets)
    opm = ratio(op, rev)
    debt = ratio(liab, equity)
    accruals = ratio((ni - cfo) if (ni is not None and cfo is not None) else None, assets)
    # 핵심 퀄리티(GPA·영업이익률) 둘 다 없으면 무효 (금융·지주 등 매출/매출총이익 미보고 자연 배제)
    if gpa is None and opm is None:
        return None
    return {"gpa": gpa, "opm": opm, "debt": debt, "accruals": accruals,
            "assets": assets, "rev": rev}


class QualityStore:
    """(code, fiscal_year) → 퀄리티 원시지표. DART 호출 캐시."""
    def __init__(self):
        try:
            self.cache = json.loads(FIN_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}
        self.corp_map = fetch_value._corp_map() if fetch_value.DART_KEY else {}
        self.calls = 0

    def get(self, code: str, year: int):
        k = f"{code}:{year}"
        if k in self.cache:
            return self.cache[k]
        corp = self.corp_map.get(code)
        fin = None
        if corp:
            fin = _quality_fin(corp, year)
            self.calls += 1
            time.sleep(DART_SLEEP)
        self.cache[k] = fin
        return fin

    def save(self):
        FIN_CACHE_PATH.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")


# ── 퀄리티 Z 스코어링 ────────────────────────────────────
def _winsor_z(values: list[float | None], sign: int) -> list[float | None]:
    """winsorize 5/95 후 z-score(부호 적용). 결측(None)은 그대로 None 반환(평균에서 제외)."""
    xs = [v for v in values if v is not None]
    if len(xs) < 5:
        return [None] * len(values)
    lo, hi = np.percentile(xs, 5), np.percentile(xs, 95)
    clipped = [min(max(v, lo), hi) if v is not None else None for v in values]
    present = [v for v in clipped if v is not None]
    mu, sd = float(np.mean(present)), float(np.std(present))
    if sd == 0:
        return [0.0 if v is not None else None for v in clipped]
    return [sign * (v - mu) / sd if v is not None else None for v in clipped]


def _score_cross_section(recs: list[dict], drop_component: str | None):
    """recs 각 원소에 quality_z 부여. 가용 z 평균(모두 결측이면 제외 대상 표시 z=None)."""
    comps = [(k, s) for k, s in Z_COMPONENTS if k != drop_component]
    zmat = {}
    for key, sign in comps:
        zmat[key] = _winsor_z([r.get(key) for r in recs], sign)
    for i, r in enumerate(recs):
        zs = [zmat[key][i] for key, _ in comps if zmat[key][i] is not None]
        r["quality_z"] = round(float(np.mean(zs)), 4) if zs else None
        r["z_detail"] = {key: (round(zmat[key][i], 3) if zmat[key][i] is not None else None)
                         for key, _ in comps}


# ── 스크린 ───────────────────────────────────────────────
def screen_at(sig_date, universe, fund, cap, p, qstore: QualityStore):
    """신호일 스냅샷 → (선정 리스트, 관문 통과 수)."""
    fy = fiscal_year_for(sig_date, p["report_month"])
    recs = []
    for code, name in universe:
        f = fund.get(code)
        c = cap.get(code, {})
        if not f or not c.get("close"):
            continue
        if c.get("cap") is None or c["cap"] < p["min_cap"]:
            continue
        eps, bps, per = f["eps"], f["bps"], f["per"]
        if not eps or not bps or eps <= 0 or bps <= 0:
            continue
        price = c["close"]
        per = per or round(price / eps, 1)
        if per > p["per_max"]:
            continue
        fin = qstore.get(code, fy)
        if not fin:
            continue
        recs.append({
            "code": code, "name": name, "per": round(per, 1),
            "roe": round(eps / bps * 100, 2),
            "gpa": fin["gpa"], "opm": fin["opm"], "debt": fin["debt"], "accruals": fin["accruals"],
        })
    n_gate = len(recs)
    _score_cross_section(recs, p.get("drop_component"))
    ranked = sorted((r for r in recs if r["quality_z"] is not None),
                    key=lambda r: r["quality_z"], reverse=True)
    # 표시용 반올림 (Z 계산 후)
    for r in ranked:
        r["gpa"] = round(r["gpa"] * 100, 2) if r["gpa"] is not None else None
        r["opm"] = round(r["opm"] * 100, 2) if r["opm"] is not None else None
        r["debt"] = round(r["debt"] * 100, 1) if r["debt"] is not None else None
        r["accruals"] = round(r["accruals"] * 100, 2) if r["accruals"] is not None else None
    return ranked[:p["top"]], n_gate


def run_screens(rebals, p, qstore: QualityStore):
    out = []
    for sig, ex in rebals:
        assert sig < ex, "신호일이 체결일보다 늦음"
        uni, src = fetch_universe_pit(sig)
        fund, cap, snap_d = fetch_snapshot(sig)
        assert snap_d <= sig.strftime("%Y%m%d"), "look-ahead: 스냅샷이 신호일 이후"
        sel, n_gate = screen_at(sig, uni, fund, cap, p, qstore)
        out.append({"sig": sig, "ex": ex, "selected": sel, "n_prelim": n_gate,
                    "uni_src": src, "top": p["top"]})
        qstore.save()
        logger.info("%s 스크린: 관문통과 %d → 선정 %d", sig.date(), n_gate, len(sel))
    return out


def run_one(days, rebals, p, qstore, start, end, bench, slip_mult=1.0, full_rebalance=False, label="base"):
    screens = run_screens(rebals, p, qstore)
    all_codes = {r["code"] for s in screens for r in s["selected"]}
    opens, closes, missing = load_prices(all_codes, start, end)
    nav, trades, aux = simulate(days, screens, opens, closes, p, slip_mult, full_rebalance)
    m = metrics(nav, trades, bench, screens, aux, missing)
    logger.info("[%s] CAGR %.2f%% (벤치 %.2f%%) MDD %.1f%% 샤프 %.2f 거래 %d",
                label, m["cagr_pct"], m["bench_cagr_pct"], m["mdd_pct"], m["sharpe"], m["closed_trades"])
    return m, screens, trades, nav


# ── 프로브 ───────────────────────────────────────────────
def probe(rebals, qstore: QualityStore):
    print(f"=== DART 키: {'있음' if fetch_value.DART_KEY else '없음'} ===", flush=True)
    if not fetch_value.DART_KEY:
        return
    for sig, ex in rebals[:3]:
        uni, _ = fetch_universe_pit(sig)
        fund, cap, _ = fetch_snapshot(sig)
        p = _base_params()
        sel, n_gate = screen_at(sig, uni, fund, cap, p, qstore)
        qstore.save()
        top5 = [(r["name"], r["quality_z"]) for r in sel[:5]]
        print(f"  {sig.date()}: 관문통과 {n_gate} 선정 {len(sel)} | 상위5 {top5}", flush=True)


# ── 실행 ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--top", type=int, default=TOP_OUT)
    ap.add_argument("--per-max", type=float, default=PER_MAX)
    ap.add_argument("--report-month", type=int, default=5)
    ap.add_argument("--slip-mult", type=float, default=1.0)
    ap.add_argument("--full-rebalance", action="store_true")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--limit-months", type=int, default=0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not fetch_value.DART_KEY:
        logger.error("DART_API_KEY 없음 — 퀄리티 백테스트 불가")
        return

    end = args.end or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    days, rebals = build_calendar(args.start, end)
    if args.limit_months:
        rebals = rebals[:args.limit_months]
    logger.info("거래일 %d, 리밸런싱 %d회 (%s ~ %s)", len(days), len(rebals), args.start, end)

    qstore = QualityStore()

    if args.probe:
        probe(rebals, qstore)
        return

    p = _base_params()
    p.update({"top": args.top, "per_max": args.per_max, "report_month": args.report_month})
    bench = load_bench(args.start, end)

    m, screens, trades, nav = run_one(days, rebals, p, qstore, args.start, end, bench,
                                      args.slip_mult, args.full_rebalance)
    result = {
        "params": p, "slip_mult": args.slip_mult, "full_rebalance": args.full_rebalance,
        "metrics": m,
        "screens": [{"sig": str(s["sig"].date()), "n_prelim": s["n_prelim"],
                     "selected": [{k: r.get(k) for k in
                                   ("code", "name", "quality_z", "roe", "gpa", "opm", "debt", "accruals", "per")}
                                  for r in s["selected"]]} for s in screens],
        "trades": trades,
        "nav": {str(k.date()): round(float(v), 5) for k, v in nav.items()},
    }
    out = CACHE_DIR / f"qbt_result{args.tag}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(m, ensure_ascii=False, indent=1, default=str), flush=True)
    logger.info("저장: %s", out)

    if args.grid:
        grid = {"base": _grid_row(m)}
        variants = [
            ("per30", {"per_max": 30.0}), ("per50", {"per_max": 50.0}), ("per_inf", {"per_max": 1e9}),
            ("top10", {"top": 10}), ("top15", {"top": 15}), ("top30", {"top": 30}),
            ("drop_roe", {"drop_component": "roe"}), ("drop_gpa", {"drop_component": "gpa"}),
            ("drop_opm", {"drop_component": "opm"}), ("drop_debt", {"drop_component": "debt"}),
            ("drop_accruals", {"drop_component": "accruals"}),
        ]
        for name, over in variants:
            vp = {**p, **over}
            vm, *_ = run_one(days, rebals, vp, qstore, args.start, end, bench, label=name)
            grid[name] = _grid_row(vm)
        vm, *_ = run_one(days, rebals, p, qstore, args.start, end, bench, slip_mult=2.0, label="slip_x2")
        grid["slip_x2"] = _grid_row(vm)
        vm, *_ = run_one(days, rebals, p, qstore, args.start, end, bench, full_rebalance=True, label="full_rebal")
        grid["full_rebal"] = _grid_row(vm)
        (CACHE_DIR / f"qbt_grid{args.tag}.json").write_text(
            json.dumps(grid, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("grid 저장: qbt_grid%s.json", args.tag)


if __name__ == "__main__":
    main()
