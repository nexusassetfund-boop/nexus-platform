# RRG 리플레이 검증 리포트 (Phase 4)

- 실행일: 2026-08-03 / 데이터: 17개 섹터, 주간 106주 (cutoff 2026-07-31)
- 이상치 플래그: {'semiconductor': ['2026-03-05', '2026-07-31'], 'robotics': ['2026-03-04'], 'energy': ['2026-03-04', '2026-07-31'], 'aero-defense-space': ['2026-03-04', '2026-03-05'], 'resource-materials': ['2026-03-04'], 'battery-renewable': ['2023-11-06'], 'shipbuilding-shipping': ['2026-03-04'], 'construction-realestate': ['2026-04-08']}

## 1. 사분면 전환 빈도 (10주당 평균 전환 횟수, 낮을수록 안정)
- 신 방식(주간 RRG): **3.29회**
- 구 방식(수익률 백분위, 주간 등가 재현): **5.56회**

## 2. 사분면 전이별 forward 상대수익률 (vs 동일가중 벤치마크, %p)
- **improving>leading** (n=59): +4주 -1.25%p(승률 42%), +8주 -2.51%p(승률 36%), +12주 -4.74%p(승률 32%)
- **leading>weakening** (n=49): +4주 -1.25%p(승률 43%), +8주 -3.08%p(승률 30%), +12주 -0.60%p(승률 38%)
- **weakening>lagging** (n=38): +4주 +1.21%p(승률 50%), +8주 +2.89%p(승률 61%), +12주 +3.61%p(승률 56%)
- **lagging>improving** (n=112): +4주 -2.25%p(승률 28%), +8주 -3.93%p(승률 29%), +12주 -5.51%p(승률 29%)
- 역신호 판정: improving>leading이 유의하게 음(-)이고 leading>weakening이 유의하게 양(+)이면 평균회귀(역신호) 구조 — 인사이트 문구에 반영 검토.

