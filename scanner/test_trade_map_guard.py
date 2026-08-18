"""전체 실행 저장 가드 단위 테스트. 실행: python scanner/test_trade_map_guard.py

배경(2026-08): 2026-07-01 행정구역 개편으로 시도코드가 바뀌자 인천(28)·광주(29)
종목 5개가 통째로 수집 실패했다. 51/56이라 trade_stats 의 "절반 미만이면 보존"
게이트는 통과했지만, 만약 실패가 더 컸다면 verify_trade_map 전체 실행이 빈
entries 를 그대로 덮어썼을 것이다. 그 파일이 커밋되면 trade_stats.build()가
즉시 return None 해서 그다음 모든 실행이 죽는다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_trade_map import save_blocked


def test_empty_result_is_blocked():
    # 관세청이 통째로 응답하지 않은 회차 — 절대 저장하면 안 된다.
    assert save_blocked(0, 56) is not None
    # 기존 파일이 없어도(최초 실행) 0개는 여전히 막는다.
    assert save_blocked(0, 0) is not None


def test_collapse_is_blocked():
    # 56 → 20 은 정상적인 감소가 아니다. 사람이 원인을 보기 전에 덮어쓰지 않는다.
    assert save_blocked(20, 56) is not None
    assert save_blocked(27, 56) is not None          # 27 < 28 = 절반


def test_normal_run_passes():
    assert save_blocked(56, 56) is None
    assert save_blocked(51, 56) is None              # 실제 사고 당시 비율 — 통과가 맞다
    assert save_blocked(28, 56) is None              # 정확히 절반은 통과


def test_first_ever_run_passes():
    # 기존 파일이 없으면 비교 대상이 없다 — 1개라도 있으면 저장한다.
    assert save_blocked(1, 0) is None
    assert save_blocked(56, 0) is None


def test_growth_passes():
    # 종목을 늘린 회차가 막히면 안 된다.
    assert save_blocked(70, 56) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("ALL PASS")
