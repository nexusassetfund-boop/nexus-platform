# RRG 리플레이 검증 리포트 (Phase 4)

- 실행일: 2026-08-03 / 데이터: 17개 섹터, 주간 114주 (cutoff 2026-07-31)
- 이상치 플래그: {'semiconductor': ['2026-03-05', '2026-07-31'], 'robotics': ['2026-03-04'], 'energy': ['2026-03-04', '2026-07-31'], 'aero-defense-space': ['2026-03-04', '2026-03-05'], 'resource-materials': ['2026-03-04'], 'battery-renewable': ['2023-11-06'], 'shipbuilding-shipping': ['2026-03-04'], 'construction-realestate': ['2026-04-08']}

## 1. 사분면 전환 빈도 (10주당 평균 전환 횟수, 낮을수록 안정)
- 신 방식(주간 RRG): **3.37회**
- 구 방식(수익률 백분위, 주간 등가 재현): **5.57회**

## 2. 부상→주도 진입 후 forward 상대수익률 (vs 동일가중 벤치마크, %p)
- 이벤트 수: 58건
- +4주: 평균 -1.03%p, 승률 41% (n=56)
- +8주: 평균 -0.66%p, 승률 37% (n=54)
- +12주: 평균 -1.78%p, 승률 36% (n=53)

