"""
큐레이션 매핑 검증기 — 종목↔(시군구×HS) 추측이 데이터로 말이 되는지 확인한다.

이 도구가 이 기능에서 실질적으로 가장 중요하다. 매핑은 한 번에 맞출 수 없고, 여기서
점유율과 순위를 보며 HS나 지역을 고쳐 다시 돌리는 반복이 곧 큐레이션 작업이다.

판정 기준: 점유율(시도 내 비중)과 **균등 몫 대비 배수**를 함께 본다.
점유율만 쓰면 시군구가 31개인 경기도 기업이 구조적으로 불리해 전부 탈락한다 —
코스맥스 화성 14%는 균등 대비 4.4배로 충분히 몰려 있는데도 예전 기준에선 버려졌다.

  1위  且 (점유 50%+ 또는 배수 5+)      high — 사실상 단독
  1~3위 且 (점유 20%+ 또는 배수 2.5+)   mid  — 타사 혼재 가능
  그 외                              low  — 매핑을 다시 잡는다
  데이터 없음                          실패 — 지역·HS 재확인

자동으로 버리지 않는다. 표를 출력하고 사람이 stock_trade_map.json을 고친다.

실행: CUSTOMS_API_KEY=... python -X utf8 scanner/verify_trade_map.py
      특정 종목만: ... verify_trade_map.py 214450 145020
출력: scanner/data/trade_map.json (검증 통과분 — trade_stats.py가 읽는 산출물)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import customs_api as capi

logger = logging.getLogger("verify")

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
SRC_PATH = Path(__file__).parent / "data" / "stock_trade_map.json"
OUT_PATH = Path(__file__).parent / "data" / "trade_map.json"
CACHE_PATH = ROOT / "docs" / "data" / "trade_raw_cache.json"

# 시군구 API 금액 단위는 **천USD** (품목별 API는 USD — 서로 다르다).
MIN_MONTHLY = 100.0       # 월 수출 하한 (천USD = $100k)
MONTHS = 6                # 검증용 최근 개월 (본 수집은 trade_stats가 60개월로 한다)


def _load(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("%s 로드 실패: %s", path.name, e)
        return default


def _match_sgg(hint: str, available: list[str]) -> str | None:
    """'강릉' 같은 힌트를 응답의 '강원특별자치도 강릉시'에 맞춘다."""
    if not hint:
        return None
    for a in available:
        if hint in a:
            return a
    return None


def grade(share: float, rank: int, n_sgg: int) -> str:
    """귀속 신뢰도. 점유율만 보면 큰 시도에 있는 기업이 부당하게 탈락한다.

    경기도는 시군구가 31개라 하나가 30%를 넘기기 구조적으로 어렵고, 강원 춘천은
    해당 품목 수출 시군구가 3개뿐이라 쉽게 100%가 나온다. 그래서 점유율과 함께
    **균등 몫 대비 배수**(share × 시군구수 / 100)를 본다 — 몰려 있는 정도를
    시도 크기와 무관하게 재는 지표다.

    실측 예: 씨젠 송파 61%(15.3배) · 에이피알 강남 58%(14.5) · 파마리서치 강릉 51%(9.1) ·
             티씨케이 안성 72%(5.7) · 휴젤 춘천 94%(4.7) · 코스맥스 화성 14%(4.4).
    반례: 이오테크닉스 안양 17%는 배수 1.3에 그친다 — 레이저 가공기를 수출하는
    시군구가 8개뿐이라 17%는 균등 수준이고, 몰려 있다고 볼 수 없다.
    """
    conc = share * max(1, n_sgg) / 100.0
    if rank == 1 and (share >= 50 or conc >= 5):
        return "high"
    if rank <= 3 and (share >= 20 or conc >= 2.5):
        return "mid"
    return "low"


def verify_entry(e: dict, month: str, api_key: str) -> dict:
    """한 항목을 검증해 결과 dict 반환. 실패해도 예외를 올리지 않는다."""
    out = {**e, "ok": False, "reason": None}
    sido, hint = e.get("sido"), e.get("sgg_hint")
    hs_list = e.get("hs") or []
    if not (sido and hint and hs_list):
        out["reason"] = "필드 누락"
        return out

    best = None
    for hs in hs_list:
        try:
            rows = []
            for s, en in capi.month_range(month, MONTHS):
                rows.extend(capi.fetch_district(hs, sido, s, en, api_key, CACHE_PATH))
        except capi.CustomsError as ex:
            out["reason"] = f"조회 실패: {str(ex)[:50]}"
            continue
        if not rows:
            continue

        # 최신 월 기준 시도 내 순위·점유율
        tot: dict[str, float] = {}
        for r in rows:
            if "".join(c for c in str(r.get("period", "")) if c.isdigit())[:6] != month:
                continue
            tot[r.get("sgg") or ""] = tot.get(r.get("sgg") or "", 0.0) + (r.get("exp_amt") or 0.0)
        if not tot:
            continue
        ranked = sorted(tot.items(), key=lambda x: -x[1])
        total = sum(v for _, v in ranked) or 1.0
        sgg = _match_sgg(hint, [n for n, _ in ranked])
        if not sgg:
            if best is None:
                out["reason"] = f"'{hint}' 지역이 응답에 없음 (상위: " + \
                    ", ".join(n.split()[-1] for n, _ in ranked[:3]) + ")"
            continue

        amt = tot[sgg]
        rank = [n for n, _ in ranked].index(sgg) + 1
        share = amt / total * 100
        cand = {"hs_used": hs, "sgg": sgg, "amount": round(amt, 1),
                "rank": rank, "of": len(ranked), "share": round(share, 1),
                "grade": grade(share, rank, len(ranked)), "conc": round(share * len(ranked) / 100.0, 1)}
        if amt < MIN_MONTHLY:
            cand["grade"] = "low"
            cand["small"] = True
        # 여러 HS 후보 중 더 잘 몰려 있는 쪽을 채택 — 등급이 같으면 배수로 가린다.
        _ord = {"high": 2, "mid": 1, "low": 0}
        if best is None or (_ord[cand["grade"]], cand["conc"]) > (_ord[best["grade"]], best["conc"]):
            best = cand

    if best is None:
        out["reason"] = out["reason"] or "데이터 없음"
        return out
    out.update(best)
    out["ok"] = True
    return out


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    api_key = capi.require_key()
    src = _load(SRC_PATH)
    if not src or not src.get("entries"):
        print(f"매핑 원본 없음: {SRC_PATH}")
        sys.exit(1)

    only = {a for a in sys.argv[1:] if a.isdigit()}
    entries = [e for e in src["entries"] if not only or e.get("ticker") in only]

    today = dt.datetime.now(tz=KST).date()
    back = 1 if today.day >= 16 else 2
    t = today.year * 12 + (today.month - 1) - back
    month = f"{t // 12:04d}{t % 12 + 1:02d}"

    print(f"기준월 {month} · 대상 {len(entries)}개\n")
    print(f"{'종목':<20}{'지역':<10}{'HS':<8}{'수출(천$)':>11}{'순위':>7}{'점유':>7}{'배수':>7}  판정")
    print("-" * 84)

    results = []
    for e in entries:
        r = verify_entry(e, month, api_key)
        results.append(r)
        if r["ok"]:
            mark = {"high": "OK ", "mid": "△  ", "low": "✗  "}[r["grade"]]
            print(f"{r['name'][:19]:<20}{r['sgg'].split()[-1][:9]:<10}{r['hs_used']:<8}"
                  f"{r['amount']:>11,.0f}{r['rank']}/{r['of']:>5}{r['share']:>6.0f}%{r['conc']:>6.1f}x  {mark}"
                  + ("(소액)" if r.get("small") else ""))
        else:
            print(f"{r['name'][:19]:<20}{'-':<10}{'-':<8}{'-':>11}{'-':>7}{'-':>7}{'-':>7}  ✗  {r['reason']}")

    ok = [r for r in results if r["ok"]]
    by = {g: len([r for r in ok if r["grade"] == g]) for g in ("high", "mid", "low")}
    print("-" * 84)
    print(f"통과 {len(ok)}/{len(results)} — high {by['high']} · mid {by['mid']} · low {by['low']}")
    print("\nhigh/mid만 화면에 올립니다. low는 stock_trade_map.json에서 HS나 지역을 고쳐 재실행하세요.")

    # ── 산출물: high/mid만 저장
    adopted = [r for r in ok if r["grade"] in ("high", "mid")]
    # 같은 (시군구, HS)에 여러 종목이 걸리면 귀속 불확실
    combo: dict[tuple, list[str]] = {}
    for r in adopted:
        combo.setdefault((r["sgg"], r["hs_used"]), []).append(r["ticker"])
    for key, users in combo.items():
        if len(users) > 1:
            for r in adopted:
                if (r["sgg"], r["hs_used"]) == key:
                    r["shared"] = True

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "base_month": month,
        "method": "종목별 공장 소재 시군구 × HS6 수출액을 매출 프록시로 사용. "
                  "시도 내 점유율·순위로 귀속 신뢰도를 표시한다.",
        "entries": [{k: v for k, v in r.items() if k not in ("ok", "reason", "seed_confidence")}
                    for r in adopted],
        "coverage": {"adopted": len(adopted), "checked": len(results), **by},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {OUT_PATH} ({len(adopted)}개)")


if __name__ == "__main__":
    main()
