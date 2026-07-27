# -*- coding: utf-8 -*-
"""생성된 earnings_output.json 검증 후 Worker에 게시 (POST /api/earnings/analysis)"""
import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
WORKER = "https://nexus-platform.nexusassetfund.workers.dev"


def main():
    out = ROOT / "earnings_output.json"
    if not out.exists():
        print("earnings_output.json 없음 — 생성 실패")
        sys.exit(1)
    b = json.loads(out.read_text("utf-8"))

    # Worker 검증 규칙과 동일하게 fail-closed 사전 검증
    assert re.fullmatch(r"\d{6}", str(b.get("code", ""))), f"code 오류: {b.get('code')}"
    assert re.fullmatch(r"\d{4}Q[1-4]", str(b.get("period", ""))), f"period 오류: {b.get('period')}"
    assert b.get("verdict") in ("상회", "부합", "하회"), f"verdict 오류: {b.get('verdict')}"
    assert str(b.get("summary", "")).strip(), "summary 비어있음"
    assert len(str(b["summary"])) <= 300, f"summary 300자 초과: {len(str(b['summary']))}자"
    secs = b.get("sections")
    assert isinstance(secs, list) and secs, "sections 비어있음"
    for i, s in enumerate(secs):
        assert str(s.get("heading", "")).strip(), f"sections[{i}].heading 비어있음"
        assert len(str(s["heading"])) <= 80, f"sections[{i}].heading 80자 초과"
        assert str(s.get("body", "")).strip(), f"sections[{i}].body 비어있음"
        assert len(str(s["body"])) <= 8000, f"sections[{i}].body 8000자 초과"

    token = os.environ["NEXUS_ADMIN_TOKEN"]
    r = requests.post(
        f"{WORKER}/api/earnings/analysis",
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        data=json.dumps(b, ensure_ascii=False).encode("utf-8"), timeout=30)
    print(f"POST /api/earnings/analysis -> {r.status_code} {r.text[:200]}")
    r.raise_for_status()


if __name__ == "__main__":
    main()