## 이벤트 상세
- 2025-01-03 semiconductor: +4w +1.1%p, +8w -4.4%p, +12w -2.7%p
- 2025-06-27 semiconductor: +4w -7.9%p, +8w -5.9%p, +12w +12.0%p
- 2025-08-29 semiconductor: +4w +17.8%p, +8w +38.7%p, +12w +33.8%p
- 2024-07-12 robotics: +4w -10.7%p, +8w -9.0%p, +12w -11.1%p
- 2024-11-22 robotics: +4w -3.9%p, +8w +5.3%p, +12w +9.1%p
- 2025-06-27 robotics: +4w -8.7%p, +8w -9.7%p, +12w -0.0%p
- 2025-09-05 robotics: +4w +8.7%p, +8w +23.5%p, +12w +29.0%p
- 2024-09-13 it-service-sw: +4w -1.1%p, +8w -2.1%p, +12w +12.6%p
- 2024-10-04 it-service-sw: +4w -0.0%p, +8w +10.1%p, +12w +4.5%p
- 2025-04-04 it-service-sw: +4w -8.8%p, +8w -15.4%p, +12w -6.1%p
- 2025-06-20 it-service-sw: +4w -8.8%p, +8w -12.6%p, +12w -14.0%p
- 2025-10-10 it-service-sw: +4w -5.2%p, +8w -6.7%p
- 2024-11-22 auto-mobility: +4w +1.1%p, +8w -3.6%p, +12w -12.9%p
- 2024-12-13 auto-mobility: +4w +0.6%p, +8w -9.0%p, +12w -14.1%p
- 2025-03-28 auto-mobility: +4w -12.3%p, +8w -20.7%p, +12w -31.0%p
- 2025-08-22 auto-mobility: +4w -7.4%p, +8w -0.1%p, +12w +2.4%p
- 2025-10-31 auto-mobility: +4w +8.8%p
- 2024-03-29 energy: +4w -4.3%p, +8w +11.7%p, +12w +4.3%p
- 2025-01-03 energy: +4w +3.4%p, +8w +0.6%p, +12w -10.1%p
- 2025-05-02 energy: +4w +11.1%p, +8w +22.8%p, +12w +31.6%p
- 2024-06-28 finance: +4w +7.6%p, +8w +12.7%p, +12w +9.6%p
- 2025-03-28 finance: +4w -4.3%p, +8w +1.7%p, +12w -2.1%p
- 2025-01-03 aero-defense-space: +4w +6.1%p, +8w +35.1%p, +12w +44.9%p
- 2024-02-16 resource-materials: +4w -5.0%p, +8w -7.2%p, +12w -8.6%p
- 2024-09-13 resource-materials: +4w +10.0%p, +8w +21.5%p, +12w +31.4%p
- 2025-02-28 resource-materials: +4w +1.0%p, +8w -12.7%p, +12w -18.8%p
- 2025-06-13 resource-materials: +4w +11.9%p, +8w +1.4%p, +12w +1.1%p
- 2024-03-22 battery-renewable: +4w -10.5%p, +8w -14.2%p, +12w -15.5%p
- 2024-09-13 battery-renewable: +4w +4.1%p, +8w -11.0%p, +12w -17.6%p
- 2024-09-27 battery-renewable: +4w -11.2%p, +8w -18.6%p, +12w -25.5%p
- 2025-07-04 battery-renewable: +4w -2.8%p, +8w -3.0%p, +12w -7.6%p
- 2025-07-18 battery-renewable: +4w +11.5%p, +8w -3.0%p, +12w -6.4%p
- 2024-07-05 bio-healthcare: +4w +4.1%p, +8w +14.0%p, +12w +10.0%p
- 2025-01-03 bio-healthcare: +4w -1.3%p, +8w -3.9%p, +12w -11.7%p
- 2025-01-31 bio-healthcare: +4w -2.4%p, +8w -10.0%p, +12w -14.7%p
- 2025-07-18 bio-healthcare: +4w -4.0%p, +8w -5.8%p, +12w -8.6%p
- 2025-09-05 bio-healthcare: +4w -3.8%p, +8w -7.2%p, +12w +10.5%p
- 2024-07-26 telecom: +4w +1.1%p, +8w +3.1%p, +12w +3.4%p
- 2025-04-04 telecom: +4w -2.2%p, +8w -8.2%p, +12w -13.2%p
- 2025-04-18 telecom: +4w -2.3%p, +8w -10.5%p, +12w -11.7%p
- 2025-11-28 telecom: (forward 구간 부족)
- 2024-02-23 construction-realestate: +4w -4.5%p, +8w -6.8%p, +12w -8.6%p
- 2024-07-19 construction-realestate: +4w +0.6%p, +8w +0.8%p, +12w -5.4%p
- 2025-02-14 construction-realestate: +4w -4.2%p, +8w +0.1%p, +12w -3.4%p
- 2025-03-28 construction-realestate: +4w -2.5%p, +8w +7.9%p, +12w +10.0%p
- 2025-12-05 construction-realestate: (forward 구간 부족)
- 2025-05-16 media-ent-game: +4w -6.8%p, +8w -7.2%p, +12w -18.7%p
- 2025-07-04 media-ent-game: +4w -13.7%p, +8w -13.6%p, +12w -14.9%p
- 2024-04-05 retail-fashion-beauty: +4w +11.7%p, +8w +33.9%p, +12w +32.8%p
- 2024-09-13 retail-fashion-beauty: +4w -6.3%p, +8w -8.5%p, +12w -14.2%p
- 2024-02-16 chem-materials: +4w -4.0%p, +8w -7.8%p, +12w -13.9%p
- 2024-09-27 chem-materials: +4w -9.1%p, +8w -15.2%p, +12w -18.2%p
- 2025-07-04 chem-materials: +4w -7.2%p, +8w -8.8%p, +12w -8.8%p
- 2025-10-17 chem-materials: +4w +6.3%p
- 2024-04-19 food-agri-fishery: +4w +4.2%p, +8w +10.7%p, +12w +0.8%p
- 2024-09-27 food-agri-fishery: +4w -6.6%p, +8w -3.9%p, +12w -0.3%p
- 2024-11-29 food-agri-fishery: +4w +1.2%p, +8w -8.3%p, +12w -12.0%p
- 2025-03-21 food-agri-fishery: +4w +2.5%p, +8w +3.7%p, +12w -5.9%p

## 게이트 판정 기준
- forward 상대수익률이 유의미하게 양(+)이고 승률 > 50%면 v2에서 매매어 코멘트 해금 검토. 아니면 상태 서술 유지 (계획안 Phase 4).