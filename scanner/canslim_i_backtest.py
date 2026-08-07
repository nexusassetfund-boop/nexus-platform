"""I(기관+외인 순매수)를 CANSLIM 점수에 넣어야 하는가 — 크로스섹션 검증.

질문: 라이브 canslim_screener는 I를 수집만 하고 7점 채점에 넣지 않는다(수급 흐름이라
L/N과 중복된다는 판단). 점수화가 실제로 나은가?

설계: 포트폴리오 시뮬 전에 **신호의 알파부터 잰다**. 스코어 정의를 건드린 8비트
포트폴리오 비교는 임계치·상위N·리밸런싱이 뒤섞여 원인 분리가 안 된다. 여기서는
CANSLIM 모멘텀 코어(N·L 통과) 안에서 I의 향후 1개월 수익률 스프레드만 본다.
  · I가 코어 안에서 스프레드가 없으면 → 점수화 근거 없음 (판정 종료)
  · 있으면 → canslim_backtest에 8비트 변형을 추가해 포트폴리오로 확인 (2단계)
Stage 0(급락 국면)·RRG Y축 때와 같은 "순위 0.5면 알파 아님" 판정 절차다.

데이터·PIT:
  · 유니버스·종가·시총·RS·N: canslim_backtest 그대로 재사용 (신호일 이전만 슬라이싱)
  · I: pykrx get_market_net_purchases_of_equities(신호일-60거래일 ~ 신호일) —
    라이브 _flow_map과 같은 소스·같은 창. **KRX 로그인 필요(KRX_ID/PW)라 CI에서만 된다.**
  · 향후 수익률: 체결일 시가 → 다음 체결일 시가 (라이브 매매와 같은 타이밍)

한계: 생존편향(상폐 종목 시세 결측 → 표본 제외, 편향 상방), 거래비용 미반영
(스프레드는 롱숏 차이라 비용에 1차적으로 중립).

실행: python scanner/canslim_i_backtest.py --spread     # CI, KRX_ID/PW 필요
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

import canslim_backtest as cbt
import canslim_screener as cs
import pullback_screener as pb

logger = logging.getLogger("canslim_i")
FLOW_CACHE = cbt.CBT_CACHE / "cbt_iflow_v1.json"
FLOW_TD = cs.I_FLOW_TD        # 60거래일 — 라이브와 동일


class FlowStore:
    """(신호일) → {종목: 기관+외인 순매수대금(원)} 디스크 캐시.

    호출 단위가 '시장 전체 × 투자자'라 신호일당 4회면 끝난다(60신호일 = 240회).
    실패한 신호일은 캐시하지 않는다 — KRX 간헐 차단이라 재실행하면 채워진다.
    """

    def __init__(self):
        try:
            self.cache = json.loads(FLOW_CACHE.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}
        self.calls = 0
        self._dirty = 0

    def save(self):
        if self._dirty:
            FLOW_CACHE.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            self._dirty = 0

    def get(self, days: list[pd.Timestamp], sig: pd.Timestamp) -> dict[str, float]:
        k = str(sig.date())
        if k in self.cache:
            return self.cache[k]
        i = days.index(sig)
        if i < FLOW_TD:
            return {}
        start = days[i - FLOW_TD].strftime("%Y%m%d")
        end = sig.strftime("%Y%m%d")
        out: dict[str, float] = {}
        ok = True
        for mkt in ("KOSPI", "KOSDAQ"):
            m = cs._flow_map(start, end, mkt)     # 라이브와 같은 함수 — 정의 불일치 방지
            if not m:
                ok = False
                logger.warning("%s 수급 결측 (%s~%s)", mkt, start, end)
            out.update(m)
            self.calls += 1
            time.sleep(pb.PYKRX_SLEEP)
        if not ok:
            return {}
        self.cache[k] = out
        self._dirty += 1
        return out


def _fwd_returns(opens: pd.DataFrame, ex: pd.Timestamp, ex_next: pd.Timestamp) -> dict[str, float]:
    """체결일 시가 → 다음 체결일 시가 수익률. 둘 중 하나라도 결측이면 제외."""
    if ex not in opens.index or ex_next not in opens.index:
        return {}
    a, b = opens.loc[ex], opens.loc[ex_next]
    out = {}
    for c in opens.columns:
        p0, p1 = a.get(c), b.get(c)
        if p0 and p1 and p0 > 0 and np.isfinite(p0) and np.isfinite(p1):
            out[c] = float(p1) / float(p0) - 1
    return out


def collect(days, rebals, opens, closes, p, store: FlowStore, core: bool = True):
    """신호일별 관측치 — 코어(N·L 통과) 종목의 I·향후수익률. core=False면 사전컷 전체."""
    obs = []
    th = p["th"]
    for (sig, ex), (_, ex_next) in zip(rebals[:-1], rebals[1:]):
        assert closes[closes.index <= sig].index.max() <= sig, "look-ahead: 가격이 신호일 이후"
        uni = cbt.pit_universe(sig, p)
        rs = cbt.make_rs(closes, sig, cbt.market_pool(sig))
        prox = cbt.make_prox(closes, sig)
        flows = store.get(days, sig)
        store.save()
        if not flows:
            continue
        fwd = _fwd_returns(opens, ex, ex_next)
        rows = []
        for u in uni:
            c = u["code"]
            if c not in fwd or c not in flows:
                continue
            r_, x_ = rs.get(c, 0), prox.get(c, 0)
            if core and not (r_ >= th["L"] and x_ >= th["N"]):
                continue
            if not core and not (r_ >= p["pre_rs"] and x_ >= p["pre_prox"]):
                continue
            rows.append({"code": c, "rs": r_, "prox": x_, "cap": u["cap"],
                         "flow": flows[c], "flow_cap": flows[c] / u["cap"], "fwd": fwd[c]})
        if len(rows) >= 10:      # 표본 10종목 미만 신호일은 스프레드가 잡음
            obs.append({"sig": sig, "ex": ex, "rows": rows})
        logger.info("%s 코어 %d종목 (I 결측 제외)", sig.date(), len(rows))
    return obs


def _t_stat(xs: list[float]) -> float:
    a = np.asarray(xs, dtype=float)
    a = a[np.isfinite(a)]
    if len(a) < 3 or a.std(ddof=1) == 0:
        return float("nan")
    return float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a))))


def analyze(obs) -> dict:
    """부호 스프레드 + 분위 단조성 + 순위상관. 월별 시계열의 t값으로 판정한다."""
    spread, pos_mean, neg_mean, q_means, ic = [], [], [], [], []
    n_pos, n_neg = 0, 0
    for o in obs:
        rows = o["rows"]
        pos = [r["fwd"] for r in rows if r["flow"] > 0]
        neg = [r["fwd"] for r in rows if r["flow"] <= 0]
        if len(pos) >= 3 and len(neg) >= 3:
            spread.append(np.mean(pos) - np.mean(neg))
            pos_mean.append(np.mean(pos))
            neg_mean.append(np.mean(neg))
            n_pos += len(pos)
            n_neg += len(neg)
        # 분위: 시총 정규화 순매수 5분위 평균 (규모 효과 제거)
        if len(rows) >= 15:
            s = sorted(rows, key=lambda r: r["flow_cap"])
            qs = np.array_split(np.array([r["fwd"] for r in s]), 5)
            q_means.append([float(np.mean(q)) for q in qs])
        # 스피어만 IC (flow_cap vs fwd)
        if len(rows) >= 10:
            fr = pd.Series([r["flow_cap"] for r in rows]).rank()
            fw = pd.Series([r["fwd"] for r in rows]).rank()
            c = fr.corr(fw)
            if pd.notna(c):
                ic.append(float(c))
    qm = np.array(q_means).mean(axis=0).tolist() if q_means else []
    return {
        "months": len(obs), "months_with_spread": len(spread),
        "n_pos_obs": n_pos, "n_neg_obs": n_neg,
        "mean_fwd_pos_pct": round(float(np.mean(pos_mean)) * 100, 3) if pos_mean else None,
        "mean_fwd_neg_pct": round(float(np.mean(neg_mean)) * 100, 3) if neg_mean else None,
        "spread_pct": round(float(np.mean(spread)) * 100, 3) if spread else None,
        "spread_t": round(_t_stat(spread), 2) if spread else None,
        "spread_hit_rate_pct": (round(float(np.mean([s > 0 for s in spread])) * 100, 1)
                                if spread else None),
        "quintile_fwd_pct": [round(v * 100, 3) for v in qm],
        "quintile_top_minus_bottom_pct": round((qm[-1] - qm[0]) * 100, 3) if qm else None,
        "ic_mean": round(float(np.mean(ic)), 4) if ic else None,
        "ic_t": round(_t_stat(ic), 2) if ic else None,
    }


def _rank(vals) -> np.ndarray:
    return pd.Series(vals).rank().to_numpy(dtype=float)


def _resid_ic(rows, key: str, ctrl: list[str]) -> float | None:
    """ctrl 순위로 설명되는 부분을 제거한 뒤의 key–fwd 순위상관.

    I가 RS·N의 재포장인지 보는 핵심 지표다. 회귀는 순위 공간에서 최소제곱 1~2변수.
    """
    if len(rows) < 15:
        return None
    y = _rank([r["fwd"] for r in rows])
    x = _rank([r[key] for r in rows])
    X = np.column_stack([np.ones(len(rows))] + [_rank([r[c] for r in rows]) for c in ctrl])
    beta, *_ = np.linalg.lstsq(X, x, rcond=None)
    res = x - X @ beta
    # 통제변수와 완전 공선이면 잔차가 부동소수 잡음(~1e-14)만 남는다 — 그 잡음과의 상관은
    # 무의미한 난수라서 반드시 0으로 눌러야 한다(합성 테스트에서 0.23이 나왔던 지점).
    if res.std() <= 1e-8 * max(x.std(), 1.0):
        return 0.0
    c = np.corrcoef(res, y)[0, 1]
    return float(c) if np.isfinite(c) else None


def analyze_neutral(obs) -> dict:
    """I가 RS와 별개인지 — RS 구간별 스프레드 + 통제 후 IC + 기간 분할."""
    buckets = {"rs_low": (0, 85), "rs_mid": (86, 92), "rs_high": (93, 99)}
    bspread = {k: [] for k in buckets}
    ic_raw, ic_ctrl_rs, ic_ctrl_both, ic_rs, ic_prox = [], [], [], [], []
    for o in obs:
        rows = o["rows"]
        for k, (lo, hi) in buckets.items():
            sub = [r for r in rows if lo <= r["rs"] <= hi]
            pos = [r["fwd"] for r in sub if r["flow"] > 0]
            neg = [r["fwd"] for r in sub if r["flow"] <= 0]
            if len(pos) >= 3 and len(neg) >= 3:
                bspread[k].append(np.mean(pos) - np.mean(neg))
        if len(rows) >= 15:
            for lst, key, ctrl in ((ic_raw, "flow_cap", []),
                                   (ic_ctrl_rs, "flow_cap", ["rs"]),
                                   (ic_ctrl_both, "flow_cap", ["rs", "prox"]),
                                   (ic_rs, "rs", []), (ic_prox, "prox", [])):
                v = _resid_ic(rows, key, ctrl)
                if v is not None:
                    lst.append(v)
    half = len(obs) // 2
    sub = {}
    for name, part in (("first_half", obs[:half]), ("second_half", obs[half:])):
        sp = []
        for o in part:
            pos = [r["fwd"] for r in o["rows"] if r["flow"] > 0]
            neg = [r["fwd"] for r in o["rows"] if r["flow"] <= 0]
            if len(pos) >= 3 and len(neg) >= 3:
                sp.append(np.mean(pos) - np.mean(neg))
        sub[name] = {"months": len(sp),
                     "spread_pct": round(float(np.mean(sp)) * 100, 3) if sp else None,
                     "spread_t": round(_t_stat(sp), 2) if sp else None,
                     "range": [str(part[0]["sig"].date()), str(part[-1]["sig"].date())] if part else None}
    def _s(x):
        return {"mean": round(float(np.mean(x)), 4), "t": round(_t_stat(x), 2), "n": len(x)} if x else None
    return {
        "rs_bucket_spread_pct": {k: (round(float(np.mean(v)) * 100, 3) if v else None)
                                 for k, v in bspread.items()},
        "rs_bucket_spread_t": {k: (round(_t_stat(v), 2) if v else None) for k, v in bspread.items()},
        "rs_bucket_months": {k: len(v) for k, v in bspread.items()},
        "ic_flow_raw": _s(ic_raw), "ic_flow_ctrl_rs": _s(ic_ctrl_rs),
        "ic_flow_ctrl_rs_prox": _s(ic_ctrl_both),
        "ic_rs_alone": _s(ic_rs), "ic_prox_alone": _s(ic_prox),
        "subperiod": sub,
    }


def mini_port(obs, top: int = 20) -> dict:
    """DART 없는 미니 포트폴리오 — 'I를 필터로 쓰면 코어 상위군이 나아지나'.

    라이브 선정 규칙(요건 통과 후 RS 내림차순 top N)의 DART 없는 축소판이다.
    점수화(가점)와 필터(관문)를 가르려면 이 비교가 필요하다.
    """
    out = {}
    for name, pick in (
        ("core_top", lambda rs: rs),
        ("core_top_I_pos", lambda rs: [r for r in rs if r["flow"] > 0]),
        ("core_top_I_neg", lambda rs: [r for r in rs if r["flow"] <= 0]),   # 반증 대조군
        ("core_top_I_rank", None),      # RS 대신 flow_cap 상위 (I만으로 뽑기)
    ):
        rets, months = [], 0
        for o in obs:
            rows = sorted(o["rows"], key=lambda r: -r["rs"])
            sel = (sorted(o["rows"], key=lambda r: -r["flow_cap"])[:top] if pick is None
                   else pick(rows)[:top])
            if len(sel) < 5:
                continue
            rets.append(float(np.mean([r["fwd"] for r in sel])))
            months += 1
        if not rets:
            out[name] = None
            continue
        a = np.asarray(rets)
        cagr = (float(np.prod(1 + a)) ** (12 / len(a)) - 1) * 100
        out[name] = {"months": months, "mean_monthly_pct": round(float(a.mean()) * 100, 3),
                     "cagr_pct": round(cagr, 2), "vol_pct": round(float(a.std(ddof=1)) * 100, 2),
                     "worst_month_pct": round(float(a.min()) * 100, 2),
                     "win_rate_pct": round(float((a > 0).mean()) * 100, 1)}
    return out


def run_variants(days, rebals, p, closes, opens, volumes, bench, store: FlowStore) -> dict:
    """3단계 — 라이브 파이프라인(7요건+DART)에 I를 넣는 세 가지 방식을 포트폴리오로 비교.

      base      : 현행 (7비트 score≥5, RS 내림차순 top20)
      i_gate    : 현행 + I>0 관문
      i_bit8_5  : I를 8번째 비트로 넣고 score≥5 (I가 다른 요건을 대체할 수 있게 됨)
      i_bit8_6  : 8비트 score≥6 (통과율을 현행과 비슷하게 맞춘 비교)
      i_rank    : 요건은 현행, 선정 순서만 RS → I(시총 정규화)
      i_neg_gate: I≤0 관문 (반증 대조군 — 여기가 나쁘지 않으면 신호가 아니다)
    """
    ni = cbt.NIStore()
    variants = ("base", "i_gate", "i_bit8_5", "i_bit8_6", "i_rank", "i_neg_gate")
    screens = {v: [] for v in variants}
    for sig, ex in rebals:
        _, n_pre, rows = cbt.screen_at(sig, p, ni, closes, volumes)
        ni.save()
        flows = store.get(days, sig)
        for r in rows:
            f = flows.get(r["code"])
            r["flow"] = f
            r["flow_cap"] = (f / r["cap"]) if (f is not None and r.get("cap")) else None
        by_rs = sorted(rows, key=lambda r: -(r.get("rs_kkangto") or 0))
        pick = {
            "base": [r for r in by_rs if r["score"] >= 5],
            "i_gate": [r for r in by_rs if r["score"] >= 5 and (r["flow"] or 0) > 0],
            "i_bit8_5": [r for r in by_rs if r["score"] + int((r["flow"] or 0) > 0) >= 5],
            "i_bit8_6": [r for r in by_rs if r["score"] + int((r["flow"] or 0) > 0) >= 6],
            "i_rank": sorted([r for r in rows if r["score"] >= 5],
                             key=lambda r: -(r["flow_cap"] if r["flow_cap"] is not None else -1e9)),
            "i_neg_gate": [r for r in by_rs if r["score"] >= 5 and (r["flow"] or 0) <= 0],
        }
        for v in variants:
            screens[v].append({"sig": sig, "ex": ex, "selected": pick[v][:p["top"]],
                               "n_prelim": n_pre, "uni_src": "pit", "top": p["top"]})
        logger.info("%s 선정수 %s", sig.date(),
                    {v: len(pick[v][:p['top']]) for v in variants})
    store.save()
    out = {}
    for v in variants:
        nav, trades, aux = cbt.vb.simulate(days, screens[v], opens, closes, p, 1.0, False)
        m = cbt.vb.metrics(nav, trades, bench, screens[v], aux, [])
        m["label"] = v
        out[v] = cbt.vb._grid_row(m)
        logger.info("[%s] CAGR %.2f%% (초과 %.2f%%p) MDD %.1f%% 승률 %s%%",
                    v, m["cagr_pct"], m["excess_cagr_pct"], m["mdd_pct"], m["win_rate"])
    out["_dart_calls"] = ni.calls
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--tag", default="")
    ap.add_argument("--spread", action="store_true", help="I 스프레드 검증 (기본 동작)")
    ap.add_argument("--variants", action="store_true",
                    help="3단계 — 7요건 파이프라인에 I를 관문/8번째비트/정렬키로 넣어 포트폴리오 비교 (DART 필요)")
    ap.add_argument("--limit-months", type=int, default=0)
    args = ap.parse_args()

    p = cbt._base_params()
    days, rebals, opens, closes, volumes, missing = cbt._load_market_data(args.start, args.end, p)
    if args.limit_months:
        rebals = rebals[:args.limit_months]

    store = FlowStore()
    out = {}
    if args.variants:
        bench = cbt.vb.load_bench(cbt.PX_START, cbt.PX_END)
        out["variants"] = run_variants(days, rebals, p, closes, opens, volumes, bench, store)
        out["meta"] = {"start": args.start, "end": args.end, "flow_calls": store.calls}
        (cbt.CBT_CACHE / f"cbt_ivar{args.tag}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    for core, key in ((True, "core_NL"), (False, "prefilter")):
        obs = collect(days, rebals, opens, closes, p, store, core=core)
        out[key] = analyze(obs)
        if core:
            # 직교성·기간분할·미니포트는 코어(라이브 후보군에 가까운 집합)에서만 본다
            out["core_neutral"] = analyze_neutral(obs)
            out["core_mini_port"] = mini_port(obs)
        logger.info("[%s] %s", key, json.dumps(out[key], ensure_ascii=False))
    out["meta"] = {"flow_calls": store.calls, "missing_prices": len(missing),
                   "flow_td": FLOW_TD, "start": args.start, "end": args.end,
                   "core_def": f"RS≥{p['th']['L']} & 52주근접≥{p['th']['N']}",
                   "prefilter_def": f"RS≥{p['pre_rs']} & 52주근접≥{p['pre_prox']}"}
    store.save()
    (cbt.CBT_CACHE / f"cbt_iflow{args.tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