## 이벤트 상세
- 2025-01-07 semiconductor: +4w +2.2%p, +8w -11.4%p, +12w -12.7%p
- 2025-02-10 semiconductor: +4w -12.9%p, +8w -14.0%p, +12w -23.0%p
- 2025-07-08 semiconductor: +4w -6.0%p, +8w -4.0%p, +12w +19.4%p
- 2025-09-10 semiconductor: +4w +28.2%p, +8w +38.8%p
- 2024-07-16 robotics: +4w -10.0%p, +8w -8.8%p, +12w -12.5%p
- 2024-11-21 robotics: +4w -2.5%p, +8w +8.1%p, +12w +8.0%p
- 2025-09-10 robotics: +4w +18.5%p, +8w +23.5%p
- 2024-09-30 it-service-sw: +4w +0.3%p, +8w +7.8%p, +12w +5.0%p
- 2025-06-24 it-service-sw: +4w -14.6%p, +8w -13.3%p, +12w -19.3%p
- 2024-11-21 auto-mobility: +4w +0.9%p, +8w -5.8%p, +12w -13.7%p
- 2024-12-12 auto-mobility: +4w +0.6%p, +8w -14.1%p, +12w -14.3%p
- 2025-01-14 auto-mobility: +4w -14.0%p, +8w -14.2%p, +12w -14.9%p
- 2025-03-25 auto-mobility: +4w -13.0%p, +8w -21.8%p, +12w -34.8%p
- 2025-08-20 auto-mobility: +4w -9.1%p, +8w -2.9%p, +12w +0.5%p
- 2025-09-03 auto-mobility: +4w -9.9%p, +8w -6.5%p, +12w +9.8%p
- 2025-11-12 auto-mobility: (forward 구간 부족)
- 2024-04-02 energy: +4w -2.6%p, +8w +12.3%p, +12w -1.2%p
- 2024-09-23 energy: +4w -4.5%p, +8w -7.4%p, +12w -6.9%p
- 2025-01-14 energy: +4w +2.4%p, +8w -7.2%p, +12w -11.8%p
- 2024-06-03 finance: +4w +3.4%p, +8w +8.9%p, +12w +11.3%p
- 2024-11-07 finance: +4w +1.8%p, +8w -5.0%p, +12w -6.9%p
- 2025-04-01 finance: +4w -2.9%p, +8w +3.9%p, +12w +5.5%p
- 2025-09-10 finance: +4w -4.8%p, +8w +0.9%p
- 2024-02-19 resource-materials: +4w -6.2%p, +8w -8.1%p, +12w -8.6%p
- 2024-09-04 resource-materials: +4w +14.1%p, +8w +30.8%p, +12w +42.4%p
- 2024-09-23 resource-materials: +4w +24.4%p, +8w +11.2%p, +12w +15.6%p
- 2025-03-11 resource-materials: +4w -10.8%p, +8w -17.0%p, +12w -20.8%p
- 2025-07-01 resource-materials: +4w +11.6%p, +8w -2.8%p, +12w -4.0%p
- 2025-12-03 resource-materials: (forward 구간 부족)
- 2024-03-12 battery-renewable: +4w -10.3%p, +8w -16.1%p, +12w -14.2%p
- 2024-09-04 battery-renewable: +4w +2.7%p, +8w -14.3%p, +12w -21.8%p
- 2025-07-22 battery-renewable: +4w +9.1%p, +8w -4.6%p, +12w +19.2%p
- 2025-10-22 battery-renewable: +4w +0.8%p
- 2024-07-09 bio-healthcare: +4w +5.7%p, +8w +11.6%p, +12w +13.4%p
- 2025-01-07 bio-healthcare: +4w -0.0%p, +8w -9.5%p, +12w -8.7%p
- 2025-02-10 bio-healthcare: +4w -9.1%p, +8w -8.3%p, +12w -21.3%p
- 2025-09-03 bio-healthcare: +4w -2.1%p, +8w +1.0%p, +12w +10.7%p
- 2024-03-19 shipbuilding-shipping: +4w +0.5%p, +8w +6.4%p, +12w +5.6%p
- 2024-07-30 telecom: +4w +1.3%p, +8w +3.1%p, +12w +5.6%p
- 2025-04-08 telecom: +4w -8.2%p, +8w -14.6%p, +12w -18.5%p
- 2025-04-22 telecom: +4w -3.3%p, +8w -13.6%p, +12w -15.1%p
- 2025-08-20 telecom: +4w -11.0%p, +8w -17.9%p, +12w -16.0%p
- 2025-11-26 telecom: (forward 구간 부족)
- 2024-02-26 construction-realestate: +4w -7.0%p, +8w -5.5%p, +12w -9.2%p
- 2024-07-16 construction-realestate: +4w +3.6%p, +8w +2.1%p, +12w -4.4%p
- 2025-04-01 construction-realestate: +4w -0.2%p, +8w +10.9%p, +12w +11.2%p
- 2025-12-03 construction-realestate: (forward 구간 부족)
- 2025-05-16 media-ent-game: +4w -10.9%p, +8w -10.1%p, +12w -13.0%p
- 2024-12-19 retail-fashion-beauty: +4w -4.2%p, +8w -12.1%p, +12w -7.6%p
- 2025-03-25 retail-fashion-beauty: +4w +5.3%p, +8w +17.6%p, +12w +12.7%p
- 2024-02-19 chem-materials: +4w -2.2%p, +8w -9.7%p, +12w -13.1%p
- 2024-09-30 chem-materials: +4w -6.7%p, +8w -15.1%p, +12w -20.2%p
- 2025-07-08 chem-materials: +4w -3.4%p, +8w -7.3%p, +12w -6.3%p
- 2025-10-22 chem-materials: +4w +2.9%p
- 2024-04-17 food-agri-fishery: +4w +4.4%p, +8w +9.9%p, +12w -1.3%p
- 2024-09-30 food-agri-fishery: +4w -4.8%p, +8w -3.8%p, +12w -2.2%p
- 2024-12-05 food-agri-fishery: +4w -6.1%p, +8w -11.0%p, +12w -13.4%p
- 2025-03-25 food-agri-fishery: +4w +3.5%p, +8w +4.7%p, +12w -9.0%p
- 2025-07-15 food-agri-fishery: +4w -3.7%p, +8w -8.7%p, +12w -12.0%p

## 게이트 판정 기준
- forward 상대수익률이 유의미하게 양(+)이고 승률 > 50%면 v2에서 매매어 코멘트 해금 검토. 아니면 상태 서술 유지 (계획안 Phase 4).