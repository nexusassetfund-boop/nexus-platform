"""전략 조합 백테스트 — 퀄리티성장 × 눌림목 × 신고가 후보 × CANSLIM (value_backtest 엔진 재사용).

질문: "2개 이상 전략에 동시에 잡힌 종목(교집합)을 사면 단독 전략보다 나은가?"
교집합은 표본이 급감할 위험이 커서(눌림목 prox 0.65~0.92 vs 신고가 gap≤12% ⇔ prox≥0.89
는 정의상 거의 배타적), 개선안 2종을 같은 조건의 비교군으로 함께 돌린다:
  gate×ranker: A 전략의 창 멤버를 B 전략 점수로 정렬 상위 N (12개 순서쌍)
  blend: 전략별 점수 풀 백분위 평균 상위 N (3개 이상 풀에 커버된 종목만)

멤버십 정의 (사용자 확정 — 신호일 기준 최근 20거래일 창, 10/40 민감도):
  일간 전략(눌림목·신고가): 창 내 일간 멤버십을 그대로 판정 (전부 패널 벡터화)
  월간 전략(퀄리티·CANSLIM): 신호일 멤버십만. window>=21이면 직전 신호일도 인정
    — 재무 기반이라 일중 변동이 무의미한, 창 규칙의 정직한 월간 해석 (리포트에 명시)

데이터·PIT:
  퀄리티/CANSLIM: 기존 quality_backtest.screen_at / canslim_backtest.screen_at 위임 (DART 필요)
  눌림목: pullback_screener 상수·산식을 FDR 가격 패널 rolling으로 재현 — pykrx 불필요.
    per_accel 비트(현재 시점 WISEreport 컨센서스 = look-ahead)는 제외 → 6점 만점, score>=4 유지
    (제외 비트는 컨센서스 제공 종목만 받던 가점이라 중소형은 원래 6점 만점이었음).
    일간 시총 = 직전 KRX 월간 덤프 cap × 수정주가비 스케일 근사 (증자·감자 왜곡 상한 1개월).
  신고가: newhigh_fetcher(스테이트리스)의 게이트를 marcap 패널 벡터화로 전 구간 재현.
    최근 3개 일자에 대해 원본 build_candidates 명단과 일치를 assert (parity check).

한계(리포트에 반드시 명시): 생존편향 상방(canslim_backtest와 동일), RS 백분위 모수가
가격 패널 보유 종목으로 제한되는 근사, marcap(신고가 멤버십)↔FDR(수익률)의 가격 소스 차,
눌림목 시장 구분이 최신 덤프 기준(이전상장 근사), 월간 전략의 창 해석 비대칭.

실행 (중간 산출물 tmp/combo_members_*.json 캐시 후엔 조합 실험이 수 초):
  python scanner/combo_backtest.py --members --strategy pullback   # 무DART, 로컬 가능
  python scanner/combo_backtest.py --members --strategy all        # quality/canslim은 DART 필요
  python scanner/combo_backtest.py --probe                         # 멤버 수·교집합 크기 점검
  python scanner/combo_backtest.py --run --window 20               # 본 실행
  python scanner/combo_backtest.py --grid                          # 창 10/40·top·slip_x2 민감도
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import canslim_backtest as cb   # 부수효과: backtest/value_backtest CACHE_DIR → 저장소 tmp/
import quality_backtest as qb   # 반드시 canslim_backtest 뒤 — CACHE_DIR 패치가 선행돼야 캐시가 tmp/에 잡힘
import newhigh_fetcher as nf
import pullback_screener as ps  # 상수만 사용 (pykrx는 지연 import라 안전)
import value_backtest as vb
import fetch_value

logger = logging.getLogger("combo_backtest")
ROOT = Path(__file__).parent.parent
CBT_CACHE = cb.CBT_CACHE
assert str(qb.FIN_CACHE_PATH).startswith(str(CBT_CACHE)), "quality 캐시가 저장소 tmp/ 밖 — import 순서 확인"
nf.CACHE = CBT_CACHE / "marcap"                    # marcap parquet도 같은 tmp/ 캐시에 편승
nf.OUT_CAND = CBT_CACHE / "combo_nhc_parity.json"  # parity check가 실데이터 파일을 덮지 않게

PX_START, PX_END = cb.PX_START, cb.PX_END  # 문자열까지 canslim과 동일해야 가격 pkl 캐시 히트
WINDOW_MAX = 40          # 멤버 JSON에 담는 최대 창 — 실험 창(10/20/40)은 조립 때 필터
MONTHLY_PREV_AGO = 21    # 월간 전략에서 직전 신호일의 ago 근사 (약 1개월 = 21거래일)
STRATS = ("quality", "canslim", "pullback", "newhigh")
TOP_COMBO = 10           # 조합 포트 슬롯 수 (NAV/10 고정, 부족분 현금)
TOP_SINGLE = 20          # 단독 전략 대조군 (기존 백테스트와 동일)
PULLBACK_MIN_SCORE = 4   # 6점 만점 기준 진입선 (라이브 권장선과 실질 동일 — 상단 독스트링)


def _members_path(strategy: str, tag: str = "") -> Path:
    return CBT_CACHE / f"combo_members_{strategy}{tag}.json"


def _name_map() -> dict[str, str]:
    names = {}
    for k in sorted(vb.load_krx_dumps()):
        for r in vb.load_krx_dumps()[k]["cap"]:
            names[str(r[0]).zfill(6)] = str(r[1])
    return names


def _save_members(strategy: str, kind: str, sigs: dict, tag: str = ""):
    out = {"kind": kind, "strategy": strategy, "window_max": WINDOW_MAX, "sigs": sigs}
    _members_path(strategy, tag).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    n = [len(v["m"]) for v in sigs.values()]
    logger.info("[%s] 멤버 저장: %d신호일, 멤버 수 평균 %.1f (min %d / max %d)",
                strategy, len(sigs), float(np.mean(n)) if n else 0, min(n, default=0), max(n, default=0))


# ── 멤버 생성기: 퀄리티 (기존 screen_at 위임, top=400으로 풀까지 확보) ──
def members_quality(rebals, closes, tag=""):
    if not fetch_value.DART_KEY:
        raise RuntimeError("quality 멤버 생성에는 DART_API_KEY 필요")
    qstore = qb.QualityStore()
    p = {**qb._base_params(), "top": 400}   # 관문 통과 전체가 점수 풀 (DART 비용은 동일)
    sigs = {}
    for sig, _ex in rebals:
        uni, _ = vb.fetch_universe_pit(sig)
        fund, cap, snap_d = vb.fetch_snapshot(sig)
        assert snap_d <= sig.strftime("%Y%m%d"), "look-ahead: 스냅샷이 신호일 이후"
        mom = vb._make_mom(closes, sig, p)
        ranked, n_gate = qb.screen_at(sig, uni, fund, cap, p, qstore, mom)
        qstore.save()
        pool = {r["code"]: [0, r["composite"]] for r in ranked}
        sigs[str(sig.date())] = {
            "m": {r["code"]: [0, r["composite"]] for r in ranked[:TOP_SINGLE]},
            "p": pool,
        }
        logger.info("%s quality: 관문 %d → 풀 %d", sig.date(), n_gate, len(pool))
    _save_members("quality", "monthly", sigs, tag)


# ── 멤버 생성기: CANSLIM (기존 screen_at 위임) + rs_only 대조군 ──
def members_canslim(rebals, closes, volumes, tag=""):
    store = cb.NIStore() if fetch_value.DART_KEY else None
    if store is None:
        raise RuntimeError("canslim 멤버 생성에는 DART_API_KEY 필요")
    p = cb._base_params()   # orig5 top20 (라이브와 동일)
    p_rs = {**cb._base_params(), "min_score": 0, "pre_prox": 0.0}   # rs_only — DART 미호출
    sigs, sigs_rs = {}, {}
    for sig, _ex in rebals:
        assert closes[closes.index <= sig].index.max() <= sig, "look-ahead: 가격이 신호일 이후"
        sel, n_pre, rows = cb.screen_at(sig, p, store, closes, volumes)
        store.save()
        scalar = lambda r: (r["score"] * 1000 + (r.get("rs_kkangto") or 0))  # 점수 우선, RS 타이브레이크
        sigs[str(sig.date())] = {
            "m": {r["code"]: [0, scalar(r)] for r in sel},
            "p": {r["code"]: [0, scalar(r)] for r in rows},
        }
        sel_rs, _, _ = cb.screen_at(sig, p_rs, None, closes, volumes)
        sigs_rs[str(sig.date())] = {
            "m": {r["code"]: [0, r.get("rs_kkangto") or 0] for r in sel_rs}, "p": {}}
        logger.info("%s canslim: 사전컷 %d → 선정 %d", sig.date(), n_pre, len(sel))
    _save_members("canslim", "monthly", sigs, tag)
    _save_members("rs_only", "monthly", sigs_rs, tag)


# ── 멤버 생성기: 눌림목 (패널 벡터화 — pykrx 불필요) ──
def _argmax_age(a: np.ndarray) -> float:
    if np.isnan(a).all():
        return np.nan
    return len(a) - 1 - int(np.nanargmax(a))


def _market_map() -> dict[str, str]:
    """code → KOSPI/KOSDAQ (최신 덤프 기준 — 이전상장은 최신 시장으로 근사)."""
    mkt = {}
    for k in sorted(vb.load_krx_dumps()):
        for r in vb.load_krx_dumps()[k]["cap"]:
            m = str(r[2] or "")
            mkt[str(r[0]).zfill(6)] = "KOSDAQ" if "KOSDAQ" in m else "KOSPI"
    return mkt


def _cap_panels(cf: pd.DataFrame):
    """일간 시총 추정(직전 덤프 cap × 수정주가비) + 가격 하한 통과 패널.

    수정주가비는 분할을 통과해도 시총 비율을 보존한다(주식수×가격 불변). 증자·감자
    왜곡은 다음 덤프(월간)에서 리셋 — 상한 1개월의 근사임을 리포트에 명시.
    """
    dumps = vb.load_krx_dumps()
    keys = sorted(dumps)
    idx = cf.index
    cap_est = pd.DataFrame(np.nan, index=idx, columns=cf.columns)
    price_ok = pd.DataFrame(False, index=idx, columns=cf.columns)
    for i, k in enumerate(keys):
        t0 = idx.searchsorted(pd.Timestamp(k), side="right") - 1
        if t0 < 0:
            continue
        t1 = (idx.searchsorted(pd.Timestamp(keys[i + 1]), side="right") - 1
              if i + 1 < len(keys) else len(idx) - 1)
        if t1 < t0:
            continue
        caps, px = {}, {}
        for r in dumps[k]["cap"]:
            c = str(r[0]).zfill(6)
            cv, pv = vb._knum(r[4]), vb._knum(r[3])
            if cv:
                caps[c] = cv
            if pv:
                px[c] = pv
        cap_row = pd.Series(caps).reindex(cf.columns)
        seg = cf.iloc[t0:t1 + 1]
        cap_est.iloc[t0:t1 + 1] = seg.div(cf.iloc[t0], axis=1).mul(cap_row, axis=1).values
        pok = (pd.Series(px).reindex(cf.columns) >= ps.MIN_PRICE).fillna(False).values
        price_ok.iloc[t0:t1 + 1] = np.repeat(pok[None, :], t1 + 1 - t0, axis=0)
    return cap_est, price_ok


def pullback_panels(closes: pd.DataFrame, volumes: pd.DataFrame,
                    cap_est: pd.DataFrame | None = None, price_ok: pd.DataFrame | None = None,
                    mkt: dict[str, str] | None = None):
    """(hard 필터 패널, 6점 스코어 패널, RS 패널) — 전부 신호일 이전 데이터만 쓰는 rolling.
    cap_est/price_ok/mkt 주입은 합성 패널 테스트용 (기본은 KRX 덤프에서 산출)."""
    cf = closes
    v = volumes.reindex(columns=cf.columns)
    r3, r6, r12 = cf / cf.shift(60) - 1, cf / cf.shift(120) - 1, cf / cf.shift(252) - 1
    # 깡토 RS: 시장별 백분위 (라이브 _pct_rank_map과 동일식 — (rank-1)/(n-1)*98+1)
    if mkt is None:
        mkt = _market_map()
    wr = 0.5 * r3 + 0.3 * r6 + 0.2 * r12
    rs = pd.DataFrame(np.nan, index=cf.index, columns=cf.columns)
    for m in ("KOSPI", "KOSDAQ"):
        cols = [c for c in cf.columns if mkt.get(c, "KOSPI") == m]
        if not cols:
            continue
        sub = wr[cols]
        rk, n = sub.rank(axis=1), sub.count(axis=1)
        rs[cols] = ((rk - 1).div((n - 1).clip(lower=1), axis=0) * 98 + 1).round()
    prox = cf / cf.rolling(252, min_periods=120).max()
    consol = cf.rolling(60, min_periods=60).apply(_argmax_age, raw=True)
    vol_dry = (v.rolling(5).mean() / v.rolling(60, min_periods=1).mean()) <= ps.DRY_RATIO
    ret = cf.pct_change()
    std20 = ret.rolling(20).std(ddof=0)
    vcp = std20 < std20.shift(20)
    vol2x = ((v > 2 * v.rolling(20).mean().shift(1)) & (cf.diff() > 0)) \
        .rolling(20, min_periods=1).max().astype(bool)
    if cap_est is None:
        cap_est, price_ok = _cap_panels(cf)
    hard = ((r6 >= ps.PCT_6M_MIN / 100) & (r12 >= ps.PCT_12M_MIN / 100)
            & (prox >= ps.PROX_MIN) & (prox <= ps.PROX_MAX)
            & (cap_est >= ps.MIN_CAP) & price_ok)
    # 6점 만점 (per_accel 제외 — 상단 독스트링). vol2x는 라이브도 점수 미반영(표시 전용)과 동일하게
    # _score의 7비트 중 rs_leader·deep·base_short·base_tight·vcp·vol_dry 6개만 쓴다.
    score = ((prox <= ps.DEEP_PROX_MAX).astype(int)
             + (consol >= ps.CONSOL_DAYS).astype(int)
             + (consol >= ps.CONSOL_TIGHT).astype(int)
             + vcp.astype(int) + vol_dry.astype(int)
             + (rs >= ps.RS_LEADER).astype(int))
    # vol2x는 멤버십·점수에 안 쓰지만 산출은 유지 — 라이브 스키마와의 대조 검증용
    _ = vol2x
    return hard, score, rs


def _window_rows(mask: pd.DataFrame, scorepan: pd.DataFrame, rebals) -> dict:
    """신호일별로 최근 WINDOW_MAX 거래일 내 멤버 {code: [ago, score]} (ago 최솟값 우선)."""
    dates = mask.index
    out = {}
    for sig, _ex in rebals:
        idx = int(dates.searchsorted(sig, side="right")) - 1
        if idx < 0:
            out[str(sig.date())] = {}
            continue
        assert dates[idx] <= sig, "look-ahead: 창 기준일이 신호일 이후"
        rows = {}
        for ago in range(min(WINDOW_MAX, idx) + 1):
            pos = idx - ago
            hit = mask.iloc[pos]
            for code in mask.columns[hit.fillna(False).values.astype(bool)]:
                if code not in rows:
                    sc = scorepan.iloc[pos][code]
                    rows[code] = [ago, None if pd.isna(sc) else round(float(sc), 2)]
        out[str(sig.date())] = rows
    return out


def members_pullback(rebals, closes, volumes, tag=""):
    hard, score, rs = pullback_panels(closes, volumes)
    member = hard & (score >= PULLBACK_MIN_SCORE)
    scorepan = score * 1000 + rs   # 점수 우선, RS 타이브레이크 (스칼라 정렬키)
    sigs_m = _window_rows(member, scorepan.where(member), rebals)
    sigs_p = _window_rows(hard, scorepan.where(hard), rebals)   # 풀 = 하드필터 통과 (score>=3 grid 파생용)
    sigs = {k: {"m": sigs_m[k], "p": sigs_p[k]} for k in sigs_m}
    _save_members("pullback", "daily", sigs, tag)


# ── 멤버 생성기: 신고가 후보 (marcap 패널 벡터화 + parity check) ──
def newhigh_panels(df: pd.DataFrame):
    close = nf._wide(df, "adjClose")
    high = nf._wide(df, "adjHigh", close)
    amount = nf._wide(df, "Amount", close)
    marcap = nf._wide(df, "Marcap", close)
    base = high.rolling(nf.HIGH52_BARS, min_periods=nf.HIGH52_MIN_BARS).max().shift(1)
    gap = (base - close) / close * 100
    avg20 = amount.rolling(20, min_periods=10).mean().shift(1)
    gap_ok = gap <= nf.ENTER_GAP
    carry = gap_ok.rolling(max(nf.CARRY_DAYS, 1), min_periods=1).max().astype(bool)
    watch_cap = nf.CARRY_MAX_GAP if nf.CARRY_DAYS > 0 else nf.GAP_WATCH
    ret = close / close.shift(nf.HIGH52_BARS) - 1
    rs = (ret.rank(axis=1, pct=True) * 100).round()   # 원본 rs_at과 동일식
    member = (carry & gap.notna() & (gap <= watch_cap)
              & (marcap >= nf.MIN_MARCAP) & (avg20 >= nf.MIN_AMOUNT_AVG20)
              & (amount >= nf.MIN_AMOUNT_TODAY) & (rs >= nf.MIN_RS))
    return member, gap, rs


def _parity_check(df: pd.DataFrame, member: pd.DataFrame):
    """벡터화 마스크가 원본 build_candidates의 snapshot 명단과 일치하는지 최근 3일 검증."""
    cand = nf.build_candidates(df, df["Date"].max())
    checked = 0
    for dstr, dd in sorted(cand["daily"].items())[-3:]:
        ref = {row[0] for row in dd["items"]}
        day = pd.Timestamp(dstr)
        got = set(member.columns[member.loc[day].fillna(False).values.astype(bool)])
        assert got == ref, f"parity 불일치 {dstr}: 벡터화-원본 = {sorted(got - ref)} / 원본-벡터화 = {sorted(ref - got)}"
        checked += 1
    logger.info("newhigh parity check 통과 (%d일)", checked)


def members_newhigh(rebals, tag=""):
    df = nf.add_adjusted(nf.download_marcap())
    df = df[df["Date"] <= pd.Timestamp(PX_END)].copy()
    member, gap, rs = newhigh_panels(df)
    _parity_check(df, member)
    scorepan = rs * 1000 - gap * 10   # RS 우선, gap 작을수록 우대
    sigs_m = _window_rows(member, scorepan.where(member), rebals)
    sigs = {k: {"m": v, "p": dict(v)} for k, v in sigs_m.items()}   # 풀 = 멤버 (별도 점수 풀 없음)
    _save_members("newhigh", "daily", sigs, tag)


# ── 조립 ──
def _load_all_members(tag="") -> dict:
    out = {}
    for s in list(STRATS) + ["rs_only"]:
        path = _members_path(s, tag)
        if not path.exists():
            raise RuntimeError(f"멤버 파일 없음: {path} — --members 먼저 실행")
        out[s] = json.loads(path.read_text(encoding="utf-8"))
    return out


def _sig_dict(mj: dict, sig_keys: list[str], i: int, window: int, field: str) -> dict:
    """i번째 신호일의 창 필터된 {code: (ago, score)}. 월간 전략은 window>=21이면 직전 신호일 포함."""
    cur = mj["sigs"].get(sig_keys[i], {}).get(field, {})
    out = {c: (v[0], v[1]) for c, v in cur.items() if v[0] <= window}
    if mj["kind"] == "monthly" and window >= MONTHLY_PREV_AGO and i > 0:
        prev = mj["sigs"].get(sig_keys[i - 1], {}).get(field, {})
        for c, v in prev.items():
            out.setdefault(c, (MONTHLY_PREV_AGO, v[1]))
    return out


def _build_screens(rebals, names, select_fn, top, keep_rank=None, hold_every=1, hold_phase=0) -> list[dict]:
    out = []
    held: list[str] = []
    for i, (sig, ex) in enumerate(rebals):
        codes = select_fn(i)
        if hold_every > 1 and i % hold_every != hold_phase:
            # 리밸런싱 안 하는 달 — 직전 목표를 그대로 유지하면 simulate가 매매를 내지 않는다.
            # 회전율만 줄이고 종목 선정 로직은 건드리지 않는다.
            pass
        elif keep_rank:
            # 잔류 규칙 — 매수는 top위, 매도는 keep_rank 밖으로 밀릴 때만.
            # 순위가 11위와 10위를 오가는 종목을 매달 갈아타는 낭비를 막는다.
            rank = {c: r for r, c in enumerate(codes)}
            keep = [c for c in held if rank.get(c, 10**9) < keep_rank]
            fill = [c for c in codes[:top] if c not in keep]
            held = (keep + fill)[:top]
        else:
            held = codes[:top]
        sel = [{"code": c, "name": names.get(c, c)} for c in held]
        out.append({"sig": sig, "ex": ex, "selected": sel,
                    "n_prelim": len(codes), "uni_src": "combo", "top": top})
    return out


def _turnover_variants(days, rebals, names, fn, top, bench, slip_mult):
    """잔류 규칙·분기 리밸런싱 조합별 (회전율, 성과) 매트릭스."""
    grid = [("기준 (매월 전량)", None, 1)]
    grid += [(f"잔류 {k}위", k, 1) for k in (15, 20, 30)]
    grid += [("분기 리밸런싱", None, 3)]
    grid += [(f"분기 + 잔류 {k}위", k, 3) for k in (20, 30)]
    grid += [(f"분기 위상{ph}", None, 3) for ph in (1, 2)]   # 시작 시점 우연 검증
    out = {}
    for label, keep, every in grid:
        ph = int(label[-1]) if label.startswith("분기 위상") else 0
        screens = _build_screens(rebals, names, fn, top, keep_rank=keep, hold_every=every, hold_phase=ph)
        codes = {r["code"] for s in screens for r in s["selected"]}
        o, c, _m = vb.load_prices(codes, PX_START, PX_END)
        nav, trades, aux = vb.simulate(days, screens, o, c, {"top": top}, slip_mult, False)
        m = vb.metrics(nav, trades, bench, screens, aux, [])
        bf = _bench_fill_row(nav, aux["cash_w"], bench)
        out[label] = {"cagr_pct": m["cagr_pct"], "mdd_pct": m["mdd_pct"], "sharpe": m["sharpe"],
                      "bf_excess_cagr_pct": round(bf["cagr_pct"] - m["bench_cagr_pct"], 2),
                      "turnover_annual_pct": m["turnover_annual_pct"],
                      "closed_trades": m["closed_trades"], "win_rate": m["win_rate"],
                      "avg_holdings": m["avg_holdings"], "avg_days": m["avg_days"]}
    return out


def portfolio_defs(members: dict, sig_keys: list[str], window: int,
                   pullback_min_scalar: float = PULLBACK_MIN_SCORE * 1000) -> dict:
    """label → (select_fn, top). select_fn(i) = 정렬된 코드 리스트."""
    def mem(s):
        return lambda i, _s=s: _sig_dict(members[_s], sig_keys, i, window, "m")

    def pool(s):
        return lambda i, _s=s: _sig_dict(members[_s], sig_keys, i, window, "p")

    def sig_day_members(s):
        """단독 대조군 — 신호일 당일(ago=0) 멤버만, 자체 점수 내림차순."""
        def f(i, _s=s):
            d = _sig_dict(members[_s], sig_keys, i, 0, "m")
            return sorted(d, key=lambda c: -(d[c][1] or 0))
        return f

    def isect(strats):
        fns = [mem(s) for s in strats]
        def f(i):
            ds = [fn(i) for fn in fns]
            common = set(ds[0]).intersection(*[set(d) for d in ds[1:]])
            # 정렬: 전략 간 점수 단위가 달라 합산 대신 '얼마나 최근에 잡혔나'(ago 합) → 코드
            return sorted(common, key=lambda c: (sum(d[c][0] for d in ds), c))
        return f

    def gate_rank(gate_s, rank_s):
        g, p = mem(gate_s), pool(rank_s)
        def f(i):
            gd, pdict = g(i), p(i)
            covered = [c for c in gd if c in pdict]
            f.cov_n += len(covered)
            f.gate_n += len(gd)
            return sorted(covered, key=lambda c: -(pdict[c][1] or 0))
        f.cov_n = f.gate_n = 0
        return f

    def blend(min_cover):
        fns = [pool(s) for s in STRATS]
        def f(i):
            pcts: dict[str, list] = {}
            for fn in fns:
                d = {c: v for c, v in fn(i).items() if v[1] is not None}
                ranked = sorted(d, key=lambda c: d[c][1])
                n = len(ranked)
                for j, c in enumerate(ranked):
                    pcts.setdefault(c, []).append(j / max(n - 1, 1))
            cand = {c: float(np.mean(v)) for c, v in pcts.items() if len(v) >= min_cover}
            return sorted(cand, key=lambda c: -cand[c])
        return f

    key = {"quality": "Q", "canslim": "C", "pullback": "P", "newhigh": "N"}
    defs: dict[str, tuple] = {}
    # 대조군: 단독 4전략 + rs_only (top20 — 기존 백테스트와 동일 조건)
    for s in STRATS:
        defs[f"solo_{s}"] = (sig_day_members(s), TOP_SINGLE)
    defs["rs_only"] = (sig_day_members("rs_only"), TOP_SINGLE)
    # 교집합 11개 (쌍 6 + 삼중 4 + 사중 1)
    for r in (2, 3, 4):
        for strats in itertools.combinations(STRATS, r):
            label = "x_" + "".join(key[s] for s in strats)
            defs[label] = (isect(strats), TOP_COMBO)
    # gate×ranker 12개 순서쌍
    for a, b in itertools.permutations(STRATS, 2):
        defs[f"g_{key[a]}r{key[b]}"] = (gate_rank(a, b), TOP_COMBO)
    # blend — 커버 3(기본)·2(완화) 둘 다. 멤버 캐시 위 조립이라 추가 비용은 시뮬 1회뿐
    defs["blend3"] = (blend(3), TOP_COMBO)
    defs["blend2"] = (blend(2), TOP_COMBO)
    return defs


# ── 시뮬·평가 ──
def _bench_fill_row(nav: pd.Series, cash_w: pd.Series, bench: pd.Series) -> dict:
    """공집합/부족분 현금을 벤치로 채웠을 때의 근사 — r' = r + 전일현금비중×벤치수익률.
    벤치 편입·이탈 거래비용은 미반영(유리한 방향) — 참고용."""
    b = bench.reindex(nav.index).ffill().pct_change().fillna(0.0)
    r = nav.pct_change().fillna(0.0)
    w = cash_w.reindex(nav.index).shift(1).fillna(1.0)
    nav_bf = (1.0 + r + w * b).cumprod()
    years = max((nav_bf.index[-1] - nav_bf.index[0]).days / 365.25, 1e-9)
    daily = nav_bf.pct_change().dropna()
    return {
        "cagr_pct": round((float(nav_bf.iloc[-1]) ** (1 / years) - 1) * 100, 2),
        "mdd_pct": round(float((nav_bf / nav_bf.cummax() - 1).min()) * 100, 1),
        "sharpe": round(float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0, 2),
    }


def run_portfolios(days, rebals, names, defs: dict, bench, tag="", slip_mult=1.0) -> dict:
    all_codes = set()
    screens_map = {}
    for label, (fn, top) in defs.items():
        screens = _build_screens(rebals, names, fn, top)
        screens_map[label] = screens
        all_codes |= {r["code"] for s in screens for r in s["selected"]}
    opens, closes, missing = vb.load_prices(all_codes, PX_START, PX_END)
    results = {}
    for label, screens in screens_map.items():
        top = screens[0]["top"]
        nav, trades, aux = vb.simulate(days, screens, opens, closes, {"top": top}, slip_mult, False)
        m = vb.metrics(nav, trades, bench, screens, aux, [])
        row = vb._grid_row(m)
        n_sel = [len(s["selected"]) for s in screens]
        row["avg_selected"] = round(float(np.mean(n_sel)), 1)
        row["empty_month_pct"] = round(sum(1 for n in n_sel if n == 0) / len(n_sel) * 100, 1)
        row["low_sample"] = bool(row["avg_holdings"] < 3 or row["empty_month_pct"] > 30)
        row["yearly_pct"] = m["yearly_pct"]
        fn = defs[label][0]
        if getattr(fn, "gate_n", 0):
            row["ranker_coverage_pct"] = round(fn.cov_n / fn.gate_n * 100, 1)
        # 교집합은 후보가 top 슬롯보다 적으면 나머지가 자동 현금이 된다. 하락장에서는
        # '현금이라서' 벤치를 이기는데 그건 알파가 아니다 — 남는 현금을 벤치에 넣은
        # 대안 수익률을 항상 같이 낸다. 이걸 이기지 못하면 그 조합의 초과수익은 현금 효과다.
        row["bench_fill"] = _bench_fill_row(nav, aux["cash_w"], bench)
        row["bf_excess_cagr_pct"] = round(row["bench_fill"]["cagr_pct"] - m["bench_cagr_pct"], 2)
        results[label] = row
        logger.info("[%s] 초과 %.2f%%p (현금중립 %.2f%%p) MDD %.1f%% 보유 %.1f 현금 %.0f%% 공집합 %.0f%%%s",
                    label, row["excess_cagr_pct"], row["bf_excess_cagr_pct"], row["mdd_pct"],
                    row["avg_holdings"], row["avg_cash_pct"], row["empty_month_pct"],
                    " [표본부족]" if row["low_sample"] else "")
    return results


# ── 돌파 매수 + 트레일링 스탑 (달력 리밸런싱 대체) ──
# 분기 리밸런싱이 '몇 월에 시작하느냐'에 12%p 흔들린 원인은 매매 시점을 달력이 정했기 때문이다.
# 여기서는 가격이 정한다 — 52주 고가 돌파일에 사고, 고점 대비 trail% 밀리면 판다.
# 위상이라는 자유 파라미터가 애초에 존재하지 않는다.

def _quality_daily(members: dict, sig_keys: list[str], days) -> list[dict]:
    """거래일별 '그 시점 최신 신호일'의 퀄리티 점수맵 (전진 채움 — look-ahead 없음)."""
    sig_ts = [pd.Timestamp(k) for k in sig_keys]
    pools = [{c: v[1] for c, v in members["quality"]["sigs"][k]["p"].items() if v[1] is not None}
             for k in sig_keys]
    out, j = [], -1
    for d in days:
        while j + 1 < len(sig_ts) and sig_ts[j + 1] <= d:
            j += 1
        out.append(pools[j] if j >= 0 else {})
    return out


def simulate_breakout(days, member, gap, qdaily, opens, closes, bench,
                      trail=0.20, max_pos=10, slip_mult=1.0, fresh_only=True):
    """돌파일 매수 → 트레일링 스탑 매도. 신호는 종가로 판정하고 체결은 익일 시가(look-ahead 차단)."""
    buy_slip, sell_slip = vb.BUY_SLIP * slip_mult, vb.SELL_SLIP * slip_mult
    sell_cost = vb.SELL_TAX + vb.COMMISSION_RT
    cf = closes.ffill()
    cash, pos, trades = 1.0, {}, []
    nav_hist, cash_hist, hold_hist, traded = [], [], [], 0.0
    pend_buy: list[str] = []
    pend_sell: list[str] = []

    def px(m, d, c):
        if c in m.columns and d in m.index:
            v = m.at[d, c]
            if v == v and v > 0:
                return float(v)
        return None

    for i, d in enumerate(days):
        # 1) 전일 신호 체결 — 매도 먼저 (현금 확보 후 매수)
        for c in pend_sell:
            if c not in pos:
                continue
            o = px(opens, d, c) or px(cf, d, c)
            if o is None:
                continue
            h = pos.pop(c)
            net = o * (1 - sell_slip) * (1 - sell_cost)
            cash += h["shares"] * net
            traded += h["shares"] * o
            trades.append({"ticker": c, "ret": round((net / h["cost"] - 1) * 100, 2),
                           "days": (d - h["entry"]).days, "reason": "trail_stop"})
        pend_sell = []
        if pend_buy:
            nav_now = cash + sum(h["shares"] * (px(cf, d, c) or h["cost"]) for c, h in pos.items())
            budget = nav_now / max_pos
            for c in pend_buy:
                if c in pos or len(pos) >= max_pos:
                    continue
                o = px(opens, d, c)
                if o is None:
                    continue
                fill = o * (1 + buy_slip)
                shares = min(budget, cash) / fill
                if shares <= 0:
                    continue
                cash -= shares * fill
                traded += shares * fill
                pos[c] = {"shares": shares, "cost": fill, "entry": d, "peak": fill}
            pend_buy = []

        # 2) 당일 종가로 다음날 신호 산출
        for c, h in pos.items():
            p = px(cf, d, c)
            if p:
                h["peak"] = max(h["peak"], p)
                if p < h["peak"] * (1 - trail):
                    pend_sell.append(c)
        if len(pos) - len(pend_sell) < max_pos and d in member.index:
            m_now = member.loc[d]
            g_now, g_prev = gap.loc[d], (gap.iloc[max(0, gap.index.get_loc(d) - 1)])
            qs = qdaily[i]
            cands = []
            for c in member.columns[m_now.fillna(False).values.astype(bool)]:
                if c in pos or c not in qs or c not in opens.columns:
                    continue
                gv, gp = g_now.get(c), g_prev.get(c)
                if gv != gv or gv > 0:                      # 돌파 상태(gap<=0) 아니면 제외
                    continue
                if fresh_only and not (gp == gp and gp > 0):  # 전일엔 미돌파 = 돌파 당일만
                    continue
                cands.append(c)
            cands.sort(key=lambda c: -qs[c])                 # 자리 경쟁은 퀄리티 점수 순
            pend_buy = cands[:max_pos - len(pos) + len(pend_sell)]

        nav = cash + sum(h["shares"] * (px(cf, d, c) or h["cost"]) for c, h in pos.items())
        nav_hist.append((d, nav))
        cash_hist.append((d, cash / nav if nav > 0 else 0.0))
        hold_hist.append((d, len(pos)))

    nav = pd.Series(dict(nav_hist)).sort_index()
    aux = {"holdings": pd.Series(dict(hold_hist)).sort_index(),
           "cash_w": pd.Series(dict(cash_hist)).sort_index(),
           "traded_notional": traded, "n_forced": 0, "n_unpriced": 0, "n_unfilled": 0,
           "open_positions": len(pos)}
    screens = [{"selected": [], "top": max_pos, "uni_src": "breakout"}]
    m = vb.metrics(nav, trades, bench, screens, aux, [])
    bf = _bench_fill_row(nav, aux["cash_w"], bench)
    return {"cagr_pct": m["cagr_pct"], "mdd_pct": m["mdd_pct"], "sharpe": m["sharpe"],
            "excess_cagr_pct": m["excess_cagr_pct"],
            "bf_excess_cagr_pct": round(bf["cagr_pct"] - m["bench_cagr_pct"], 2),
            "turnover_annual_pct": m["turnover_annual_pct"], "closed_trades": m["closed_trades"],
            "win_rate": m["win_rate"], "avg_win": m["avg_win"], "avg_loss": m["avg_loss"],
            "avg_days": m["avg_days"], "avg_holdings": m["avg_holdings"],
            "avg_cash_pct": m["avg_cash_pct"], "open_positions": aux["open_positions"],
            "yearly_pct": m["yearly_pct"]}


# ── 리밸런싱 주기 스윕 (회전율-알파 곡선의 꼭짓점 탐색) ──
# §5-C에서 "회전이 곧 알파의 전달 경로"가 드러났다. 그렇다면 매월(회전 472%)이 정점인지
# 그냥 시험 범위의 끝이었는지 모른다. 주기를 5·10·21·42·63거래일로 훑어 곡선을 그린다.
# 분기 실험의 교훈대로 주기마다 위상을 여러 개 돌려 평균·분산을 함께 낸다.

def _freq_rebals(days, n: int, phase: int = 0):
    return [(days[i], days[i + 1]) for i in range(len(days) - 1) if i % n == phase]


def freq_screens(rebals, names, mem_win, qdaily_by_date, top=10):
    """신호일마다 (최근 창 내 신고가 멤버) ∩ (그 시점 퀄리티 풀) 중 점수 상위 top."""
    out = []
    for sig, ex in rebals:
        qs = qdaily_by_date.get(sig, {})
        row = mem_win.loc[sig] if sig in mem_win.index else None
        codes = []
        if row is not None and qs:
            cand = [c for c in mem_win.columns[row.fillna(False).values.astype(bool)] if c in qs]
            cand.sort(key=lambda c: -qs[c])
            codes = cand[:top]
        out.append({"sig": sig, "ex": ex,
                    "selected": [{"code": c, "name": names.get(c, c)} for c in codes],
                    "n_prelim": len(codes), "uni_src": "freq", "top": top})
    return out


# ── 실행 ──
def _calendar(args):
    days, rebals = vb.build_calendar(args.start, args.end)
    if args.limit_months:
        rebals = rebals[:args.limit_months]
    return days, rebals


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--members", action="store_true", help="전략별 창 멤버 JSON 생성")
    ap.add_argument("--strategy", default="all", choices=list(STRATS) + ["all"])
    ap.add_argument("--probe", action="store_true", help="멤버 수·교집합 크기 점검 (멤버 캐시 필요)")
    ap.add_argument("--run", action="store_true", help="교집합 11 + 개선안 + 대조군 시뮬")
    ap.add_argument("--grid", action="store_true", help="창 10/40·pullback s3·blend2·top·slip_x2")
    ap.add_argument("--turnover", action="store_true", help="회전율 감축(잔류 규칙·분기 리밸런싱) 실험")
    ap.add_argument("--breakout", action="store_true", help="돌파 매수 + 트레일링 스탑 (달력 리밸런싱 대체)")
    ap.add_argument("--freq", action="store_true", help="리밸런싱 주기 스윕 1주~3개월 (위상 전수)")
    ap.add_argument("--slip", type=float, default=1.0, help="슬리피지 배수")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--limit-months", type=int, default=0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    days, rebals = _calendar(args)
    logger.info("거래일 %d, 신호일 %d회 (%s ~ %s)", len(days), len(rebals), args.start, args.end)

    if args.members:
        want = STRATS if args.strategy == "all" else (args.strategy,)
        need_panel = {"quality", "canslim", "pullback"} & set(want)
        closes = volumes = None
        if need_panel:
            _d, _r, _o, closes, volumes, missing = cb._load_market_data(args.start, args.end, cb._base_params())
            logger.info("가격 패널 %d종목 (결측 %d)", closes.shape[1], len(missing))
        if "pullback" in want:
            members_pullback(rebals, closes, volumes, args.tag)
        if "newhigh" in want:
            members_newhigh(rebals, args.tag)
        if "canslim" in want:
            members_canslim(rebals, closes, volumes, args.tag)
        if "quality" in want:
            members_quality(rebals, closes, args.tag)
        return

    members = _load_all_members(args.tag)
    sig_keys = [str(sig.date()) for sig, _ in rebals]
    miss = [k for k in sig_keys if k not in members["pullback"]["sigs"]]
    if miss:
        logger.warning("멤버 파일에 없는 신호일 %d개 (예: %s) — 해당 월은 공집합 처리", len(miss), miss[:3])

    if args.probe:
        key = {"quality": "Q", "canslim": "C", "pullback": "P", "newhigh": "N"}
        counts = {s: [] for s in STRATS}
        isects = {}
        for i in range(len(sig_keys)):
            ms = {s: _sig_dict(members[s], sig_keys, i, args.window, "m") for s in STRATS}
            for s in STRATS:
                counts[s].append(len(ms[s]))
            for a, b in itertools.combinations(STRATS, 2):
                isects.setdefault(f"{key[a]}∩{key[b]}", []).append(len(set(ms[a]) & set(ms[b])))
        print(f"=== 창 {args.window}거래일 기준 신호일별 멤버 수 (평균/최소/최대) ===")
        for s in STRATS:
            c = counts[s]
            print(f"  {s:9s}: {np.mean(c):5.1f} / {min(c)} / {max(c)}")
        print("=== 쌍별 교집합 크기 (평균 / 0인 신호일 비율) ===")
        for k, v in isects.items():
            print(f"  {k}: {np.mean(v):4.1f} / {sum(1 for x in v if x == 0) / len(v) * 100:.0f}%")
        return

    names = _name_map()
    bench = vb.load_bench(PX_START, PX_END)

    if args.turnover:
        defs = portfolio_defs(members, sig_keys, args.window)
        out = {}
        for label in ("g_NrQ", "g_QrN"):
            fn, _t = defs[label]
            for slip in (1.0, 2.0):
                key = f"{label}_slip{slip:g}x"
                out[key] = _turnover_variants(days, rebals, names, fn, TOP_COMBO, bench, slip)
                for v_label, v in out[key].items():
                    logger.info("[%s | %s] 회전율 %6.1f%% CAGR %6.2f%% 현금중립 %6.2f%%p "
                                "MDD %5.1f%% 거래 %3d 보유일 %.0f",
                                key, v_label, v["turnover_annual_pct"], v["cagr_pct"],
                                v["bf_excess_cagr_pct"], v["mdd_pct"], v["closed_trades"],
                                v["avg_days"] or 0)
        path = CBT_CACHE / f"combo_turnover{args.tag}.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("저장: %s", path)

    if args.freq:
        df = nf.add_adjusted(nf.download_marcap())
        df = df[df["Date"] <= pd.Timestamp(PX_END)].copy()
        nh_member, _g, _rs = newhigh_panels(df)
        qsigs = members["quality"]["sigs"]
        qcodes = {c for k in sig_keys if k in qsigs for c in qsigs[k]["p"]}
        codes = sorted(qcodes & set(nh_member.columns))
        opens, closes, _m = vb.load_prices(set(codes), PX_START, PX_END)
        idx = [d for d in closes.index if pd.Timestamp(args.start) <= d <= pd.Timestamp(args.end)]
        nh = nh_member.reindex(index=idx, columns=closes.columns).fillna(False)
        mem_win = nh.rolling(args.window, min_periods=1).max().astype(bool)   # 창 내 1회라도 멤버
        qd = dict(zip(idx, _quality_daily(members, sig_keys, idx)))
        logger.info("주기 스윕 유니버스 %d종목, 거래일 %d", len(codes), len(idx))

        out = {}
        for n, label in ((5, "1주"), (10, "2주"), (21, "1개월"), (42, "2개월"), (63, "3개월")):
            phases = sorted(set(round(k * n / 5) % n for k in range(5)))
            rows = []
            for ph in phases:
                rb = _freq_rebals(idx, n, ph)
                if len(rb) < 8:
                    continue
                sc = freq_screens(rb, names, mem_win, qd)
                nav, tr, aux = vb.simulate(idx, sc, opens, closes, {"top": 10}, args.slip)
                m = vb.metrics(nav, tr, bench, sc, aux, [])
                bf = _bench_fill_row(nav, aux["cash_w"], bench)
                rows.append({"phase": ph, "bf_excess": round(bf["cagr_pct"] - m["bench_cagr_pct"], 2),
                             "cagr": m["cagr_pct"], "mdd": m["mdd_pct"], "sharpe": m["sharpe"],
                             "turnover": m["turnover_annual_pct"], "trades": m["closed_trades"],
                             "win_rate": m["win_rate"]})
            ex = [r["bf_excess"] for r in rows]
            out[label] = {"n_days": n, "phases": rows,
                          "mean_bf_excess": round(float(np.mean(ex)), 2),
                          "min_bf_excess": min(ex), "max_bf_excess": max(ex),
                          "spread": round(max(ex) - min(ex), 2),
                          "mean_turnover": round(float(np.mean([r["turnover"] for r in rows])), 1),
                          "mean_mdd": round(float(np.mean([r["mdd"] for r in rows])), 1)}
            logger.info("%-4s (%2d거래일) 회전율 %6.1f%%  현금중립초과 평균 %6.2f%%p "
                        "[%6.2f ~ %6.2f, 폭 %5.2f]  MDD %5.1f%%  위상 %d개",
                        label, n, out[label]["mean_turnover"], out[label]["mean_bf_excess"],
                        out[label]["min_bf_excess"], out[label]["max_bf_excess"],
                        out[label]["spread"], out[label]["mean_mdd"], len(rows))
        p = CBT_CACHE / f"combo_freq{args.tag}.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("저장: %s", p)
        return

    if args.breakout:
        df = nf.add_adjusted(nf.download_marcap())
        df = df[df["Date"] <= pd.Timestamp(PX_END)].copy()
        nh_member, nh_gap, _rs = newhigh_panels(df)
        qsigs = members["quality"]["sigs"]
        qcodes = {c for k in sig_keys if k in qsigs for c in qsigs[k]["p"]}
        codes = sorted(qcodes & set(nh_member.columns))
        logger.info("돌파 후보 유니버스 %d종목 (퀄리티 풀 ∩ 신고가 패널)", len(codes))
        opens, closes, _miss = vb.load_prices(set(codes), PX_START, PX_END)
        idx = [d for d in closes.index if pd.Timestamp(args.start) <= d <= pd.Timestamp(args.end)]
        nh_member = nh_member.reindex(index=idx, columns=closes.columns).fillna(False)
        nh_gap = nh_gap.reindex(index=idx, columns=closes.columns)
        out = {}

        def run(label, days_, **kw):
            qd = _quality_daily(members, sig_keys, days_)
            r = simulate_breakout(days_, nh_member, nh_gap, qd, opens, closes, bench, **kw)
            out[label] = r
            logger.info("  %-22s 현금중립초과 %6.2f%%p  CAGR %6.2f%%  MDD %5.1f%%  회전율 %5.0f%%  "
                        "거래 %3d  승률 %s%%  보유일 %s  종목 %.1f",
                        label, r["bf_excess_cagr_pct"], r["cagr_pct"], r["mdd_pct"],
                        r["turnover_annual_pct"], r["closed_trades"], r["win_rate"],
                        r["avg_days"], r["avg_holdings"])

        logger.info("=== 트레일링 스탑 폭 ===")
        for t in (0.10, 0.15, 0.20, 0.25, 0.30):
            run(f"트레일 {t:.0%}", idx, trail=t)
        logger.info("=== 동시 보유 종목 수 (트레일 20%%) ===")
        for n in (5, 15):
            run(f"트레일20 · {n}종목", idx, trail=0.20, max_pos=n)
        logger.info("=== 진입 규칙 완화 ===")
        run("돌파상태 진입", idx, trail=0.20, fresh_only=False)
        logger.info("=== 슬리피지 ×2 ===")
        for t in (0.15, 0.20, 0.25):
            run(f"트레일 {t:.0%} · 슬립×2", idx, trail=t, slip_mult=2.0)
        logger.info("=== 시작일 민감도 (달력 위상의 대체 검증) ===")
        for off in (1, 2, 3, 6):
            sub = [d for d in idx if d >= pd.Timestamp(args.start) + pd.DateOffset(months=off)]
            run(f"시작 +{off}개월", sub, trail=0.20)
        p = CBT_CACHE / f"combo_breakout{args.tag}.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("저장: %s", p)
        return

    if args.turnover:
        # 기본 케이스가 슬리피지 ×2에서 우위를 잃는다(회전율 472%). 잔류 규칙·분기 리밸런싱으로
        # 회전율을 낮췄을 때 스트레스를 통과하는지가 채택/폐기를 가른다.
        defs = portfolio_defs(members, sig_keys, args.window)
        out = {}
        for label in ("g_NrQ", "g_QrN"):
            fn, _ = defs[label]
            for slip in (1.0, 2.0):
                key = f"{label}_slip{slip:g}x"
                out[key] = _turnover_variants(days, rebals, names, fn, TOP_COMBO, bench, slip)
                logger.info("=== %s ===", key)
                for k, v in out[key].items():
                    logger.info("  %-18s 회전율 %6.1f%%  현금중립초과 %6.2f%%p  CAGR %6.2f%%  MDD %5.1f%%  거래 %3d",
                                k, v["turnover_annual_pct"], v["bf_excess_cagr_pct"],
                                v["cagr_pct"], v["mdd_pct"], v["closed_trades"])
        p = CBT_CACHE / f"combo_turnover{args.tag}.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("저장: %s", p)
        return

    if args.run:
        defs = portfolio_defs(members, sig_keys, args.window)
        results = run_portfolios(days, rebals, names, defs, bench, args.tag)
        out = CBT_CACHE / f"combo_result{args.tag}.json"
        out.write_text(json.dumps({"window": args.window, "results": results},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(results, ensure_ascii=False, indent=1))
        logger.info("저장: %s", out)

    if args.grid:
        grid = {}
        base_defs = portfolio_defs(members, sig_keys, args.window)
        base = run_portfolios(days, rebals, names, base_defs, bench, args.tag)
        grid[f"w{args.window}"] = base
        for w in (10, WINDOW_MAX):
            if w == args.window:
                continue
            grid[f"w{w}"] = run_portfolios(days, rebals, names,
                                           portfolio_defs(members, sig_keys, w), bench, args.tag)
        # pullback score>=3 변형: 풀(하드필터)에서 scalar>=3000을 멤버로 파생
        pb3 = json.loads(json.dumps(members["pullback"]))
        for k, v in pb3["sigs"].items():
            v["m"] = {c: w_ for c, w_ in v["p"].items() if (w_[1] or 0) >= 3000}
        m3 = {**members, "pullback": pb3}
        d3 = {k: v for k, v in portfolio_defs(m3, sig_keys, args.window).items()
              if k.startswith("x_") and "P" in k}
        grid["pullback_s3"] = run_portfolios(days, rebals, names, d3, bench, args.tag)
        # 상위 조합(표본 충분 + 초과수익 순) 3개: top5/top15 + slip_x2 + bench_fill
        # 상위 선정은 현금중립 초과수익(bf_excess) 기준 — 그냥 초과수익으로 뽑으면
        # 하락장에 현금만 들고 있던 조합이 올라온다 (bench_fill 주석 참조)
        ranked = sorted((k for k, v in base.items()
                         if not v["low_sample"] and (k.startswith("x_") or k.startswith("g_") or k == "blend3")),
                        key=lambda k: -base[k]["bf_excess_cagr_pct"])[:3]
        for label in ranked:
            fn = portfolio_defs(members, sig_keys, args.window)[label][0]
            for top in (5, 15):
                grid[f"{label}_top{top}"] = run_portfolios(
                    days, rebals, names, {label: (fn, top)}, bench, args.tag)[label]
            grid[f"{label}_slip_x2"] = run_portfolios(
                days, rebals, names, {label: (fn, TOP_COMBO)}, bench, args.tag,
                slip_mult=2.0)[label]
        out = CBT_CACHE / f"combo_grid{args.tag}.json"
        out.write_text(json.dumps(grid, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info("저장: %s", out)


if __name__ == "__main__":
    main()
