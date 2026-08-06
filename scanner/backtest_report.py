"""백테스트 결과(tmp의 bt_result_*.json, bt_grid.json) → reports/backtest_{s1,s3}.md 생성.

evaluate_backtest.py(backtest-expert 스킬)를 트랙별로 호출해 Deploy/Refine/Abandon 판정 포함.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CACHE_DIR = Path(os.environ.get("CLAUDE_JOB_DIR", str(ROOT))) / "tmp"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)
EVAL = Path(os.path.expanduser("~")) / ".claude" / "skills" / "backtest-expert" / "scripts" / "evaluate_backtest.py"

NUM_PARAMS = 8  # rs_min, conf컷, trail_stop, exit_ma, max_age, cooldown, max_track, max_daily


def _years(m: dict) -> int:
    ys = [int(y) for y in m.get("yearly", {})]
    return max(1, (max(ys) - min(ys) + 1)) if ys else 5


def run_eval(m: dict) -> dict:
    """evaluate_backtest.py 호출 → JSON 판정 반환"""
    if not m or not EVAL.exists():
        return {}
    m = {**m, "years": _years(m)}
    args = [
        sys.executable, str(EVAL),
        "--total-trades", str(m["trades"]),
        "--win-rate", str(m["win_rate"]),
        "--avg-win-pct", str(m["avg_win"]),
        "--avg-loss-pct", str(m["avg_loss"]),
        "--max-drawdown-pct", str(abs(m["mdd_pct"])),
        "--years-tested", str(m.get("years", 5)),
        "--num-parameters", str(NUM_PARAMS),
        "--slippage-tested",
        "--output-dir", str(CACHE_DIR / "eval"),
    ]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        # 스크립트는 JSON을 먼저 저장한 뒤 콘솔 print에서 cp949 크래시할 수 있음 → check 안 함
        subprocess.run(args, capture_output=True, text=True, env=env)
        evaldir = CACHE_DIR / "eval"
        jsons = sorted(evaldir.glob("backtest_eval_*.json"), key=lambda p: p.stat().st_mtime)
        if jsons:
            return json.loads(jsons[-1].read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)}
    return {}


def grid_table(grid: dict, track: str) -> str:
    rows = []
    rows.append("| 파라미터 | 거래 | 승률 | 기대값 | PF | 총수익% | MDD% | 평균보유 |")
    rows.append("|---|---|---|---|---|---|---|---|")
    for key, m in grid.items():
        if not key.startswith(track + "_") or not m or m.get("trades") is None:
            continue
        label = key[len(track) + 1:]
        rows.append(f"| {label} | {m['trades']} | {m['win_rate']} | {m['expectancy']} | "
                    f"{m['profit_factor']} | {m['total_return_pct']} | {m['mdd_pct']} | {m['avg_days']} |")
    return "\n".join(rows)


def report(track: str, title: str, m: dict, grid: dict, verdict: dict) -> str:
    if not m:
        return f"# {title}\n\n거래가 발생하지 않았습니다 (편입 신호 0건).\n"
    L = [f"# {title}", ""]
    ys = sorted(int(y) for y in m.get("yearly", {}))
    period = f"{ys[0]}~{ys[-1]} ({_years(m)}년)" if ys else ""
    L.append(f"검증 기간: {period} · 유니버스: KOSPI200+KOSDAQ150 (현재 구성원)")
    L.append("")
    L.append("## 핵심 지표")
    L.append("| 지표 | 값 |")
    L.append("|---|---|")
    L.append(f"| 총 거래 수 | {m['trades']} |")
    L.append(f"| 승률 | {m['win_rate']}% |")
    L.append(f"| 평균 손익 (기대값) | {m['expectancy']:+}% |")
    L.append(f"| 평균 수익 / 평균 손실 | +{m['avg_win']}% / -{m['avg_loss']}% |")
    L.append(f"| Profit Factor | {m['profit_factor']} |")
    L.append(f"| 평균 / 중앙값 보유일 | {m['avg_days']} / {m['med_days']} |")
    L.append(f"| 누적 수익 (10%씩 균등, 비복리) | {m['total_return_pct']}% |")
    L.append(f"| 최대 낙폭 (MDD) | {m['mdd_pct']}% |")
    L.append(f"| 같은 기간 KOSPI Buy&Hold | {m['kospi_bh_pct']}% |")
    L.append(f"| 주당 편입 건수(평균) | {m['entries_per_week']} |")
    L.append("")
    L.append("## 이탈 사유 분포")
    for r, n in m["exit_reasons"].items():
        L.append(f"- {r}: {n}건")
    L.append("")
    L.append("## 연도별 성과 (거래 수 / 손익합%)")
    for y, yr in sorted(m["yearly"].items()):
        L.append(f"- {y}: {yr['n']}건 / {yr['sum']:+}%")
    L.append("")
    L.append("## 강건성 그리드 (고원 vs 스파이크)")
    L.append(grid_table(grid, track))
    L.append("")
    if verdict and "total_score" in verdict:
        L.append("## backtest-expert 평가")
        L.append(f"- **판정: {verdict.get('verdict','?')}** (점수 {verdict.get('total_score','?')}/100)")
        for dim in verdict.get("dimensions", []):
            L.append(f"  - {dim.get('name')}: {dim.get('score')}/{dim.get('max_score')}")
        if verdict.get("red_flags"):
            L.append("- 레드플래그:")
            for rf in verdict["red_flags"]:
                L.append(f"  - [{rf.get('severity')}] {rf.get('message')}")
    L.append("")
    L.append("## 한계")
    L.append("- 현재 시점 지수 구성종목으로 과거 시뮬 → **생존 편향(수익 상방)**")
    L.append("- RS를 유니버스(~350종목) 내 백분위로 계산 (프로덕션은 전체 시장)")
    L.append("- rs_momentum·rs_new_high 보너스 미반영 → 신뢰도 보수적 (편입 약간 과소)")
    L.append("- 누적수익은 슬롯당 10% 고정·비복리 단순 합산 (실계좌 곡선 아님, 트랙 간 비교용)")
    return "\n".join(L)


def main():
    grid = {}
    gpath = CACHE_DIR / "bt_grid.json"
    if gpath.exists():
        grid = json.loads(gpath.read_text(encoding="utf-8"))
    summary = {}
    for track, title, fname in [
        ("S1", "S1 트랙 백테스트 — 초기 추세 편입 (stage1 & conf≥75)", "backtest_s1.md"),
        ("S3", "S3 트랙 백테스트 — 돌파 편입 (stage3 & conf≥70)", "backtest_s3.md"),
    ]:
        rpath = CACHE_DIR / f"bt_result_{track}.json"
        m = {}
        if rpath.exists():
            m = json.loads(rpath.read_text(encoding="utf-8")).get("metrics", {})
        verdict = run_eval(m) if m else {}
        (REPORTS / fname).write_text(report(track, title, m, grid, verdict), encoding="utf-8")
        summary[track] = {"metrics": m,
                          "verdict": verdict.get("verdict") if verdict else None,
                          "score": verdict.get("total_score") if verdict else None}
        print(f"{track}: {fname} 생성 (거래 {m.get('trades','-')}, 판정 {summary[track]['verdict']})")
    (CACHE_DIR / "bt_summary.json").write_text(json.dumps(summary, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
