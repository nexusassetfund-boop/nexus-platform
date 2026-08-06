"""CANSLIM 백테스트 결과(tmp/cbt_*.json) → reports/backtest_canslim.md.

판정 대상은 **사전 지정한 loose5 하나**다. grid에서 최고 CAGR을 골라 판정하면
25개 변형에 대한 다중검정이 되어 과최적화다 — 그래서 고르지 않는다.

실행: python scanner/canslim_backtest_report.py [--tag ""]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backtest_report import run_eval   # evaluate_backtest.py(backtest-expert) 호출부 재사용

ROOT = Path(__file__).parent.parent
CBT_CACHE = ROOT / "tmp"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

NUM_PARAMS_CANSLIM = 7   # C / A_growth / A_roe / N / S1 / L / min_score

# 판정선 — grid를 보기 전에 고정 (계획 문서와 동일)
GATE_MIN_TRADES, GATE_MIN_HOLD, GATE_MAX_CASH = 60, 8, 40.0
LOOSE_EDGE, ABS_EDGE, VS_RS_EDGE, MDD_TOLER = 3.0, 3.0, 2.0, 5.0


def _load(name: str):
    p = CBT_CACHE / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _eval_compat(m: dict) -> dict:
    """value 계열 metrics → evaluate_backtest.py 입력 형태로 변환."""
    if not m:
        return {}
    return {
        "trades": m.get("closed_trades", 0),
        "win_rate": m.get("win_rate") or 0,
        "avg_win": m.get("avg_win") or 0,
        "avg_loss": m.get("avg_loss") or 0,
        "mdd_pct": m.get("mdd_pct") or 0,
        "yearly": m.get("yearly_pct", {}),
    }


_COLS = [("cagr_pct", "CAGR%"), ("excess_cagr_pct", "초과%p"), ("mdd_pct", "MDD%"),
         ("sharpe", "샤프"), ("win_rate", "승률%"), ("closed_trades", "거래"),
         ("avg_holdings", "평균보유"), ("avg_cash_pct", "현금%"),
         ("turnover_annual_pct", "회전율%")]


def _table(rows: dict, title: str) -> str:
    if not rows:
        return ""
    out = [f"### {title}", "",
           "| 변형 | " + " | ".join(c[1] for c in _COLS) + " |",
           "|---" * (len(_COLS) + 1) + "|"]
    for name, m in rows.items():
        cells = []
        for k, _ in _COLS:
            v = m.get(k)
            cells.append("-" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v)))
        out.append(f"| `{name}` | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def _gate0(m: dict) -> tuple[bool, str]:
    """표본 유효성 — 후보가 상시 0인 변형을 '무손실'로 읽지 않기 위한 관문."""
    if not m:
        return False, "결과 없음"
    bad = []
    if (m.get("closed_trades") or 0) < GATE_MIN_TRADES:
        bad.append(f"거래 {m.get('closed_trades')}건 < {GATE_MIN_TRADES}")
    if (m.get("avg_holdings") or 0) < GATE_MIN_HOLD:
        bad.append(f"평균보유 {m.get('avg_holdings')}종목 < {GATE_MIN_HOLD}")
    if (m.get("avg_cash_pct") or 0) > GATE_MAX_CASH:
        bad.append(f"현금 {m.get('avg_cash_pct')}% > {GATE_MAX_CASH}%")
    return (not bad), ("통과" if not bad else " · ".join(bad))


def verdict(controls: dict, grid: dict | None) -> tuple[str, list[str]]:
    """사전 고정 기준으로 세 결론 중 하나를 낸다."""
    o5, l5, rs = controls.get("orig5"), controls.get("loose5"), controls.get("rs_only")
    lines = []
    if not (o5 and l5 and rs):
        return "판정 불가", ["orig5 / loose5 / rs_only 중 결과 누락"]

    ok, why = _gate0(l5)
    lines.append(f"- 게이트 0(loose5 표본 유효성): **{why}**")
    if not ok:
        return "판정 불가 (표본 부족)", lines

    e_l5, e_o5, e_rs = l5["excess_cagr_pct"], o5["excess_cagr_pct"], rs["excess_cagr_pct"]
    d_loose = e_l5 - e_o5
    d_rs = e_l5 - e_rs
    mdd_gap = abs(l5["mdd_pct"]) - abs(rs["mdd_pct"])
    slip = (grid or {}).get("slip_x2", {}).get("excess_cagr_pct")

    lines += [
        f"- 완화 순효과: loose5 − orig5 = **{d_loose:+.2f}%p** (기준 ≥ +{LOOSE_EDGE})",
        f"- loose5 절대 초과수익: **{e_l5:+.2f}%p** (기준 ≥ +{ABS_EDGE})",
        f"- rs_only 대비: **{d_rs:+.2f}%p** (기준 ≥ +{VS_RS_EDGE})",
        f"- MDD 악화(vs rs_only): **{mdd_gap:+.1f}%p** (허용 ≤ +{MDD_TOLER})",
        f"- 슬리피지 2배 초과수익: **{slip if slip is not None else '미측정'}**",
    ]

    best = max((v.get("excess_cagr_pct", -1e9) for v in
                list(controls.values()) + list((grid or {}).values())), default=-1e9)
    if best <= e_rs or best < 0:
        return "CANSLIM 자체가 알파 없음", lines + [
            f"- 모든 변형 최고 초과수익 **{best:+.2f}%p** ≤ rs_only({e_rs:+.2f}%p) 또는 음수",
            "  → 임계치 논쟁이 아니라 요건 구성 자체가 국내에서 RS 대비 부가가치 없음"]

    if (d_loose >= LOOSE_EDGE and e_l5 >= ABS_EDGE and d_rs >= VS_RS_EDGE
            and mdd_gap <= MDD_TOLER and (slip is None or slip > 0)):
        return "완화가 유효", lines
    if -1.0 <= d_loose < LOOSE_EDGE:
        return "완화 무효 (원전 유지)", lines + [
            "  → 완화는 후보 수만 늘리고 수익률은 노이즈. 후보가 필요하면 min_score 컷만 낮춰라."]
    return "완화 무효 (완화가 오히려 열위)", lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    controls = _load(f"cbt_controls{args.tag}.json") or {}
    grid = _load(f"cbt_grid{args.tag}.json")
    base = _load(f"cbt_result{args.tag}.json") or {}
    bits = base.get("bit_log") or _load(f"cbt_bits{args.tag}.json") or []

    v, reasons = verdict(controls, grid)
    ev = run_eval({**_eval_compat(controls.get("loose5", {})),
                   "num_parameters": NUM_PARAMS_CANSLIM})

    md = [f"# CANSLIM 임계치 완화 검증{' — ' + args.tag if args.tag else ''}", "",
          f"**판정: {v}**", "",
          "질문: 오닐 원전 임계치(ROE 17% 등)를 국내시장에 맞게 완화하는 것이 더 유효한가.",
          "판정 대상은 사전 지정한 `loose5` 하나다 — grid 최고값을 고르면 다중검정이 되어 과최적화다.",
          ""]
    md += reasons + [""]

    if bits:
        md += ["## 요건별 통과율 (신호일별)", "",
               "ROE 병목이 최근 현상인지 5년 내내인지 — 질문의 절반은 여기서 답한다.", "",
               "| 신호일 | 대상 | " + " | ".join(bits[0]["pass_rate_pct"].keys()) + " |",
               "|---" * (len(bits[0]["pass_rate_pct"]) + 2) + "|"]
        for b in bits:
            md.append(f"| {b['sig']} | {b['n']} | "
                      + " | ".join(f"{v:g}" for v in b["pass_rate_pct"].values()) + " |")
        md.append("")

    md += [_table(controls, "대조군"), _table(grid or {}, "OFAT · 강건성")]

    if ev:
        md += ["### backtest-expert 판정 (loose5)", "",
               f"- 총점 **{ev.get('total_score', '-')}/100** → **{ev.get('recommendation', '-')}**",
               f"- 파라미터 {NUM_PARAMS_CANSLIM}개 × 60개월은 과최적화 경보 구간이라 "
               "Robustness 감점은 정상이다.", ""]

    md += ["## 한계 (해석 시 반드시 감안)", "",
           f"- **생존편향**: `_corp_map()`이 현 상장사만 매핑 → 상폐 종목 재무 결측 → 미충족 처리. "
           f"편향은 상방. corp_code 결측 {base.get('no_corp', '-')}종목, 시세 결측 "
           f"{base.get('missing_prices', '-')}종목.",
           "- **거래대금 미고려**: 소형주 편입 시 실제 체결 슬리피지는 모형보다 나쁠 수 있다. "
           "`slip_x2`가 그 대용.",
           "- **RS 재현**: 라이브 `_pct_rank_map`과 2,193종목 불일치 0건으로 검증했으나 "
           "스냅샷 소스(KRX 덤프 vs pykrx)가 달라 미세 오차 가능.",
           "- **I(수급) 미포함**: 라이브 점수에도 미반영.",
           "- 산출물은 연구용이다. `docs/data`로 나가지 않으며 프론트에 노출되지 않는다.",
           ""]

    out = REPORTS / f"backtest_canslim{args.tag}.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"판정: {v}")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
