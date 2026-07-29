"""
관세청 수출입무역통계 오픈API 클라이언트 (공공데이터포털 제공기관코드 1220000).

제공 데이터셋(각각 별도 활용신청 필요 — 개발계정 자동승인, 무료):
  - 품목별 수출입실적(GW)          data.go.kr/data/15101609  → HS 2/4/6/10단위 집계
  - 품목별 국가별 수출입실적(GW)   data.go.kr/data/15100475  → 국가×HS (cntyCd 필수)
  - 시군구별 품목별 수출입실적     data.go.kr/data/15134343  → 지역×HS (종목 매칭의 핵심)

공통 제약:
  - 응답이 XML (JSON 미제공). 기간은 YYYYMM 월 단위, **1회 조회 최대 1년**.
  - 개발계정 호출 한도 10,000회 → 월별 응답을 디스크에 캐시하고 재호출하지 않는다.
  - 확정치는 매월 15일경 전월분 현행화. 그 전에 조회하면 전월 데이터가 없다.

엔드포인트: 시군구별은 data.go.kr 상세명세에서 직접 확인했다 —
      apis.data.go.kr/1220000/sigunguperprlstperacrs/getSigunguPerPrlstPerAcrs
      품목별/품목별국가별 경로는 미확인 추정치이므로, 실제와 다르면 환경변수
      CUSTOMS_EP_ITEM / CUSTOMS_EP_ITEM_COUNTRY 로 덮어쓸 수 있게 해 두었다.
      **시군구별 응답에는 중량이 없다** — 금액과 건수(expCnt/impCnt)만 준다. 따라서 종목
      레벨에서는 진짜 수출단가(금액÷중량)를 만들 수 없고, 건당 금액(금액÷건수)을 대용으로
      쓴다. 진짜 단가가 필요하면 중량이 있는 품목별 API(Itemtrade)를 품목 레벨에서 쓴다.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger("customs")

BASE = "https://apis.data.go.kr/1220000"
# 시군구별 경로는 data.go.kr 상세명세(15134343)에서 직접 확인함. 나머지가 틀리면 환경변수로 교정.
EP_ITEM = os.environ.get("CUSTOMS_EP_ITEM", f"{BASE}/Itemtrade/getItemtradeList")
EP_ITEM_COUNTRY = os.environ.get("CUSTOMS_EP_ITEM_COUNTRY", f"{BASE}/nitemtrade/getNitemtradeList")
EP_DISTRICT = os.environ.get(
    "CUSTOMS_EP_DISTRICT", f"{BASE}/sigunguperprlstperacrs/getSigunguPerPrlstPerAcrs")
# 관세환율(주간 고시). 수출액이 USD라 원화 매출과 상관계수를 낼 때 환산 보정에 쓴다.
EP_FX = os.environ.get("CUSTOMS_EP_FX", f"{BASE}/retrieveTrifFxrtInfo/getRetrieveTrifFxrtInfo")

TIMEOUT = 30
RETRIES = 3
SLEEP = 0.3          # 호출 간 간격 (API 보호)

# 응답 필드명 → 표준 키. 두 오퍼레이션의 태그 체계가 완전히 다르므로 합집합으로 매핑한다.
#   Itemtrade      : year / hsCode / statKor / expDlr / expWgt / impDlr / impWgt / balPayments
#   시군구별       : priodTitle / hsSgn / korePrlstNm / expUsdAmt / expCnt / impUsdAmt / impCnt
#                    / cmtrBlncAmt / sggNm
# 중요: 시군구별 응답에는 **중량이 없다**. 대신 건수(expCnt)가 있어 물량 프록시로 쓴다.
_FIELD_MAP = {
    # 공통/품목별(Itemtrade)
    "year": "period", "hsCode": "hs", "hsCd": "hs", "statKor": "name", "statCd": "hs",
    "expDlr": "exp_amt", "expWgt": "exp_wgt", "impDlr": "imp_amt", "impWgt": "imp_wgt",
    "balPayments": "balance", "cntyName": "country", "cntyCd": "country_cd",
    # 시군구별
    "priodTitle": "period", "hsSgn": "hs", "korePrlstNm": "name", "sggNm": "sgg",
    "expUsdAmt": "exp_amt", "impUsdAmt": "imp_amt",
    "expCnt": "exp_cnt", "impCnt": "imp_cnt", "cmtrBlncAmt": "balance",
    "expLnCnt": "exp_cnt", "impLnCnt": "imp_cnt",
    # 관세환율
    "fxrt": "fx_rate", "currSgn": "curr", "mtryUtNm": "curr_name",
    "aplyBgnDt": "apply_date", "imexTp": "imex", "cntySgn": "country_cd",
}
_NUMERIC = ("exp_amt", "exp_wgt", "imp_amt", "imp_wgt", "balance",
            "exp_cnt", "imp_cnt", "fx_rate")


class CustomsError(RuntimeError):
    pass


# 실제 네트워크 호출 수(캐시 히트는 세지 않는다). 개발계정 한도 대비 소진량 파악용.
_CALLS = 0


def call_count() -> int:
    return _CALLS


def _cache_load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("캐시 로드 실패(무시하고 새로 받음): %s", e)
        return {}


def _cache_save(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _parse(xml_text: str) -> list[dict]:
    """관세청 XML → dict 리스트. resultCode가 00이 아니면 CustomsError."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise CustomsError(f"XML 파싱 실패: {e} / 응답 앞부분={xml_text[:200]!r}") from e

    code_el = root.find(".//resultCode")
    if code_el is not None and (code_el.text or "").strip() not in ("00", "0"):
        msg = root.find(".//resultMsg")
        raise CustomsError(f"API 오류 {code_el.text}: {msg.text if msg is not None else '?'}")

    rows = []
    for item in root.iter("item"):
        row = {}
        for child in item:
            key = _FIELD_MAP.get(child.tag, child.tag)
            val = (child.text or "").strip()
            if key in _NUMERIC:
                try:
                    val = float(val.replace(",", "")) if val else None
                except ValueError:
                    val = None
            row[key] = val
        if row:
            rows.append(row)
    return rows


