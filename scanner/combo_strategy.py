# -*- coding: utf-8 -*-
"""조합 전략 — 신고가 후보로 거르고 퀄리티 점수로 고른다 (전략실 '신고가×퀄리티' 탭용).

검증: reports/backtest_combo.md (2021-07~2026-06, 59회 월간 리밸런싱)
  25가지 결합 방식 중 유일 생존. CAGR 34.36% vs 벤치(KS200) 26.83%,
  현금중립 초과 +7.79%p, 샤프 1.13, MDD -34.9%.
  월말 ±3거래일이 +7.65~+9.89%p 고원 — 퀄리티 데이터가 월말에 갱신되므로
  갱신 직후에 사야 신선한 순위로 산다. 주 1회·분기·돌파진입은 전부 열위.

매매 규칙 (제로 재량):
  1) 신고가 후보 명단에 최근 20거래일 내 한 번이라도 오른 종목을 후보군으로
  2) 그중 퀄리티 성장 풀(관문 통과 + composite 산출분)에 있는 종목만 남김
  3) composite 내림차순 상위 10종목 동일비중
  4) 월 첫 거래일 시가에 전량 교체, 한 달간 유지 (중도 청산 규칙 없음 — 백테스트 미검증)
  5) 월중 유입 자금은 명단을 바꾸지 않고 기존 10종목에 동일비중 편입

산출: docs/data/combo.json
  portfolio  — 이번 달 확정 포트폴리오(진입가·현재가·수익률·현재 상태)
  preview    — 지금 교체한다면 뽑힐 명단 (다음 월 첫 거래일에 반영)
  ledger     — 월별 실현 성과 (슬리피지 실측 기반 — 백테스트 대비 괴리 추적)
상태: docs/data/combo_state.json (확정 포트폴리오, append-only 원장)

주의: 백테스트는 월말 KRX 덤프(월 1회 갱신) 기준이고 라이브 퀄리티는 주 1회 갱신이라
      라이브 쪽 데이터가 더 신선하다. 더 잦은 교체가 유리한지는 미검증(§5-D 한계).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("combo")
KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "data"
QG_PATH = DATA / "quality_growth.json"
NHC_PATH = DATA / "newhigh_candidates.json"
OUT_PATH = DATA / "combo.json"
STATE_PATH = DATA / "combo_state.json"

TOP_N = 10          # 동일비중 종목 수 (백테스트 top10)
WINDOW_TD = 20      # 신고가 명단 창 — 검증된 값 (10/40 민감도에서 부호 안정)
LEDGER_MAX = 60     # 원장 보관 개월 수
HISTORY_MAX = 90    # 후보 풀 편입·편출 이력 보관 건수

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load(path: Path, what: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"{what} 로드 실패 {path}: {e}") from e


def window_members(nhc: dict, asof: str | None = None, window: int = WINDOW_TD) -> set[str]:
    """asof(포함) 기준 최근 window 거래일 안에 한 번이라도 신고가 명단에 오른 종목.

    daily(과거 일자별 명단) + candidates(오늘 명단)를 합쳐 창을 만든다. asof를 주면
    그날까지만 본다 — 월 첫 거래일에 '전월 말 기준' 신호를 재현할 때 쓴다.
    """
    daily = nhc.get("daily") or {}
    today = nhc.get("data_last_date")
    dates = sorted(daily)
    if today and today not in daily:
        dates.append(today)
    if asof:
        dates = [d for d in dates if d <= asof]
    if not dates:
        return set()
    codes: set[str] = set()
    for d in dates[-window:]:
        if d == today and today not in daily:
            codes |= {c["code"] for c in nhc.get("candidates", [])}
        else:
            codes |= {row[0] for row in daily.get(d, {}).get("items", [])}
    return codes


def select(nhc: dict, qg: dict, asof: str | None = None, top: int = TOP_N) -> list[dict]:
    """창 내 신고가 멤버 ∩ 퀄리티 풀 → composite 상위 top."""
    gate = window_members(nhc, asof)
    pool = {r["code"]: r for r in (qg.get("pool") or []) if r.get("composite") is not None}
    meta = nhc.get("meta") or {}
    rows = []
    for code in gate:
        q = pool.get(code)
        if not q:
            continue
        rows.append({
            "code": code,
            "name": q.get("name") or (meta.get(code) or {}).get("name") or code,
            "composite": q["composite"],
            "quality_z": q.get("quality_z"),
            "mom_z": q.get("mom_z"),
            "sector": (meta.get(code) or {}).get("sector", ""),
        })
    rows.sort(key=lambda r: -r["composite"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows[:top]


def _last_trading_day_of_prev_month(nhc: dict, today: str) -> str | None:
    """오늘(= 새 달 첫 거래일)의 직전 달 마지막 거래일 — 신호 기준일."""
    daily = nhc.get("daily") or {}
    prev = [d for d in sorted(daily) if d[:7] < today[:7]]
    return prev[-1] if prev else None


async def _prices(codes: list[str]) -> dict[str, dict]:
    """보유 종목의 최근 일봉 — 진입일 시가·현재가 산출용. 실패 종목은 제외."""
    if not codes:
        return {}
    import data_provider as dp
    out: dict[str, dict] = {}

    async def one(code: str):
        try:
            df = await dp.fetch_ohlcv(code, days=120)
            if df is None or df.empty:
                return
            df = df.rename(columns=str.lower)
            out[code] = {str(i.date()): {"open": float(r["open"]), "close": float(r["close"])}
                         for i, r in df.iterrows()}
        except Exception as e:
            logger.warning("시세 조회 실패 %s: %s", code, e)

    sem = asyncio.Semaphore(5)

    async def guarded(c):
        async with sem:
            await one(c)

    await asyncio.gather(*(guarded(c) for c in codes))
    return out


def _px(bars: dict, code: str, date: str, field: str):
    d = (bars.get(code) or {}).get(date)
    return d[field] if d else None


def _last_close(bars: dict, code: str, upto: str):
    days = sorted(d for d in (bars.get(code) or {}) if d <= upto)
    return bars[code][days[-1]]["close"] if days else None


def _latest_close(bars: dict, code: str):
    """가장 최근 종가 — 신고가 명단(asof)이 하루 늦어도 평가액은 최신이어야 한다.
    진입가는 진입일에 고정하고, 현재가만 최신으로 본다."""
    days = sorted(bars.get(code) or {})
    return (days[-1], bars[code][days[-1]]["close"]) if days else (None, None)


def build(nhc: dict, qg: dict, state: dict, bars: dict, now: dt.date | None = None,
          confirm_now: bool = False) -> tuple[dict, dict]:
    """(combo.json, combo_state.json) 산출. bars 없이도 명단은 나온다(가격만 빈다)."""
    today = nhc.get("data_last_date")
    if not today:
        raise RuntimeError("newhigh_candidates.json에 data_last_date 없음")
    month = today[:7]
    holdings = list(state.get("holdings") or [])
    ledger = list(state.get("ledger") or [])
    cur_month = state.get("month")
    rebalanced = False

    # ── 월 첫 거래일 = 교체일. 신호는 전월 마지막 거래일 기준, 체결은 오늘 시가 ──
    stale = ((now or dt.datetime.now(tz=KST).date()) - dt.date.fromisoformat(today)).days
    if confirm_now and not holdings:
        # 수동 확정(--confirm) — 검증 규칙(월 첫 거래일 진입)에서 벗어난 월중 진입이다.
        # 원장이 부분 월로 남으므로 forced 플래그로 표시해 나중에 오독하지 않게 한다.
        picks = select(nhc, qg)
        pdates = sorted({d for c in bars for d in bars[c]})
        entry_d = pdates[-1] if pdates else today
        holdings = [{
            "code": p["code"], "name": p["name"], "sector": p.get("sector", ""),
            "entry_date": entry_d,
            "entry_price": _px(bars, p["code"], entry_d, "open") or _last_close(bars, p["code"], entry_d),
            "composite_at_entry": p["composite"], "rank_at_entry": p["rank"], "forced_entry": True,
        } for p in picks]
        cur_month = month
        rebalanced = bool(holdings)
        logger.info("수동 확정 — %s %d종목, 진입일 %s (월중 진입: 검증 규칙 밖)",
                    month, len(holdings), entry_d)
    elif cur_month != month and not cur_month:
        # 첫 실행 — 월 중간에 들어가면 검증된 규칙(월 첫 거래일 진입)과 어긋나고 원장도 더러워진다.
        # 기준월만 잡아두고 명단은 미리보기로만 보여준다. 확정은 다음 달 첫 거래일에.
        cur_month = month
        logger.info("첫 실행 — 기준월 %s만 기록, 확정 진입은 다음 달 첫 거래일", month)
    elif cur_month != month and stale > 5:
        logger.warning("신고가 데이터가 %d일 지연(asof %s) — 교체 보류", stale, today)
    elif cur_month != month:
        sig_date = _last_trading_day_of_prev_month(nhc, today)
        picks = select(nhc, qg, asof=sig_date)
        if picks:
            # 직전 달 보유분 청산 기록 (같은 날 시가)
            closed = []
            for h in holdings:
                exit_px = _px(bars, h["code"], today, "open") or _last_close(bars, h["code"], today)
                ret = (round((exit_px / h["entry_price"] - 1) * 100, 2)
                       if exit_px and h.get("entry_price") else None)
                closed.append({**h, "exit_date": today, "exit_price": exit_px, "ret_pct": ret})
            if closed:
                rets = [c["ret_pct"] for c in closed if c["ret_pct"] is not None]
                ledger.append({
                    "month": cur_month, "entry_date": closed[0]["entry_date"], "exit_date": today,
                    "n": len(closed), "avg_ret_pct": round(sum(rets) / len(rets), 2) if rets else None,
                    "forced_entry": any(c.get("forced_entry") for c in closed),
                    "win_rate": (round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1)
                                 if rets else None),
                    "added": [{"code": p["code"], "name": p["name"]} for p in picks
                              if p["code"] not in {c["code"] for c in closed}],
                    "removed": [{"code": c["code"], "name": c["name"]} for c in closed
                                if c["code"] not in {p["code"] for p in picks}],
                    "holdings": closed,
                })
                ledger = ledger[-LEDGER_MAX:]
            holdings = [{
                "code": p["code"], "name": p["name"], "sector": p.get("sector", ""),
                "entry_date": today,
                "entry_price": _px(bars, p["code"], today, "open"),
                "composite_at_entry": p["composite"], "rank_at_entry": p["rank"],
            } for p in picks]
            cur_month = month
            rebalanced = True
            logger.info("교체 확정 %s → %d종목 (신호 기준일 %s)", month, len(holdings), sig_date or "부트스트랩")
        else:
            logger.warning("%s 선정 0종목 — 교체 보류, 기존 보유 유지", month)

    # ── 보유 종목 현황 (정보 제공용 — 매도 신호 아님) ──
    gate_now = window_members(nhc, None)
    pool_now = {r["code"]: r for r in (qg.get("pool") or []) if r.get("composite") is not None}
    rank_now = {r["code"]: i for i, r in enumerate(
        sorted(pool_now.values(), key=lambda r: -r["composite"]), 1)}
    live_pick = {p["code"]: p["rank"] for p in select(nhc, qg)}
    port, price_dates = [], []
    for h in holdings:
        pdate, last = _latest_close(bars, h["code"])
        if pdate:
            price_dates.append(pdate)
        port.append({
            **h,
            "price": last,
            "ret_pct": (round((last / h["entry_price"] - 1) * 100, 2)
                        if last and h.get("entry_price") else None),
            "days_held": (dt.date.fromisoformat(pdate or today)
                          - dt.date.fromisoformat(h["entry_date"])).days,
            "in_gate": h["code"] in gate_now,            # 아직 신고가 창 안인가
            "quality_rank": rank_now.get(h["code"]),     # 퀄리티 풀 내 현재 순위
            "in_current_pick": h["code"] in live_pick,   # 지금 뽑아도 들어오는가
        })

    # 후보 풀 전체 — 상위 10만 보여주면 11·12위가 안 보여 왜 안 뽑혔는지 알 수 없다
    eligible = select(nhc, qg, top=10 ** 9)
    held = {h["code"] for h in holdings}
    for r in eligible:
        r["is_held"] = r["code"] in held
        r["would_buy"] = r["rank"] <= TOP_N
    preview = eligible[:TOP_N]

    # 후보 풀 편입·편출 이력 (append-only) — 명단이 왜 바뀌었는지 추적
    history = list(state.get("history") or [])
    prev = dict(state.get("eligible") or [])
    cur = {r["code"]: r["name"] for r in eligible}
    if prev:   # 첫 실행에 전량 '편입'으로 찍히는 걸 막는다
        added = [{"code": c, "name": n} for c, n in cur.items() if c not in prev]
        removed = [{"code": c, "name": n} for c, n in prev.items() if c not in cur]
        if added or removed:
            if history and history[-1]["date"] == today:
                history[-1] = {"date": today, "added": added, "removed": removed}
            else:
                history.append({"date": today, "added": added, "removed": removed})
            history = history[-HISTORY_MAX:]
    closed_rets = [l["avg_ret_pct"] for l in ledger if l.get("avg_ret_pct") is not None]

    out = {
        "updated_at": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "updated": today,       # 홈 교차 카드가 읽는 하우스 키 (데이터 기준일)
        "asof": today,
        "month": cur_month,
        "rebalanced_today": rebalanced,
        "forced_entry": any(h.get("forced_entry") for h in holdings),
        "rule": {
            "gate": f"신고가 후보 명단 최근 {WINDOW_TD}거래일 창",
            "ranker": "퀄리티 성장 composite (0.5·퀄리티Z + 0.5·모멘텀Z)",
            "top": TOP_N,
            "rebalance": "월 첫 거래일 시가 전량 교체, 한 달 유지 (중도 청산 없음)",
            "inflow": "월중 유입 자금은 명단 변경 없이 기존 종목에 동일비중 편입",
            "backtest": ("2021-07~2026-06 CAGR 34.36% vs 벤치 26.83%, 현금중립 초과 +7.79%p, "
                         "샤프 1.13, MDD -34.9% (reports/backtest_combo.md)"),
            "caveat": "슬리피지 편도 0.5% 가정 — 1.0%면 초과수익 +1.66%p로 축소. 원장으로 실측 중",
        },
        "sources": {
            "newhigh_asof": nhc.get("data_last_date"),
            "price_asof": max(price_dates) if price_dates else None,
            "quality_base": qg.get("base_date"), "quality_updated": qg.get("updated"),
            "quality_pool_n": len(pool_now),
        },
        "counts": {"gate": len(gate_now), "pool": len(pool_now),
                   "eligible": len(set(gate_now) & set(pool_now))},
        "portfolio": port,
        "eligible": eligible,
        "preview": preview,
        "history": history,
        "ledger": ledger,
        "ledger_stats": {
            "months": len(closed_rets),
            "avg_month_pct": round(sum(closed_rets) / len(closed_rets), 2) if closed_rets else None,
            "win_months": sum(1 for r in closed_rets if r > 0),
        },
    }
    new_state = {"month": cur_month, "holdings": holdings, "ledger": ledger,
                 "eligible": [[r["code"], r["name"]] for r in eligible], "history": history}
    return out, new_state


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="지금 명단을 확정 포트폴리오로 잡는다 (월중 진입 — 검증 규칙 밖, forced 표시)")
    args = ap.parse_args()
    nhc = _load(NHC_PATH, "신고가 후보")
    qg = _load(QG_PATH, "퀄리티 성장")
    if not qg.get("pool"):
        logger.error("quality_growth.json에 pool 없음 — 퀄리티 스캐너를 먼저 갱신해야 한다. 중단")
        sys.exit(1)
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    # 보유 후보 + 이번 달 교체 대상 시세를 한 번에 (교체일엔 신규 종목 시가가 필요하다)
    codes = sorted({h["code"] for h in (state.get("holdings") or [])}
                   | {p["code"] for p in select(nhc, qg)})
    bars = asyncio.run(_prices(codes))
    out, new_state = build(nhc, qg, state, bars, confirm_now=args.confirm)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    STATE_PATH.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("저장: %s (보유 %d, 미리보기 %d, 원장 %d개월)",
                OUT_PATH, len(out["portfolio"]), len(out["preview"]), len(out["ledger"]))


if __name__ == "__main__":
    main()
