"""quality_growth 순수 스코어링 함수 단위 테스트. 실행: python scanner/test_quality_growth.py"""
from quality_growth import _winsor_z, _quality_z, _composite, Z_COMPONENTS


def approx(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def test_winsor_z_sign_and_center():
    # 대칭 표본: 평균 0, 부호 +면 큰 값이 양의 z
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    z = _winsor_z(vals, +1)
    assert z[0] < 0 < z[-1], z
    assert approx(sum(z), 0.0, 1e-9), z          # z-score 합은 0
    zneg = _winsor_z(vals, -1)
    assert all(approx(a, -b) for a, b in zip(z, zneg)), (z, zneg)  # 부호 반전


def test_winsor_z_too_few_returns_none():
    assert _winsor_z([1.0, 2.0, 3.0, 4.0], +1) == [None] * 4      # 5개 미만


def test_winsor_z_preserves_missing():
    z = _winsor_z([1.0, None, 2.0, 3.0, 4.0, 5.0], +1)
    assert z[1] is None, z
    assert len([x for x in z if x is not None]) == 5, z


def test_winsor_z_caps_outlier():
    # 극단 이상치는 95퍼센타일로 클립 → z가 무한정 커지지 않음
    vals = [1.0, 1.0, 1.0, 1.0, 1.0, 1000.0]
    z = _winsor_z(vals, +1)
    assert z[-1] is not None and z[-1] < 5, z


def test_quality_z_averages_available_components():
    recs = [{"roe": v, "gpa": v, "opm": v, "debt": v, "accruals": v} for v in (1, 2, 3, 4, 5, 6)]
    _quality_z(recs)
    assert recs[0]["quality_z"] < recs[-1]["quality_z"], recs
    # 모든 구성요소 동일값 → debt/accruals 부호(−) 상쇄로 최고 roe 종목이 반드시 최고는 아님:
    # 여기선 5개 지표가 같은 방향이라 순위 단조 확인만.


def test_quality_z_none_when_all_missing():
    recs = [{"roe": None, "gpa": None, "opm": None, "debt": None, "accruals": None} for _ in range(6)]
    _quality_z(recs)
    assert all(r["quality_z"] is None for r in recs), recs


def test_composite_5050_blend():
    # quality_z·mom 모두 존재 → composite는 두 z의 평균 방향
    recs = []
    for i, mom in enumerate([10, 20, 30, 40, 50, 60]):
        recs.append({"roe": i, "gpa": i, "opm": i, "debt": 0, "accruals": 0, "mom_12_1": mom})
    _quality_z(recs)
    _composite(recs)
    comps = [r["composite"] for r in recs]
    assert all(c is not None for c in comps), comps
    assert comps[0] < comps[-1], comps      # 퀄리티·모멘텀 동반 상승 → composite 단조 증가


def test_composite_uses_quality_only_when_mom_missing():
    recs = [{"roe": i, "gpa": i, "opm": i, "debt": 0, "accruals": 0, "mom_12_1": None}
            for i in range(6)]
    _quality_z(recs)
    _composite(recs)
    # 모멘텀 전무 → mom_z None → composite == quality_z
    for r in recs:
        assert r["mom_z"] is None, r
        assert approx(r["composite"], r["quality_z"]), r


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("ALL PASS")