def _get(endpoint: str, params: dict, api_key: str) -> list[dict]:
    """단일 호출 + 재시도. serviceKey는 이미 인코딩된 키를 그대로 붙인다(이중 인코딩 방지)."""
    global _CALLS
    _CALLS += 1
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{endpoint}?serviceKey={api_key}&{qs}"
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                return _parse(resp.read().decode("utf-8", errors="replace"))
        except CustomsError:
            raise                      # API가 명시적으로 거절한 것은 재시도해도 같다
        except urllib.error.HTTPError as e:
            # 4xx는 인증·권한·잘못된 요청이라 재시도해도 같다. 429(호출 초과)만 예외로 기다린다.
            # 키 미승인 상태에서 수백 조합을 각각 3회씩 재시도하면 작업이 몇 시간씩 늘어진다.
            if 400 <= e.code < 500 and e.code != 429:
                raise CustomsError(
                    f"HTTP {e.code} — 인증키가 이 API에 승인되지 않았거나 요청이 잘못됐습니다 "
                    f"(승인 직후에는 반영에 시간이 걸립니다). {endpoint}") from e
            last = e
            logger.warning("관세청 호출 실패(시도 %d/%d): %s", attempt + 1, RETRIES, e)
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            last = e
            logger.warning("관세청 호출 실패(시도 %d/%d): %s", attempt + 1, RETRIES, e)
            time.sleep(2 * (attempt + 1))
    raise CustomsError(f"호출 최종 실패: {last}")


def fetch_item(hs: str, start_yymm: str, end_yymm: str, api_key: str,
               cache_path: Path | None = None) -> list[dict]:
    """품목별 수출입실적. 기간은 1년 이내여야 한다(API 제약)."""
    return _fetch_cached(EP_ITEM, {"strtYymm": start_yymm, "endYymm": end_yymm, "hsSgn": hs},
                         api_key, cache_path, f"item:{hs}:{start_yymm}:{end_yymm}")


def fetch_all_items(yymm: str, api_key: str, cache_path: Path | None = None) -> list[dict]:
    """hsSgn을 생략하면 그 달의 전 품목(HS 10단위 약 9,600건)이 품목명과 함께 온다.

    별도의 HS 마스터 파일(data.go.kr 파일데이터)을 수동으로 준비하지 않아도
    이 호출 하나로 코드↔한글품목명 대응표를 만들 수 있다. 수출금액도 함께 오므로
    같은 이름의 후보가 여럿일 때 규모로 우선순위를 매길 수 있다.
    """
    return _fetch_cached(EP_ITEM, {"strtYymm": yymm, "endYymm": yymm},
                         api_key, cache_path, f"allitems:{yymm}")


