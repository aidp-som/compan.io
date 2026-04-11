---
description: 피처를 PM→디자이너→개발→테스터→리뷰어 순서로 반복 구현하여 모든 점수가 10점이 될 때까지 루프합니다. Shift-Left 원칙에 따라 디자인을 개발 전에 배치하여 재작업을 최소화합니다.
disable-model-invocation: true
---

# 피처 구현 루프 오케스트레이션

당신은 메인 오케스트레이터입니다. 아래 파이프라인을 점수가 모두 10점이 될 때까지 반복하세요.

## 에이전트 간 산출물 전달 규칙

오케스트레이터는 각 에이전트의 **출력 텍스트를 다음 에이전트의 프롬프트에 직접 포함**하여 전달합니다.

| 단계 | 보내는 쪽 | 받는 쪽 | 전달 내용 |
|------|-----------|---------|-----------|
| 1→2 | PM | Designer | PM의 JSON 산출물 전체 (feature, module, tasks, AC) |
| 1→3 | PM+Designer | Developer | PM JSON + Designer 디자인 스펙 전체 |
| 3→4 | Developer | Tester | Developer가 변경/생성한 **파일 경로 목록** + PM의 AC |
| 1~4→5 | 전체 | Reviewer | PM JSON + Designer 스펙 + Developer 변경 파일 + Tester 결과 JSON |

### 전달 프롬프트 템플릿

에이전트 호출 시 아래 형식으로 이전 산출물을 포함하세요:

```
이전 에이전트 산출물:

[PM 산출물]
{PM의 출력 텍스트}

[Designer 산출물]
{Designer의 출력 텍스트}

피처 요청: {사용자 요청}
이전 라운드 피드백: {있는 경우만}
```

## 실행 규칙

### 라운드 제한
- 최대 5라운드까지 반복
- 5라운드 내에 10점 도달 못 하면 현재까지의 최선의 결과로 종료

### 파이프라인 순서 (Shift-Left 원칙)

#### ROUND 1: 풀 파이프라인
첫 라운드에서는 아래 순서대로 **모든** 서브에이전트를 호출하세요.

**Phase 1: 기획 (순차)**
1. `pm` 에이전트 호출 → 요구사항 분석 및 태스크 분해

**Phase 2: 설계 (순차)**
2. `designer` 에이전트 호출 → PM 산출물 기반 UI/UX 스펙 정의
   - 컴포넌트 구조, 상태 정의, 접근성 요건, 반응형 기준 명시
   - 개발자가 참고할 구체적인 디자인 스펙 산출

**Phase 3: 구현 (순차)**
3. `developer` 에이전트 호출 → PM 스펙 + 디자이너 스펙을 동시에 참고하여 코드 구현

**Phase 4: 검증 (순차)**
4. `tester` 에이전트 호출 → 구현 결과에 대해 테스트 작성 및 실행

**Phase 5: 최종 판정 (순차)**
5. `reviewer` 에이전트 호출 → 모든 산출물 종합 검토

#### ROUND 2+: 선택적 재실행
2라운드부터는 reviewer 출력에서 `RERUN_AGENTS:` 라인을 찾아 **해당 에이전트만** 재실행하세요.

##### RERUN_AGENTS 파싱 방법
1. Reviewer 출력 텍스트에서 `**RERUN_AGENTS:` 로 시작하는 라인을 찾는다
2. `[` 와 `]` 사이의 에이전트 이름을 추출한다
3. 유효한 에이전트 이름: `pm`, `designer`, `developer`, `tester`
4. 추출된 에이전트를 **파이프라인 순서**(pm→designer→developer→tester)대로 실행한다
5. 파싱 실패 시 → 풀 파이프라인으로 폴백

예시:
- `RERUN_AGENTS: [developer, tester]` → developer → tester → reviewer 순서로만 실행
- `RERUN_AGENTS: [pm, designer, developer, tester]` → 풀 파이프라인 재실행

재실행하지 않는 에이전트의 이전 산출물은 그대로 유지하고, 다음 에이전트에 전달합니다.
reviewer는 매 라운드 반드시 실행합니다.

### 점수 확인 및 루프 결정

reviewer의 FINAL_VERDICT를 확인하세요:

- **PASS (모든 점수 10/10)** → 루프 종료, 커밋 단계로 진행
- **FAIL** → FAIL_REASONS와 RERUN_AGENTS를 확인하여 다음 라운드 진행

### 다음 라운드 진행 시
- reviewer의 피드백을 해당 에이전트에게 전달: "이전 라운드 피드백: {FAIL_REASONS}"
- 해당 에이전트는 피드백을 반영하여 산출물 개선
- reviewer가 다시 전체 산출물 종합 검토

### 각 라운드 시작 시 출력
```
========================================
ROUND {N}/5 시작
이전 라운드 점수:
  PM: {score}/10
  DESIGN: {score}/10
  DEV: {score}/10
  TEST: {score}/10
  REVIEW: {score}/10
재실행 대상: {RERUN_AGENTS 또는 "전체 (첫 라운드)"}
========================================
```

> 첫 라운드에서는 이전 점수를 "N/A"로 표시하세요.

### 루프 종료 시 출력
```
========================================
PIPELINE 완료
최종 라운드: {N}
최종 점수:
  PM: {score}/10
  DESIGN: {score}/10
  DEV: {score}/10
  TEST: {score}/10
  REVIEW: {score}/10
VERDICT: {PASS/FAIL}
========================================
```

## 피처 요청
$ARGUMENTS
