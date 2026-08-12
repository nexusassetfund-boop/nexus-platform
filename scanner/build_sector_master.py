"""산업분류 엑셀 → scanner/data/sector_master.json 변환 (로컬 1회성, CI에서는 안 돈다).

엑셀 컬럼: 종목코드 | 종목명 | 시장 | 산업명(대) | 산업명(중) | 주요제품
출력: {"updated": ..., "stocks": {"005930": ["반도체", "메모리반도체", "DRAM, NAND 등"]}}

run_scan.py 가 이 파일을 읽어 KRX 표준산업분류(FDR)를 덮어쓴다. 마스터에 없는 종목은
기존 KRX 분류를 그대로 쓰므로, 엑셀이 전 종목을 덮지 않아도 안전하다.

    python build_sector_master.py [엑셀경로]

openpyxl 은 이 스크립트에서만 쓴다 — requirements 에 넣지 않는다.
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

DEFAULT_SRC = Path.home() / "OneDrive" / "바탕 화면" / "통합 문서1.xlsx"
OUT = Path(__file__).parent / "data" / "sector_master.json"


def norm_code(v) -> str:
    """엑셀은 코드를 숫자로 저장해 앞의 0이 날아간다. 신형 영숫자 코드(0156T0)는 문자열."""
    s = str(v).strip().upper()
    return s.zfill(6) if s.isdigit() else s


def build(src: Path) -> dict:
    import openpyxl
    ws = openpyxl.load_workbook(src, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))[1:]

    stocks, dupes, skipped = {}, [], []
    for r in rows:
        if not r or r[0] is None:
            continue
        code = norm_code(r[0])
        if not re.fullmatch(r"\d[0-9A-Z]{5}", code):
            skipped.append((code, r[1]))
            continue
        clean = [str(x).strip() if x is not None else "" for x in r[3:6]]
        if not clean[0]:
            skipped.append((code, r[1]))
            continue
        if code in stocks and stocks[code] != clean:
            dupes.append((code, r[1]))
        stocks[code] = clean

    return {
        "updated": dt.datetime.now().strftime("%Y-%m-%d"),
        "source": src.name,
        "stocks": dict(sorted(stocks.items())),
    }, dupes, skipped


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src.exists():
        sys.exit(f"엑셀 없음: {src}")
    data, dupes, skipped = build(src)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=0), "utf-8")

    l1 = {v[0] for v in data["stocks"].values()}
    l2 = {v[1] for v in data["stocks"].values() if v[1]}
    print(f"{OUT} — {len(data['stocks'])}종목 / 대분류 {len(l1)} / 중분류 {len(l2)}")
    if dupes:
        print(f"  중복 코드(값 불일치, 마지막 행 채택) {len(dupes)}건: {dupes[:5]}")
    if skipped:
        print(f"  스킵(코드 형식·대분류 누락) {len(skipped)}건: {skipped[:5]}")


if __name__ == "__main__":
    main()
