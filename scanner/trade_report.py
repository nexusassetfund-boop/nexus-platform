"""
수출 리포트 준비·게시 — Claude 생성 단계의 앞뒤를 맡는다.

  prepare : trade.json에서 리포트 재료를 추려 trade_report_input.json을 만든다.
            같은 기준월 리포트가 이미 있으면 skip 마커를 남겨 중복 생성을 막는다.
  publish : Claude가 쓴 trade_report_output.json을 검증해 docs/data/trade_report.json에
            누적하고(최근 12회) /api/push로 KV에 반영한다.

문체·구조·금지 규칙은 trade_report_prompt.md가 정의한다. 여기서는 스키마 검증과
"수치 창작 방지"의 마지막 방어선(픽 티커가 실제 데이터에 있는지)만 확인한다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("trade_report")

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
TRADE_PATH = ROOT / "docs" / "data" / "trade.json"
REPORT_PATH = ROOT / "docs" / "data" / "trade_report.json"
EARN_PATH = ROOT / "docs" / "data" / "earnings_calendar.json"
DISC_PATH = ROOT / "docs" / "data" / "trade_discover.json"
INPUT_PATH = ROOT / "trade_report_input.json"
OUTPUT_PATH = ROOT / "trade_report_output.json"
WORKER = "https://nexus-platform.nexusassetfund.workers.dev"
KEEP = 12


def _load(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("%s 로드 실패: %s", p.name, e)
        return default


def prepare() -> int:
    trade = _load(TRADE_PATH, None)
    if not trade or not trade.get("stocks"):
        logger.error("trade.json 없음/비어 있음 — 스캔이 먼저 돌아야 한다")
        return 1
    month = trade.get("data_month")

    reports = _load(REPORT_PATH, {"items": []})
    if any(r.get("month") == month for r in reports.get("items", [])):
        # 같은 확정치로 리포트를 두 번 쓰지 않는다.
        INPUT_PATH.write_text(json.dumps({"skip": True, "reason": f"{month} 리포트 이미 존재"},
                                         ensure_ascii=False), encoding="utf-8")
        logger.info("skip — %s 리포트가 이미 있다", month)
        return 0

    # 종목 상세는 리포트에 필요한 필드만 추린다(카드 52장 원본을 통째로 주지 않는다).
    slim = []
    for s in trade["stocks"]:
        slim.append({k: s.get(k) for k in (
            "ticker", "name", "label", "region", "grade", "share", "shared",
            "amount", "yoy", "mom", "streak", "q_sum", "q_sum_yoy", "q_progress",
            "ttm_yoy", "ath", "high_12m", "flags", "note")})

    # 발표 예정일 — 겹치는 종목만, 맥락 언급용.
    earn = []
    ec = _load(EARN_PATH, {})
    tickers = {s["ticker"] for s in slim}
    for ev in ec.get("events") or []:
        code = str(ev.get("code", "")).zfill(6)
        if code in tickers and ev.get("status") != "발표완료" and ev.get("date"):
            earn.append({"ticker": code, "name": ev.get("name"),
                         "period": ev.get("period"), "date": ev.get("date")})

    prev = (reports.get("items") or [None])
    prev_report = prev[0] if prev and prev[0] else None
    if prev_report:
        prev_report = {k: prev_report.get(k) for k in ("month", "title", "summary", "picks")}

    INPUT_PATH.write_text(json.dumps({
        "data_month": month,
        "macro": trade.get("macro"),
        "industries": (trade.get("report_input") or {}).get("industries"),
        "movers": (trade.get("report_input") or {}).get("movers"),
        "stocks": slim,
        "prev_report": prev_report,
        "earnings": earn,
        "imports": trade.get("imports"),
        "discover": (_load(DISC_PATH, {}) or {}).get("items", [])[:10],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("입력 준비 완료 — 기준월 %s, 종목 %d, 발표예정 %d", month, len(slim), len(earn))
    return 0


def publish() -> int:
    inp = _load(INPUT_PATH, {})
    if inp.get("skip"):
        logger.info("skip 마커 — 게시 생략")
        return 0
    out = _load(OUTPUT_PATH, None)
    if not out:
        logger.error("trade_report_output.json 없음 — 생성 실패")
        return 1
    for k in ("title", "summary", "body_md", "picks"):
        if not out.get(k):
            logger.error("출력 필드 누락: %s", k)
            return 1

    # 픽 티커가 실제 입력 데이터에 있는 종목인지 — 창작 방지의 마지막 방어선.
    known = {s["ticker"] for s in inp.get("stocks") or []}
    bad = [t for t in out["picks"] if t not in known]
    if bad:
        logger.error("입력에 없는 티커를 언급: %s — 게시 중단", bad)
        return 1

    month = inp.get("data_month")
    reports = _load(REPORT_PATH, {"items": []})
    items = [r for r in reports.get("items", []) if r.get("month") != month]
    items.insert(0, {
        "id": f"trade-{month}",
        "month": month,
        "title": out["title"], "summary": out["summary"],
        "body_md": out["body_md"], "picks": out["picks"],
        "created": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
    })
    doc = {"updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
           "items": items[:KEEP]}
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("저장: %s (%d회분, 최신 '%s')", REPORT_PATH, len(doc["items"]), out["title"])

    token = os.environ.get("NEXUS_ADMIN_TOKEN", "").strip()
    if not token:
        logger.warning("NEXUS_ADMIN_TOKEN 없음 — KV push 생략(다음 동기화 때 반영)")
        return 0
    req = urllib.request.Request(
        f"{WORKER}/api/push",
        data=json.dumps({"files": {"trade_report.json": doc}}, ensure_ascii=False).encode(),
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            logger.info("KV push: %s", r.read().decode()[:100])
    except Exception as e:
        logger.warning("KV push 실패(%s) — 다음 스캔 동기화 때 자동 반영", e)
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare":
        sys.exit(prepare())
    if cmd == "publish":
        sys.exit(publish())
    print("usage: trade_report.py prepare|publish")
    sys.exit(2)


if __name__ == "__main__":
    main()
