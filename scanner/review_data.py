# -*- coding: utf-8 -*-
"""전략 포트폴리오 주간 복기 — 입력 데이터 수집.

tracking.json(전략 원장·기준가)과 야후 지수로 이번 주 성과·매매 내역을 정리해
review_input.json을 만든다. 해설·개선 제안은 Claude(review_prompt.md)가 작성.

사용: python scanner/review_data.py [--date YYYY-MM-DD]  (기본: 오늘 KST)
"""
import argparse
import datetime as dt
import json
import re
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "data"
OUT = ROOT / "review_input.json"

TRACK_NAMES = {"1": "모멘텀", "3": "돌파"}


def _pick_weekly(pts, week_start: dt.date, week_end: dt.date):
    """(date, close) 목록 → 주 시작 전 마지막 종가 대비 주중 마지막 종가 등락률(%)."""
    before = [c for day, c in pts if day < week_start]
    inweek = [c for day, c in pts if week_start <= day <= week_end]
    if not before or not inweek:
        return None
    return round((inweek[-1] / before[-1] - 1) * 100, 2)


def _index_weekly(yahoo_sym: str, naver_sym: str, week_start: dt.date, week_end: dt.date):
    """지수 주간 등락률 — 야후 우선, 캔들 지연 시 네이버 fchart 폴백."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?range=1mo&interval=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        res = d["chart"]["result"][0]
        pts = [(dt.datetime.fromtimestamp(t, tz=KST).date(), c)
               for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"])
               if c is not None]
        v = _pick_weekly(pts, week_start, week_end)
        if v is not None:
            return v
    except Exception:
        pass
    try:  # 네이버 fchart: item data="YYYYMMDD|시|고|저|종|량"
        url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={naver_sym}"
               f"&timeframe=day&count=30&requestType=0")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            xml = r.read().decode("euc-kr", "replace")
        pts = []
        for m in re.finditer(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', xml):
            pts.append((dt.date(int(m[1][:4]), int(m[1][4:6]), int(m[1][6:])), float(m[2])))
        return _pick_weekly(pts, week_start, week_end)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="기준일 YYYY-MM-DD (기본 오늘 KST)")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz=KST).date()
    week_start = today - dt.timedelta(days=today.weekday())  # 이번 주 월요일
    week_id = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"

    tracking = json.loads((DATA / "tracking.json").read_text("utf-8"))
    scan = json.loads((DATA / "scan.json").read_text("utf-8"))

    # ── 트랙별 주간 성과 (track_nav 기준가: 주 시작 전 마지막 값 대비) ──
    tracks = {}
    fresh = False
    for tid, series in (tracking.get("track_nav") or {}).items():
        pts = sorted(series, key=lambda p: p["date"])
        before = [p for p in pts if p["date"] < week_start.isoformat()]
        inweek = [p for p in pts if week_start.isoformat() <= p["date"] <= today.isoformat()]
        if inweek:
            fresh = True
        base = before[-1]["nav"] if before else 1000.0
        last = inweek[-1]["nav"] if inweek else (before[-1]["nav"] if before else None)
        tracks[tid] = {
            "name": TRACK_NAMES.get(str(tid), f"트랙{tid}"),
            "nav": last,
            "weekly_pct": round((last / base - 1) * 100, 2) if (last and base) else None,
            "since_inception_pct": round((last / 1000 - 1) * 100, 2) if last else None,
            "week_series": [{"date": p["date"], "nav": p["nav"]} for p in inweek],
        }

    if not fresh:
        OUT.write_text(json.dumps({"skip": True, "reason": "이번 주 거래 데이터 없음"},
                                  ensure_ascii=False), "utf-8")
        print("skip: 이번 주 track_nav 없음 (휴장 주간?)")
        return

    def _h(h, extra=()):
        keys = ["ticker", "name", "sector", "entry_date", "entry_price", "entry_stage",
                "last_price", "return_pct", "total_return_pct", "qty_frac", "realized_pct",
                "days_held", "stage_now", "confidence_now", "signals_at_entry", "last_action"]
        out = {k: h.get(k) for k in list(keys) + list(extra) if h.get(k) is not None}
        if h.get("partials"):
            out["partials"] = [{"date": p.get("date"), "frac": p.get("frac"),
                                "ret_pct": p.get("ret_pct"), "reason": p.get("reason")}
                               for p in h["partials"]]
        return out

    holdings = [_h(h) for h in tracking.get("holdings", [])]
    exited_all = tracking.get("exited", [])
    week_exits = [_h(e, extra=("exit_date", "exit_price", "exit_reason"))
                  for e in exited_all
                  if (e.get("exit_date") or "") >= week_start.isoformat()]
    new_entries = [h for h in holdings if (h.get("entry_date") or "") >= week_start.isoformat()]

    # 스테이지 분포 (감지기 유니버스 컨텍스트)
    stage_dist = {}
    for r in scan.get("results", []):
        s = r.get("stage")
        stage_dist[str(s)] = stage_dist.get(str(s), 0) + 1

    out = {
        "week": week_id,
        "range": {"start": week_start.isoformat(), "end": today.isoformat()},
        "generated": dt.datetime.now(tz=KST).isoformat(timespec="seconds"),
        "nav_base_date": tracking.get("nav_base"),
        "tracks": tracks,
        "benchmark": {
            "kospi_weekly_pct": _index_weekly("%5EKS11", "KOSPI", week_start, today),
            "kosdaq_weekly_pct": _index_weekly("%5EKQ11", "KOSDAQ", week_start, today),
            "kospi_now": scan.get("kospi"),
        },
        "stats": tracking.get("stats", {}),
        "holdings": holdings,
        "new_entries_this_week": new_entries,
        "exits_this_week": week_exits,
        "exited_recent": [_h(e, extra=("exit_date", "exit_price", "exit_reason"))
                          for e in exited_all[-10:]],
        "stage_distribution": stage_dist,
        "rules": {
            "편입": "2트랙 — 모멘텀(스테이지2 진입)·돌파(전고점 돌파+거래량), MTT 통과 필수",
            "이탈": "-10% 손절 또는 MA60 종가 이탈",
            "부분익절": "10 EMA +20% 이격 시 절반",
        },
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    print(f"review_input.json 저장 — {week_id} ({week_start}~{today}), "
          f"보유 {len(holdings)} 신규 {len(new_entries)} 이탈 {len(week_exits)}")


if __name__ == "__main__":
    main()
