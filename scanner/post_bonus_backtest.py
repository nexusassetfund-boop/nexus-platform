# -*- coding: utf-8 -*-
"""docs/data/bonus_backtest.json 검증 → Worker KV 게시 (이벤트드리븐 > 무상증자 백테스터 데이터).

repo에는 커밋하지 않는다 — 일봉 포함 수 MB 데이터를 매일 커밋하면 repo가 비대해짐.
KV가 유일한 저장소이므로, 게시 전 검증으로 빈 데이터 덮어쓰기를 방지한다.
"""
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "data" / "bonus_backtest.json"
WORKER = "https://nexus-platform.nexusassetfund.workers.dev"
MIN_EVENTS = 50  # 2024년 이후 무상증자는 연 100건 안팎 — 급감하면 수집 실패로 간주


def main():
    if not SRC.exists():
        print("bonus_backtest.json 없음 — 수집 실패")
        sys.exit(1)
    data = json.loads(SRC.read_text("utf-8"))
    events = data.get("events") or []
    if len(events) < MIN_EVENTS:
        print(f"이벤트 {len(events)}건 < 최소 {MIN_EVENTS} — 수집 이상, KV 게시 중단")
        sys.exit(1)

    token = os.environ.get("NEXUS_ADMIN_TOKEN", "").strip()
    if not token:
        print("NEXUS_ADMIN_TOKEN 없음 — KV 게시 생략 (파일만 저장)")
        return
    resp = requests.post(
        f"{WORKER}/api/push",
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        data=json.dumps({"files": {"bonus_backtest.json": data}}, ensure_ascii=False).encode("utf-8"),
        timeout=120)
    print(f"POST /api/push -> {resp.status_code} {resp.text[:200]}")
    resp.raise_for_status()
    print(f"KV 게시 완료 — {len(events)}건 이벤트, updated_at {data.get('updated_at')}")


if __name__ == "__main__":
    main()
