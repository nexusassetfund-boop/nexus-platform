"""수급 감지기 설계 검증 백테스트 — "연속일수 vs 강도" 중 뭐가 실제로 맞는가.

측정하려는 것 (설계 논쟁 그대로):
  Q1. 무관용 연속일수(hard streak)가 신호로서 쓸모가 있는가? 4일 vs 5일?
  Q2. 흠집 허용 스트릭(tolerance)이 무관용보다 나은가? — "10만주씩 4일 사다 1만주 매도"
      한 방에 리셋되는 문제를 고치면 실제로 신호가 좋아지는지
  Q3. 스트릭(이산) vs 강도(연속, 거래량 정규화)? 어느 쪽 IC가 높은가
  Q4. 외국인 / 기관 / 쌍끌이 중 어느 주체가 정보를 갖는가
  Q5. 1일 집중도 필터(리밸런싱·블록딜 배제)가 개선을 주는가
  Q6. 추세형(종가>MA20) vs 역발상형(종가<MA20) — 어느 쪽에서 수급이 먹히는가
  Q7. 매도→매수 전환(rev)이 신호인가 — 직전 매도 스트릭 문턱 3/4/5 민감도 포함
  Q8. KRX 상세가 닿을 때: 연기금(P 등급 조건), 기관합계−금융투자(inst_xf)가
      기관합계보다 나은가 — 스크리너가 실제로 쓰는 기준의 사후 검증
  대조군: mom20(20일 모멘텀). 수급이 모멘텀의 재탕이면 IC가 이것과 겹친다.

데이터: flow_history.FlowCache (네이버 일별 수급). 종가도 같은 표에서 나오므로
        수익률 계산에 추가 API가 필요 없다. KRX 상세(연기금·금융투자)는
        detail_available() 통과 시(KRX_ID/KRX_PW 필요)만 수집 — 차단 환경에선
        해당 신호가 표에서 빠질 뿐 나머지는 그대로 돈다.
평가:  주간(5거래일) 리밸런싱 시점마다 횡단면 스피어만 IC(fwd 20거래일) →
       평균 IC·t값, 그리고 상·하위 분위 평균수익률.

한계 (해석 시 반드시 감안):
  · 생존편향 — 유니버스가 '현재' 상장사 스냅샷이다. 상방 편향.
  · 네이버 '기관'은 기관합계 — inst_xf(−금융투자)는 KRX 상세가 닿을 때만 별도 신호로 검증.
  · rev 같은 희소 이벤트는 횡단면 IC가 무의미하다(대부분 0) — 초과수익%·초과t·건수로 판단.
  · 종가는 네이버 수정주가 기준이나 권리락 처리 검증은 안 했다. 극단치 윈저라이즈로 방어.
  · 거래비용 미반영 — IC 비교용이지 수익률 약속이 아니다.

실행:
  python scanner/flow_backtest.py --warm            # 수급 이력 수집(최초 1회, 수 분)
  python scanner/flow_backtest.py                   # 전체 표
  python scanner/flow_backtest.py --limit 100 --pages 13   # 빠른 점검
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import flow_history as fh

logger = logging.getLogger("flow_backtest")
ROOT = Path(__file__).parent.parent
CACHE_DIR = Path(os.environ.get("CLAUDE_JOB_DIR", str(ROOT))) / "tmp"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FLOW_CACHE = CACHE_DIR / "flow_cache.json"
UNIV_CACHE = CACHE_DIR / "flow_universe.json"
OUT_PATH = CACHE_DIR / "flow_backtest.json"

FWD_TD = 20          # 성과 측정 구간 (거래일)
STEP_TD = 5          # 신호일 간격 (주간)
WINSOR = 0.5         # 수익률 ±50% 절단


# ── 통계 유틸 ───────────────────────────────────────────
def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 10:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


def tstat(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / (sd / math.sqrt(len(xs))) if sd else 0.0


# ── 신호 정의 ───────────────────────────────────────────
def _rev(side: dict, min_sell: int) -> float:
    return 1.0 if (side["rev_flip"] and side["rev_sell"] >= min_sell) else 0.0


def signals(m: dict, mom20: float | None, trend: bool = True,
            pen: dict | None = None, ixf: dict | None = None) -> dict[str, float]:
    """지표 → 검증할 신호 값들. 이산 신호는 0/1, 연속 신호는 값 그대로.
    pen/ixf: KRX 상세가 병합된 종목만 전달 — 없으면 해당 신호가 빠진다."""
    f, i = m["frgn"], m["inst"]
    both_streak_tol = min(f["streak"], i["streak"])
    conc_ok = max(f["concentr"], i["concentr"]) <= 0.6
    out = {
        # Q1 무관용 연속일수
        "hard_frgn_ge4": 1.0 if f["hard_streak"] >= 4 else 0.0,
        "hard_frgn_ge5": 1.0 if f["hard_streak"] >= 5 else 0.0,
        "hard_inst_ge4": 1.0 if i["hard_streak"] >= 4 else 0.0,
        "hard_both_ge4": 1.0 if min(f["hard_streak"], i["hard_streak"]) >= 4 else 0.0,
        # Q2 흠집 허용
        "tol_frgn_ge4": 1.0 if f["streak"] >= 4 else 0.0,
        "tol_frgn_ge5": 1.0 if f["streak"] >= 5 else 0.0,
        "tol_inst_ge4": 1.0 if i["streak"] >= 4 else 0.0,
        "tol_both_ge4": 1.0 if both_streak_tol >= 4 else 0.0,
        # Q3 연속 강도
        "intensity_frgn": f["intensity"],
        "intensity_inst": i["intensity"],
        "intensity_both": m["both_intensity"],
        # Q4 지속성
        "persist_frgn": f["persist"],
        "persist_inst": i["persist"],
        "persist_both": (f["persist"] + i["persist"]) / 2,
        # Q5 집중도 필터 적용판
        "intensity_both_conc": m["both_intensity"] if conc_ok else 0.0,
        "tol_both_ge4_conc": 1.0 if (both_streak_tol >= 4 and conc_ok) else 0.0,
        # 1차 결과 후속 검증 — 기관 강도가 유의한 (−)로 나와서 추가한 조합들
        "frgn_minus_inst": f["intensity"] - i["intensity"],
        "frgn_pos_inst_neg": 1.0 if (f["streak"] >= 4 and i["intensity"] < 0) else 0.0,
        "intensity_frgn_trend": f["intensity"] if trend else 0.0,
        "tol_frgn_ge4_trend": 1.0 if (f["streak"] >= 4 and trend) else 0.0,
        # Q7 매도→매수 전환 (스크리너 rev 플래그) — 매도 스트릭 문턱 민감도
        "rev_frgn_s3": _rev(f, 3),
        "rev_frgn_s4": _rev(f, 4),      # 스크리너 실제 조건 (MIN_STREAK=4)
        "rev_frgn_s5": _rev(f, 5),
        "rev_inst_s3": _rev(i, 3),
        "rev_inst_s4": _rev(i, 4),
        "rev_inst_s5": _rev(i, 5),
        # 대조군
        "mom20": mom20 if mom20 is not None else 0.0,
    }
    if pen is not None:                 # Q8 연기금 (KRX 상세)
        out.update({
            "pension_intensity": pen["intensity"],
            "pension_persist": pen["persist"],
            "pension_tol_ge4": 1.0 if pen["streak"] >= 4 else 0.0,
            # 스크리너 P 등급 조건 그대로: 스트릭≥4 & 강도≥2%
            "pension_grade_P": 1.0 if (pen["streak"] >= 4 and pen["intensity"] >= 0.02) else 0.0,
            "rev_pension_s4": _rev(pen, 4),
        })
    if ixf is not None:                 # Q8 기관합계−금융투자 (KRX 상세)
        out.update({
            "instxf_intensity": ixf["intensity"],
            "instxf_persist": ixf["persist"],
            "instxf_tol_ge4": 1.0 if ixf["streak"] >= 4 else 0.0,
            "frgn_minus_instxf": f["intensity"] - ixf["intensity"],
            "rev_instxf_s4": _rev(ixf, 4),
        })
    return out


def _mom20(rows: list[dict]) -> float | None:
    if len(rows) < 21:
        return None
    p0, p1 = rows[-21]["close"], rows[-1]["close"]
    return (p1 / p0 - 1) if p0 else None


def _ma20(rows: list[dict]) -> float | None:
    if len(rows) < 20:
        return None
    return statistics.fmean(r["close"] for r in rows[-20:])


# ── 평가 ────────────────────────────────────────────────
def evaluate(cache: fh.FlowCache, codes: list[str], window: int) -> dict:
    # 공통 거래일 축 — 가장 많이 거래된 종목의 날짜열을 기준으로 삼는다
    base = max((cache.data.get(c, []) for c in codes), key=len, default=[])
    dates = [r["date"] for r in base]
    if len(dates) < window + FWD_TD + STEP_TD:
        raise SystemExit(f"이력 부족: {len(dates)}거래일 — --warm 으로 --pages 를 늘려라")

    per_date: dict[str, list[float]] = {}
    bins: dict[str, list[tuple[float, float]]] = {}     # 신호 → [(값, fwd)]
    split: dict[str, list[float]] = {"trend": [], "contra": []}
    split_sig: dict[str, dict[str, list[tuple[float, float]]]] = {"trend": {}, "contra": {}}
    n_obs = 0

    for di in range(window - 1, len(dates) - FWD_TD, STEP_TD):
        t, t_fwd = dates[di], dates[di + FWD_TD]
        # 신호 → [(값, 수익률, 레짐)]. KRX 상세 신호는 병합된 종목에만 있으므로
        # 인덱스 정렬 가정을 버리고 튜플로 묶는다.
        sig_vals: dict[str, list[tuple[float, float, str]]] = {}
        rets: list[float] = []
        regimes: list[str] = []
        for code in codes:
            rows = cache.rows(code, t)
            if len(rows) < max(window, 21) or rows[-1]["date"] != t:
                continue
            fwd_rows = [r for r in cache.data.get(code, []) if r["date"] == t_fwd]
            if not fwd_rows:
                continue
            p0, p1 = rows[-1]["close"], fwd_rows[0]["close"]
            if not p0 or not p1:
                continue
            ret = max(-WINSOR, min(WINSOR, p1 / p0 - 1))
            m = fh.metrics(rows, window)
            if m is None:
                continue
            ma20 = _ma20(rows)
            reg = "trend" if (ma20 and p0 >= ma20) else "contra"
            has_det = all("pension" in r for r in rows[-window:])
            s = signals(m, _mom20(rows), trend=reg == "trend",
                        pen=fh.side_metrics(rows, "pension", window) if has_det else None,
                        ixf=fh.side_metrics(rows, "inst_xf", window) if has_det else None)
            for k, v in s.items():
                sig_vals.setdefault(k, []).append((v, ret, reg))
            rets.append(ret)
            regimes.append(reg)
        if len(rets) < 20:
            continue
        n_obs += len(rets)
        mkt = statistics.fmean(rets)
        for k, triples in sig_vals.items():
            vals = [v for v, _, _ in triples]
            krets = [r for _, r, _ in triples]
            ic = spearman(vals, krets)
            if ic is not None:
                per_date.setdefault(k, []).append(ic)
            # 이산 신호: 초과수익 집계 / 연속 신호: 상위 20% 초과수익
            hits = [r - mkt for v, r in zip(vals, krets) if v > 0] if set(vals) <= {0.0, 1.0} \
                else _top_quintile(vals, krets, mkt)
            bins.setdefault(k, []).extend((1.0, h) for h in hits)
            for reg in ("trend", "contra"):
                sub = [(v, r - mkt) for v, r, g in triples if g == reg]
                if sub:
                    split_sig[reg].setdefault(k, []).extend(sub)
        for r, g in zip(rets, regimes):
            split[g].append(r - mkt)

    return {"per_date": per_date, "bins": bins, "split": split,
            "split_sig": split_sig, "n_obs": n_obs, "n_dates": len(next(iter(per_date.values()), []))}


def _top_quintile(vals: list[float], rets: list[float], mkt: float) -> list[float]:
    k = max(1, len(vals) // 5)
    idx = sorted(range(len(vals)), key=lambda i: vals[i], reverse=True)[:k]
    return [rets[i] - mkt for i in idx]


def report(res: dict, window: int) -> str:
    lines = [
        f"# 수급 감지기 백테스트 (window={window}거래일, fwd={FWD_TD}거래일, "
        f"신호일 {res['n_dates']}회, 관측 {res['n_obs']:,}건)",
        "",
        "IC = 횡단면 스피어만 상관(신호 vs 20거래일 수익률)의 신호일 평균. |t|>2 면 통계적으로 유의.",
        "초과수익 = 신호 종목(연속형은 상위 20%)의 시장평균 대비 20거래일 초과수익.",
        "rev·pension 같은 희소 이벤트는 IC가 아니라 초과수익%·초과t·건수로 판단할 것.",
        "",
        f"{'신호':<24}{'평균IC':>9}{'t값':>8}{'초과수익%':>11}{'초과t':>8}{'건수':>9}",
        "-" * 69,
    ]
    rows = []
    for k, ics in res["per_date"].items():
        ex = [h for _, h in res["bins"].get(k, [])]
        rows.append((k, statistics.fmean(ics), tstat(ics),
                     statistics.fmean(ex) * 100 if ex else 0.0, tstat(ex), len(ex)))
    for k, ic, t, ex, ext, n in sorted(rows, key=lambda r: -abs(r[1])):
        lines.append(f"{k:<24}{ic:>9.4f}{t:>8.2f}{ex:>11.2f}{ext:>8.2f}{n:>9,}")

    lines += ["", "## 추세형(종가≥MA20) vs 역발상형(종가<MA20) — 초과수익%", "",
              f"{'신호':<24}{'추세형':>10}{'역발상형':>11}"]
    lines.append("-" * 45)
    for k in res["per_date"]:
        cells = []
        for reg in ("trend", "contra"):
            sub = res["split_sig"][reg].get(k, [])
            binary = set(v for v, _ in sub) <= {0.0, 1.0}
            hits = [h for v, h in sub if v > 0] if binary else \
                _top_quintile([v for v, _ in sub], [h for _, h in sub], 0.0)
            cells.append(statistics.fmean(hits) * 100 if hits else 0.0)
        lines.append(f"{k:<24}{cells[0]:>10.2f}{cells[1]:>11.2f}")
    return "\n".join(lines)


DETAIL_CACHE = CACHE_DIR / "flow_detail.json"


def warm_detail(cache: fh.FlowCache, codes: list[str]) -> int:
    """KRX 상세(금융투자·연기금) 이력을 네이버 캐시 기간만큼 수집해 rows에 병합.
    detail_available() 실패(무자격 환경)면 0 — 상세 신호만 빠지고 나머지는 그대로.
    반환: 병합된 종목 수."""
    if not fh.detail_available():
        return 0
    det: dict[str, dict] = {}
    if DETAIL_CACHE.exists():
        try:
            det = json.loads(DETAIL_CACHE.read_text(encoding="utf-8"))
        except Exception as e:                      # noqa: BLE001
            logger.warning("상세 캐시 무시: %s", e)
    todo = [c for c in codes if c not in det and cache.data.get(c)]
    if todo:
        logger.info("KRX 상세 수집 %d종목 (순차 — 차단 회피)", len(todo))
    for n, c in enumerate(todo, 1):
        rows = cache.data[c]
        det[c] = fh.fetch_detail(c, rows[0]["date"], rows[-1]["date"]) or {}
        if n % 25 == 0:
            logger.info("  %d/%d", n, len(todo))
            DETAIL_CACHE.write_text(json.dumps(det), encoding="utf-8")
        time.sleep(0.3)
    if todo:
        DETAIL_CACHE.write_text(json.dumps(det), encoding="utf-8")
    merged = 0
    for c in codes:
        hit = False
        for r in cache.data.get(c, []):
            x = det.get(c, {}).get(r["date"])
            if x:
                r["inst_xf"] = r["inst"] - x["fin"]
                r["pension"] = x["pension"]
                hit = True
        merged += hit
    return merged


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warm", action="store_true", help="수급 이력 수집만")
    ap.add_argument("--limit", type=int, default=300, help="유니버스 종목 수(시총순)")
    ap.add_argument("--pages", type=int, default=26, help="네이버 페이지 수(×20거래일)")
    ap.add_argument("--window", type=int, default=fh.WINDOW)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    codes = list(fh.universe(a.limit, UNIV_CACHE))
    cache = fh.FlowCache(FLOW_CACHE)
    cache.warm(codes, a.pages)
    have = [c for c in codes if len(cache.data.get(c, [])) >= a.window + FWD_TD]
    logger.info("이력 확보 %d/%d 종목", len(have), len(codes))
    n_det = warm_detail(cache, have)
    logger.info("KRX 상세 병합 %d종목", n_det)
    if a.warm:
        return
    res = evaluate(cache, have, a.window)
    text = report(res, a.window)
    OUT_PATH.write_text(text, encoding="utf-8")   # 출력보다 먼저 — cp949 콘솔에서 print가 죽는다
    logger.info("리포트 저장 %s", OUT_PATH)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)


if __name__ == "__main__":
    main()