def fetch_district(hs: str, sido_cd: str, start_yymm: str, end_yymm: str, api_key: str,
                   cache_path: Path | None = None) -> list[dict]:
    """시군구별 품목별 수출입실적 — 종목 매칭 프록시의 원천.

    입력은 **시도코드 2자리**(sidoCd)이고 응답이 그 시도 안의 시군구(sggNm)별로 쪼개져 온다.
    따라서 (시도 × HS) 1회 호출로 그 도의 모든 시군구를 얻는다 — 호출 수를 크게 아낀다.
    HS는 **6단위 필수**. 파라미터명 HsSgn의 대문자 H는 명세 그대로다(품목별 API는 소문자).
    """
    hs6 = str(hs)[:6]
    return _fetch_cached(EP_DISTRICT,
                         {"strtYymm": start_yymm, "endYymm": end_yymm, "HsSgn": hs6, "sidoCd": sido_cd},
                         api_key, cache_path, f"dist:{sido_cd}:{hs6}:{start_yymm}:{end_yymm}")


def _fetch_cached(endpoint: str, params: dict, api_key: str,
                  cache_path: Path | None, cache_key: str) -> list[dict]:
    cache = _cache_load(cache_path) if cache_path else {}
    if cache_key in cache:
        return cache[cache_key]
    rows = _get(endpoint, params, api_key)
    time.sleep(SLEEP)
    if cache_path is not None:
        cache[cache_key] = rows
        _cache_save(cache_path, cache)
    return rows


def fetch_fx_month(yymm: str, api_key: str, cache_path: Path | None = None,
                   imex: str = "1") -> float | None:
    """해당 월의 USD 관세환율(원/달러). 없으면 None.

    관세환율은 주간 고시라 월 대표값이 필요하다. 월 중순(15일) 기준 주간 환율 한 건만
    받아 그 달의 대표값으로 쓴다 — 개발계정 한도가 1,000회로 낮아 월 1회 호출로 아낀다.
    weekFxrtTpcd는 수출(1)/수입(2)이 다르며, 수출액 환산이므로 기본값이 수출이다.
    """
    for day in ("15", "01", "08", "22"):
        try:
            rows = _fetch_cached(
                EP_FX, {"aplyBgnDt": f"{yymm}{day}", "weekFxrtTpcd": imex},
                api_key, cache_path, f"fx:{imex}:{yymm}{day}")
        except CustomsError as e:
            # 인증/권한 문제면 다른 날짜로 바꿔도 결과가 같다 — 즉시 포기한다.
            if "HTTP 4" in str(e):
                raise
            logger.debug("환율 조회 실패 %s%s: %s", yymm, day, e)
            continue
        for r in rows:
            if str(r.get("curr", "")).upper() == "USD" or "미국" in str(r.get("curr_name", "")):
                rate = r.get("fx_rate")
                if rate:
                    return rate
    return None


def month_range(end_yymm: str, months: int) -> list[tuple[str, str]]:
    """end_yymm에서 과거로 months개월을 1년 이내 구간들로 쪼갠다(API 1년 제약 대응)."""
    y, m = int(end_yymm[:4]), int(end_yymm[4:])
    chunks, remaining = [], months
    while remaining > 0:
        take = min(12, remaining)
        e_y, e_m = y, m
        s_total = y * 12 + (m - 1) - (take - 1)
        s_y, s_m = divmod(s_total, 12)
        chunks.append((f"{s_y:04d}{s_m + 1:02d}", f"{e_y:04d}{e_m:02d}"))
        total = s_total - 1
        y, m = total // 12, total % 12 + 1
        remaining -= take
    return chunks


def require_key() -> str:
    key = os.environ.get("CUSTOMS_API_KEY", "").strip()
    if not key:
        raise CustomsError(
            "CUSTOMS_API_KEY 미설정 — data.go.kr에서 '관세청_시군구별 품목별 수출입실적' "
            "활용신청 후 발급받은 키를 환경변수(또는 GitHub Secrets)에 넣으세요.")
    return key
