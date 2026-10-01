# 세트 비교 — `baseline` vs `v1` (`ai-agent-daily-briefing`)

| case | type | baseline 통과 | v1 통과 | baseline 점수 | v1 점수 | 변화 |
|---|---|---|---|---|---|---|
| core-01 | core | 1/2 | 1/2 | 0.933/1.0 | 1.0/0.933 | = 동등/겹침 |
| core-02 | core | 2/2 | 2/2 | 1.0/1.0 | 1.0/1.0 | = 동등/겹침 |
| reg-01 | regression | 2/2 | 2/2 | 1.0/1.0 | 1.0/1.0 | = 동등/겹침 |
| bnd-01 | boundary | 2/2 | 2/2 | 1.0/1.0 | 1.0/1.0 | = 동등/겹침 |
| bnd-02 | boundary | 2/2 | 2/2 | 1.0/1.0 | 1.0/1.0 | = 동등/겹침 |
| bnd-03 | boundary | 2/2 | 2/2 | 1.0/1.0 | 1.0/1.0 | = 동등/겹침 |

## 제안 판정: `tie`
- 근거: 결정적 차이 없음
- ※ 기본값은 tie. promote 전에 두 variant 의 transcript·브리핑을 직접 읽고 확정하라.