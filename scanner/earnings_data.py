# -*- coding: utf-8 -*-
"""실적 분석 입력 데이터 수집 — 확정 숫자는 코드가 만들고, 해설만 Claude가 쓴다.

Worker의 earnings_calendar.json에서 대상 이벤트(code+period)를 추출해
earnings_input.json (repo 루트, gitignore 대상)으로 저장한다.
이벤트가 없으면 명확한 로그와 함께 exit 1 (fail-closed).

사용: python scanner/earnings_data.py --code 005930 --period 2026Q2 [--name 삼성전자]
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
WORKER = "https://nexus-platform.nexusassetfund.workers.dev"
OUT = ROOT / "earnings_input.json"


def fetch_event(code, period):
    """earnings_calendar.json에서 code+period 일치 이벤트 — 없으면 None."""
    r = requests.get(f"{WORKER}/data/earnings_calendar.json", timeout=30)
    r.raise_for_status()
    cal = r.json()
    for ev in cal.get("events") or []:
        if ev.get("code") == code and ev.get("period") == period:
            return ev, cal.get("updated")
    return None, cal.get("updated")


def recent_price(code):
    """보조 컨텍스트 — 로컬 docs/data에서 최근 주가를 싸게 찾는다 (없으면 None, 실패해도 무해)."""
    try:
        vp = json.loads((ROOT / "docs" / "data" / "value_price.json").read_text("utf-8"))
        items = vp.get("items") or vp.get("prices") or []
        if isinstance(items, dict):
            rec = items.get(code)
            if rec:
                return {"source": "value_price.json", "data": rec}
        else:
            for rec in items:
                if isinstance(rec, dict) and str(rec.get("code", "")).strip() == code:
                    return {"source": "value_price.json", "data": rec}
    except Exception:
        pass
    try:
        scan = json.loads((ROOT / "docs" / "data" / "scan.json").read_text("utf-8"))
        for rec in scan.get("results") or []:
            if str(rec.get("code", "")).strip() == code:
                return {"source": "scan.json",
                        "data": {k: rec.get(k) for k in ("name", "price", "change_pct", "stage_label")
                                 if rec.get(k) is not None}}
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="6자리 종목코드")
    ap.add_argument("--period", required=True, help="실적 기간 (YYYYQn, 예: 2026Q2)")
    ap.add_argument("--name", default="", help="종목명 (선택 — 이벤트에 있으면 이벤트 우선)")
    args = ap.parse_args()

    code = args.code.strip()
    period = args.period.strip()
    if not re.fullmatch(r"\d{6}", code):
        print(f"code 형식 오류: {code!r} (6자리 숫자여야 함)")
        sys.exit(1)
    if not re.fullmatch(r"\d{4}Q[1-4]", period):
        print(f"period 형식 오류: {period!r} (YYYYQn 이어야 함)")
        sys.exit(1)

    event, updated = fetch_event(code, period)
    if event is None:
        print(f"이벤트 없음: code={code} period={period} — earnings_calendar.json(updated={updated})에 "
              "해당 항목이 없어 분석을 중단합니다 (수집기 미실행 또는 잘못된 요청)")
        sys.exit(1)

    if args.name.strip() and not (event.get("name") or "").strip():
        event["name"] = args.name.strip()

    data = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "calendar_updated": updated,
        "event": event,
    }
    ctx = recent_price(code)
    if ctx:
        data["price_context"] = ctx

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"{code} {event.get('name') or args.name} {period} status={event.get('status')} -> {OUT}")


if __name__ == "__main__":
    main()
