# -*- coding: utf-8 -*-
"""review_output.json 검증 → docs/data/weekly_review.json 병합 → Worker KV 게시."""
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "docs" / "data" / "weekly_review.json"
WORKER = "https://nexus-platform.nexusassetfund.workers.dev"
KEEP = 30  # 최근 30주 보관


def main():
    inp = ROOT / "review_input.json"
    if inp.exists() and json.loads(inp.read_text("utf-8")).get("skip"):
        print("휴장 주간 — 게시 건너뜀")
        return
    out = ROOT / "review_output.json"
    if not out.exists():
        print("review_output.json 없음 — 생성 실패")
        sys.exit(1)
    r = json.loads(out.read_text("utf-8"))
    assert str(r.get("week", "")).strip(), "week 비어있음"
    assert str(r.get("title", "")).strip(), "title 비어있음"
    assert isinstance(r.get("sections"), list) and r["sections"], "sections 비어있음"
    r["date"] = dt.datetime.now(tz=KST).date().isoformat()

    store = {"updated": None, "reports": []}
    if STORE.exists():
        try:
            store = json.loads(STORE.read_text("utf-8"))
        except Exception:
            pass
    reports = [x for x in store.get("reports", []) if x.get("week") != r["week"]]
    reports.insert(0, r)  # 같은 주 재실행 시 교체, 최신이 앞
    store = {"updated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
             "reports": reports[:KEEP]}
    STORE.write_text(json.dumps(store, ensure_ascii=False, indent=1), "utf-8")
    print(f"weekly_review.json 저장 — {r['week']} (보관 {len(store['reports'])}건)")

    token = os.environ.get("NEXUS_ADMIN_TOKEN", "").strip()
    if not token:
        print("NEXUS_ADMIN_TOKEN 없음 — KV 게시 생략 (파일만 저장)")
        return
    resp = requests.post(
        f"{WORKER}/api/push",
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        data=json.dumps({"files": {"weekly_review.json": store}}, ensure_ascii=False).encode("utf-8"),
        timeout=30)
    print(f"POST /api/push -> {resp.status_code} {resp.text[:200]}")
    resp.raise_for_status()


if __name__ == "__main__":
    main()
