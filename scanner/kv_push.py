# -*- coding: utf-8 -*-
"""JSON 파일 검증 → Worker KV 게시 (post_*_backtest.py 3종 + 워크플로 인라인 push 통합).

repo에는 데이터를 커밋하지 않는다 — KV가 유일한 저장소이므로,
게시 전 검증(--list-key/--min, --check-prices)으로 빈 데이터 덮어쓰기를 방지한다.
표준 라이브러리만 사용 — pip install 없는 워크플로에서도 동작.

사용례:
  python scanner/kv_push.py docs/data/ipo_backtest.json --list-key stocks --min 200
  python scanner/kv_push.py docs/data/newhigh_backtest.json --list-key events --min 400 --check-prices
  python scanner/kv_push.py docs/data/value.json --optional --ignore-push-error
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKER = "https://nexus-platform.nexusassetfund.workers.dev"
RETRIES = 4
RETRY_WAIT = 20  # 초 — 실패마다 20/40/60초로 늘려 엣지 일시 차단이 풀릴 시간을 준다

if hasattr(sys.stdout, "reconfigure"):  # Windows cp949 콘솔 대비
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--name", help="KV 파일명 (기본: src 파일명)")
    ap.add_argument("--list-key", help="검증할 리스트 키 (stocks/events 등)")
    ap.add_argument("--min", type=int, default=0, help="리스트 최소 건수 — 미달 시 게시 중단")
    ap.add_argument("--check-prices", action="store_true",
                    help="events의 code가 prices에 모두 있는지 검증 (신고가 백테스터)")
    ap.add_argument("--optional", action="store_true", help="파일 없으면 실패 대신 조용히 종료")
    ap.add_argument("--ignore-push-error", action="store_true",
                    help="KV push 실패를 무시 (다음 스캔 동기화 때 자동 반영)")
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args()

    if not a.src.exists():
        print(f"{a.src.name} 없음 — " + ("건너뜀" if a.optional else "수집 실패"))
        sys.exit(0 if a.optional else 1)
    data = json.loads(a.src.read_text("utf-8"))

    if a.list_key:
        items = data.get(a.list_key) or []
        if len(items) < a.min:
            print(f"{a.list_key} {len(items)}건 < 최소 {a.min} — 수집 이상, KV 게시 중단")
            sys.exit(1)
        if a.check_prices:
            prices = data.get("prices") or {}
            missing = [e["code"] for e in items if e["code"] not in prices]
            if missing:
                print(f"일봉 누락 종목 {len(missing)}개 ({missing[:5]}...) — 수집 이상, KV 게시 중단")
                sys.exit(1)
        n = len(items)
    else:
        n = None

    token = os.environ.get("NEXUS_ADMIN_TOKEN", "").strip()
    if not token:
        print("NEXUS_ADMIN_TOKEN 없음 — KV 게시 생략 (파일만 저장)")
        return
    name = a.name or a.src.name
    body = json.dumps({"files": {name: data}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{WORKER}/api/push", data=body, method="POST",
        # user-agent 필수 — Python-urllib 기본 UA는 Cloudflare가 403으로 차단한다
        headers={"authorization": f"Bearer {token}", "content-type": "application/json",
                 "user-agent": "nexus-scanner"})
    # 재시도 필수 — 워커는 인증 실패에 401만 돌려준다. 403은 항상 Cloudflare 엣지가
    # 낸 것(봇 판정·일시 차단)이고 한 번 튕기면 그날 데이터가 통째로 날아간다.
    # 2026-08-05 ipo/bonus가 나란히 403을 맞고 이틀간 8/4 데이터에 멈춰 있었다.
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=a.timeout) as resp:
                print(f"POST /api/push -> {resp.status} {resp.read()[:200].decode('utf-8', 'replace')}")
            last_err = None
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (400, 401):  # 요청·토큰 문제 — 재시도해도 같다
                break
        except Exception as e:
            last_err = e
        if attempt < RETRIES:
            wait = RETRY_WAIT * attempt
            print(f"KV push {attempt}차 실패({last_err}) — {wait}초 후 재시도")
            time.sleep(wait)
    if last_err is not None:
        print(f"KV push 실패: {last_err}" + (" — 다음 스캔 동기화 때 자동 반영됨" if a.ignore_push_error else ""))
        if not a.ignore_push_error:
            sys.exit(1)
        return
    extra = f"{n}건, " if n is not None else ""
    print(f"KV 게시 완료 — {extra}updated_at {data.get('updated_at')}")


if __name__ == "__main__":
    main()
