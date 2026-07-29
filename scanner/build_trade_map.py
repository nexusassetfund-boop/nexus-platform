"""
수출 품목 ↔ 종목 인덱스 빌더 — "이 품목을 수출하는 상장사는 누구인가"를 만든다.

설계 배경: 처음에는 종목별로 (소재지 × 품목) 수출액을 매출과 상관계수로 검증해
매출을 추정하려 했으나 실패했다. 20종목 시험에서 A·B등급이 0개였다. 두 가지 이유였다.
  - DART 본점 주소는 등기상 본사이지 공장이 아니다(기아=서초구, 유한양행=동작구).
  - 수출·매출이 함께 우상향하기만 해도 수준 상관은 0.5~0.6이 나온다(허위 상관).
그리고 애초에 방향이 거꾸로였다. 디램·자동차처럼 큰 품목은 수출사가 수십 곳이라
어느 종목에도 깨끗이 안 붙는다.

그래서 키움증권 방식으로 바꿨다. 키움은 매출 상관을 주장하지 않는다. "인공호흡기
수출 43만 달러, 이걸 수출하는 종목은 한컴라이프케어·씨유메디칼" 처럼 **좁은 품목과
소수 종목**을 보여줄 뿐이다. 품목이 좁을수록 수출사가 적어 귀속이 깨끗해진다.

파이프라인:
  1) 품목별 API로 HS(10단위) 전 품목 + 한글 품목명 + 수출금액 수집
  2) HS 앞자리로 산업 분류(반도체·2차전지·화장품/미용 등 15개 안팎)
  3) sector_map.json 테마명 ↔ HS 품목명 매칭으로 품목별 종목 목록 구성
  4) 종목이 적을수록 귀속이 깨끗하다 → 종목 수를 신뢰도 신호로 함께 기록
출력: scanner/data/trade_map.json (trade_stats.py가 읽음)

DART를 쓰지 않는다 — 상관계수 검증을 하지 않으므로 분기 매출이 필요 없다.
덕분에 20종목에 21분 걸리던 병목이 사라졌다.

한계(화면에 그대로 표시한다):
  - 이 종목이 그 품목을 수출한다는 것이지, 수출액이 그 종목 몫이라는 뜻이 아니다.
  - 해외 생산분은 한국 수출통계에 잡히지 않는다.
  - 비상장사·미매칭 기업의 수출도 같은 품목에 섞여 있다.

실행: 스케줄 없음. 월 1회 정도 trade-map-rebuild.yml 수동 실행이면 충분하다.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import customs_api as capi

logger = logging.getLogger("trade_map")

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
OUT_PATH = Path(__file__).parent / "data" / "trade_map.json"
CACHE_PATH = ROOT / "docs" / "data" / "trade_raw_cache.json"
# sector_map.json은 nexus-cloud에 있다. CI에서는 워크플로가 Worker 공개 URL에서
# scanner/data/ 로 내려받고, 로컬에서는 옆 디렉터리 원본을 읽는다.
SECTOR_MAP_CI = Path(__file__).parent / "data" / "sector_map.json"
SECTOR_MAP_LOCAL = ROOT.parent / "nexus-cloud" / "public" / "sector_map.json"

# ── 기준 ──
# 품목 수출입액 하한. **품목별 API의 금액 단위는 USD다**(시군구별 API는 천USD로 다르다).
# 키움도 총수출액 43만 달러짜리 품목을 다룬다 — 규모가 작아야 수출사가 적어 귀속이
# 깨끗하다. 다만 너무 낮으면 통관 한두 건짜리 노이즈가 들어와 10만 달러를 하한으로 둔다.
MIN_AMOUNT = 100_000.0
MAX_STOCKS = 12           # 종목이 이보다 많이 붙는 품목은 귀속이 흐려 목록에서 뺀다
SIM_CUTOFF = 0.62         # 테마명 ↔ 품목명 유사도 하한

# HS 앞자리 → 산업 분류. 키움의 산업 분류 선택(음식료·자동차·철강·2차전지·IT·
# 미디어엔터·통신·화장품/미용·화학 …)을 참고해 15개 안팎으로 묶는다.
# 더 구체적인 규칙(4자리)이 앞에 오고, 없으면 2자리 장(章)으로 떨어진다.
_IND_4: list[tuple[tuple[str, ...], str]] = [
    (("8541", "8542"), "반도체"),
    (("8486",), "반도체장비"),
    (("8534",), "반도체"),
    (("8507",), "2차전지"),
    (("8517", "8525", "8526", "8527", "8528"), "통신/미디어"),
    (("8471", "8473", "8523"), "IT"),
    (("9013", "9001"), "디스플레이"),
    (("3303", "3304", "3305", "3306", "3307"), "화장품/미용"),
    (("2710", "2711"), "석유/에너지"),
    (("8802", "8806", "9306", "9301", "9302"), "방산/항공"),
]
_IND_2: dict[str, str] = {
    **{f"{c:02d}": "음식료" for c in range(1, 25)},
    **{f"{c:02d}": "석유/에너지" for c in (25, 26, 27)},
    **{f"{c:02d}": "제약/바이오" for c in (29, 30)},
    **{f"{c:02d}": "화학" for c in (28, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40)},
    **{f"{c:02d}": "소비재" for c in range(41, 50)},
    **{f"{c:02d}": "섬유/의류" for c in range(50, 68)},
    **{f"{c:02d}": "소비재" for c in (68, 69, 70)},
    "71": "귀금속",
    **{f"{c:02d}": "철강/금속" for c in range(72, 84)},
    "84": "기계",
    "85": "IT",
    **{f"{c:02d}": "운송기계" for c in (86, 88)},
    "87": "자동차",
    "89": "조선",
    "90": "의료기기/정밀",
    **{f"{c:02d}": "소비재" for c in range(91, 98)},
}


def classify(hs: str) -> str:
    """HS 코드 → 산업 분류명."""
    hs = str(hs or "")
    for prefixes, name in _IND_4:
        if hs[:4] in prefixes:
            return name
    return _IND_2.get(hs[:2], "기타")


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("%s 로드 실패: %s", path.name, e)
        return default


def latest_month(today: dt.date) -> str:
    """확정치 기준월 — 매월 15일경 전월분이 현행화된다."""
    back = 1 if today.day >= 16 else 2
    t = today.year * 12 + (today.month - 1) - back
    return f"{t // 12:04d}{t % 12 + 1:02d}"


def load_stocks() -> dict[str, list[tuple[str, str]]]:
    """sector_map.json → {테마명: [(종목코드, 종목명)]}"""
    smap = _load(SECTOR_MAP_CI, None) or _load(SECTOR_MAP_LOCAL, None)
    if not smap:
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for sec in smap.get("sectors") or []:
        for th in sec.get("themes") or []:
            name = th.get("name") or ""
            lst = out.setdefault(name, [])
            for st in th.get("stocks") or []:
                if isinstance(st, (list, tuple)) and st:
                    code = str(st[0]).zfill(6)
                    if code.isdigit() and len(code) == 6:
                        nm = st[1] if len(st) > 1 else code
                        if (code, nm) not in lst:
                            lst.append((code, nm))
    return out


def _match_score(theme: str, name: str) -> float:
    """테마명과 품목명의 유사도. 한쪽이 다른 쪽을 포함하면 강하게 인정한다."""
    if not theme or not name:
        return 0.0
    t, n = theme.lower().replace(" ", ""), name.lower().replace(" ", "")
    if len(t) >= 2 and (t in n or n in t):
        return 0.9
    return difflib.SequenceMatcher(None, t, n).ratio()


def build() -> dict | None:
    api_key = capi.require_key()
    today = dt.datetime.now(tz=KST).date()
    month = latest_month(today)

    themes = load_stocks()
    if not themes:
        logger.error("sector_map.json 없음 — 워크플로의 '섹터맵 내려받기' 단계를 확인하세요")
        return None
    logger.info("테마 %d개 / 종목 %d개",
                len(themes), len({c for v in themes.values() for c, _ in v}))

    try:
        rows = capi.fetch_all_items(month, api_key, CACHE_PATH)
    except capi.CustomsError as e:
        logger.error("품목 목록 수집 실패: %s", e)
        return None
    logger.info("품목 %d건 수집 (%s 기준)", len(rows), month)

    # ── 품목 정리: 10단위 그대로 쓴다. 키움도 '인공호흡기' 같은 잎 단위를 보여준다.
    items: dict[str, dict] = {}
    for r in rows:
        hs = str(r.get("hs") or "")
        name = (r.get("name") or "").strip()
        amt = r.get("exp_amt") or 0.0
        imp = r.get("imp_amt") or 0.0
        # 수출이 작아도 수입이 크면 남긴다 — 원재료·소재는 수입 쪽이 신호다.
        # BeOn 채널이 MR-MUF(언더필) 수입으로 SK하이닉스·삼성전자를, 펄프 수입으로
        # 제지주를 추적하는 방식이다. 특수 소재는 수입사가 적어 귀속이 오히려 깨끗하다.
        # 응답 끝에 hs가 '총계'인 합계 행이 하나 섞여 온다 — 자릿수 검사로 걸러진다.
        if len(hs) < 6 or not name or max(amt, imp) < MIN_AMOUNT:
            continue
        if name in ("기타", "기타의 것", "그 밖의 것", "총계"):
            continue          # 이름만으로 무엇인지 알 수 없는 품목은 제외
        items[hs] = {"hs": hs, "name": name,
                     "amount": round(amt, 1), "import_amount": round(imp, 1),
                     "industry": classify(hs), "stocks": []}

    # ── 품목 ↔ 종목: 테마명과 품목명을 대조한다.
    #     좁은 품목일수록 붙는 종목이 적고, 그럴수록 귀속이 깨끗하다.
    for hs, it in items.items():
        seen: set[str] = set()
        for theme, stocks in themes.items():
            if _match_score(theme, it["name"]) < SIM_CUTOFF:
                continue
            for code, nm in stocks:
                if code not in seen:
                    seen.add(code)
                    it["stocks"].append({"ticker": code, "name": nm, "theme": theme})

    matched = {hs: it for hs, it in items.items()
               if 0 < len(it["stocks"]) <= MAX_STOCKS}
    if not matched:
        logger.error("종목이 붙은 품목이 하나도 없음 — 매칭 기준을 확인하세요")
        return None

    by_ind: dict[str, int] = {}
    for it in matched.values():
        by_ind[it["industry"]] = by_ind.get(it["industry"], 0) + 1
    tickers = {s["ticker"] for it in matched.values() for s in it["stocks"]}

    logger.info("매칭 품목 %d개 / 종목 %d개 / 산업 %d종",
                len(matched), len(tickers), len(by_ind))
    return {
        "updated": dt.datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M"),
        "base_month": month,
        "method": "품목명 ↔ 섹터맵 테마명 매칭. 해당 품목을 수출하는 것으로 보이는 상장사를 제시하며, "
                  "수출액이 그 종목의 실적이라는 뜻은 아니다.",
        "criteria": {"min_amount": MIN_AMOUNT, "max_stocks": MAX_STOCKS, "sim_cutoff": SIM_CUTOFF},
        "industries": sorted(by_ind, key=lambda k: -by_ind[k]),
        # 수출·수입 중 큰 쪽 기준으로 정렬 — 수입만 큰 소재 품목도 앞에 오도록.
        "items": sorted(matched.values(),
                        key=lambda x: -max(x["amount"], x.get("import_amount") or 0)),
        "coverage": {"items": len(matched), "tickers": len(tickers),
                     "scanned_items": len(items), "by_industry": by_ind},
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        data = build()
    except capi.CustomsError as e:
        logger.error("인덱스 구축 실패: %s", e)
        sys.exit(1)
    if data is None:
        logger.error("인덱스 구축 실패 — 기존 파일 보존, exit 1")
        sys.exit(1)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    cov = data["coverage"]
    logger.info("저장: %s (품목 %d · 종목 %d · 산업 %s)",
                OUT_PATH, cov["items"], cov["tickers"],
                ", ".join(f"{k} {v}" for k, v in
                          sorted(cov["by_industry"].items(), key=lambda x: -x[1])[:6]))


if __name__ == "__main__":
    main()
