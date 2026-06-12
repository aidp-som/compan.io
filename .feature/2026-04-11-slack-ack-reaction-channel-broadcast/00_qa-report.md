# QA 결과 보고서

> 작성일: 2026-04-11
> 대상: Phase 1, 2, 3 (PR 1·2·3)
> 기반: 00_qa-criteria.md, 04-plan.md

## 1. Phase별 검증 결과

### Phase 1 (PR 1: Reaction ACK) — `feature/slack-ack-reaction` (`32f2774`)

| # | 검증 항목 | 기대 결과 | 실제 결과 | 판정 |
|---|---|---|---|---|
| Q1.1 | `_add_reaction` 헬퍼 + 9가지 에러 분기 | 메서드 + try/except | ✅ 9가지 코드 (`already_reacted`, `message_not_found`, `no_reaction`, `not_reacted`, `not_in_channel`, `invalid_auth`, `missing_scope`, `ratelimited`, 기타) 모두 분기 | ✅ PASS |
| Q1.2 | `_remove_reaction` 동일 패턴 | 동일 | ✅ 동일 패턴 | ✅ PASS |
| Q1.3 | `_on_app_mention` lock 밖 발송 | `asyncio.create_task` fire-and-store | ✅ `_send_ack_reaction` 내부에서 task 생성 | ✅ PASS |
| Q1.4 | `_on_message` (DM, channel thread) 동일 처리 | 동일 | ✅ 3 곳 모두 동일 패턴 | ✅ PASS |
| Q1.5 | `send()` `_reaction_lifecycle` 분기 | done_success/done_error 처리 | ✅ `_finalize_reaction` 호출 + content 빈 경우 텍스트 dispatch skip | ✅ PASS |
| Q1.6 | `_active_ack_reactions` OrderedDict + TTL + maxsize + Lock | 자료구조 + 락 | ✅ TTL 1h, maxsize 500, asyncio.Lock | ✅ PASS |
| Q1.7 | `_dispatch` try/finally + done 보장 | 정상/예외/취소 모든 경로 | ✅ 3 경로 단위 테스트로 잠금 | ✅ PASS |
| Q1.8 | `ack_reactions_enabled` flag | 스키마 + 런타임 분기 | ✅ default False | ✅ PASS |
| Q1.9 | scope 누락 시 자동 비활성화 | flag off + ERROR | ✅ `_ack_reactions_runtime_disabled` 세팅 + ERROR 로그 | ✅ PASS |
| Q1.10 | `_reaction_lifecycle`이 화이트리스트에 없음 | 변경 없음 | ✅ 회귀 가드 통과 | ✅ PASS |
| Q1.11 | grep guard `message_ts` 추출 | 정규식 일치 | ✅ `test_slack_inbound_metadata.py` 통과 | ✅ PASS |
| Q1.12 | pytest 전체 통과 | 기존 + 신규 | ✅ 신규 42 통과 (사전 실패 2건 제외) | ✅ PASS |
| Q1.13 | ruff 통과 | 변경 파일 모두 | ✅ 신규 코드 0 위반 (사전 1건 제외) | ✅ PASS |

**Phase 1 판정: PASS** (필수 13/13)

### Phase 2 (PR 2: Progress Pulse) — `feature/slack-progress-pulse` (`b8fed2c`)

| # | 검증 항목 | 기대 결과 | 실제 결과 | 판정 |
|---|---|---|---|---|
| Q2.1 | `_progress_pulse_loop` + 5초 4회 캡 | 상수 + asyncio loop | ✅ `PULSE_INTERVAL_SECONDS=5`, `PULSE_MAX_COUNT=4` | ✅ PASS |
| Q2.2 | task 생성 + finally cancel + await | try/finally | ✅ `_process_message` 안에서 lifecycle (P1 충돌 회피) | ✅ PASS |
| Q2.3 | 빠른 응답 시 pulse 0회 | sleep 도달 전 cancel | ✅ `test_pulse_cancelled_on_fast_response` | ✅ PASS |
| Q2.4 | `"생각 중... (약 N초)"` 형식 | format string | ✅ `PULSE_TEXT_FORMAT` | ✅ PASS |
| Q2.5 | `progress_pulse_enabled` flag | 스키마 + 런타임 분기 | ✅ default False | ✅ PASS |
| Q2.6 | Slack 외 채널 미발송 | `channel == "slack"` 체크 | ✅ `_should_start_pulse` | ✅ PASS |
| Q2.7 | pytest 통과 | 기존 + 신규 | ✅ 신규 15 통과 (사전 실패 제외) | ✅ PASS |
| Q2.8 | ruff 통과 | 변경 파일 | ✅ 신규 코드 0 위반 | ✅ PASS |

**Phase 2 판정: PASS** (필수 8/8)

### Phase 3 (PR 3: Channel Broadcast) — `feature/slack-broadcast` (`3d4d88e`)

| # | 검증 항목 | 기대 결과 | 실제 결과 | 판정 |
|---|---|---|---|---|
| Q3.1 | `share_to_channel` 파라미터 | 시그니처 확장 | ✅ default False | ✅ PASS |
| Q3.2 | 3중 안전장치 | 모두 True 시만 broadcast | ✅ is_channel + thread_ts + flag + blocked 4중 검사 (defense in depth) | ✅ PASS |
| Q3.3 | skip 힌트 반환 | "(Note: ... skipped)" | ✅ 반환값에 사유 포함 | ✅ PASS |
| Q3.4 | 마지막 chunk만 broadcast | for loop index | ✅ `is_last` 분기 | ✅ PASS |
| Q3.5 | `broadcast_blocked_channels` 강제 차단 | 차단 채널 무시 | ✅ MessageSender + SlackChannel 양쪽에서 검사 | ✅ PASS |
| Q3.6 | Shadow detector WARN | 트리거 매칭 + 도구 미호출 | ✅ `_BROADCAST_TRIGGER_PATTERN` + `_invoke_claude_turn` 헬퍼 + try/finally | ✅ PASS |
| Q3.7 | `broadcast_enabled` + `broadcast_blocked_channels` flag | 스키마 필드 | ✅ 둘 다 추가 | ✅ PASS |
| Q3.8 | `reply_broadcast`가 화이트리스트에 없음 | 회귀 가드 | ✅ `test_reply_broadcast_not_in_whitelist` 통과 | ✅ PASS |
| Q3.9 | 구조화 로그 (sent INFO + shadow_miss WARN) | logger 호출 | ✅ caplog 검증 | ✅ PASS |
| Q3.10 | pytest 통과 | 기존 + 신규 | ✅ 신규 27 + 회귀 2 통과 (사전 실패 제외) | ✅ PASS |
| Q3.11 | ruff 통과 | 변경 파일 | ✅ 신규 코드 0 위반 | ✅ PASS |

**Phase 3 판정: PASS** (필수 11/11)

## 2. 의도 vs 결과

### 의도한 것
- **Feature 1 (Reaction ACK)**: 사용자가 멘션 시 즉시 (lock 밖) 시각적 ACK → "봇이 살아있다" 신호
- **Feature 2 (Channel Broadcast)**: 사용자가 자연어로 "채널에도 공유" 요청 시 broadcast, 안전한 가드로 오탐/오용 방지
- **Feature 3 (Progress Pulse)**: 처리가 길어질 때 5초 단위 경과시간 표시로 "진행 중" 신호

### 실제 달성
- **F1**: ✅ `_on_app_mention`/`_on_message` 3 곳에서 lock 밖 `asyncio.create_task`로 발송. dispatch session lock과 무관하게 즉시 ACK
- **F1**: ✅ done lifecycle은 `_dispatch` try/finally로 모든 경로(정상/예외/취소) 보장
- **F1**: ✅ scope 누락은 런타임 자동 비활성화 + ERROR 로그 (운영 사고 시 자가 보호)
- **F2**: ✅ `share_to_channel` LLM 도구 옵션, 4중 가드(`is_channel` + `thread_ts` + `broadcast_enabled` + `broadcast_blocked_channels`)
- **F2**: ✅ Shadow detector로 트리거 발화 vs LLM 도구 사용 불일치 자동 감지 → 2주 metric 리뷰의 데이터 소스
- **F2**: ✅ 구조화 감사 로그 (`slack.broadcast.sent` INFO)
- **F3**: ✅ `_progress_messages` 캐시 + `chat.update` 경로 100% 재사용, 코드 추가 없이 metadata만으로 트리거
- **F3**: ✅ Cancel 보장 (response/exception/cancellation 모든 경로에서 task 정리)

### 차이 분석
- **`_process_message` 헬퍼 분리**: P2와 P3가 각각 본문을 별도 헬퍼(`_process_message_inner`, `_invoke_claude_turn`)로 분리. 04-plan에 명시되지 않았지만 try/finally 사용을 위한 합리적 리팩토링. **단, P2와 P3가 같은 메서드에 다른 헬퍼 이름을 도입하므로 PR 간 머지 충돌 발생 예정** — control-agent가 머지 시 reconcile 필요. 트리아지: "후속 처리"
- **F2 Shadow detector 범위**: slash command(`/new`, `/help`, `/stop`) 경로는 LLM을 거치지 않으므로 shadow 검사 대상에서 제외 (P3 worker가 의도적 결정). 합리적이며 04-plan과 충돌 없음.
- 추가 부수 보강 (`not_in_channel` 채널당 1회 WARN, retry 성공 경로 테스트 추가) — 04-plan §1.11 표 충실 구현, 결정 변경 아님.

## 3. 미충족 항목 대응

없음. 모든 필수 항목 통과.

## 4. 발견된 부수 이슈

### [심각도: 높음] PR 2 ↔ PR 3 머지 충돌 예정

- **현상**: P2는 `_process_message` 본문을 `_process_message_inner` 헬퍼로 분리, P3는 `_invoke_claude_turn` 헬퍼로 분리. 같은 메서드에 다른 이름의 헬퍼를 도입.
- **영향**: 두 PR을 모두 머지할 때 manual reconcile 필요.
- **권장 조치 (3가지)**:
  1. **머지 순서 결정 → reconcile**: P1 → P2 머지 → P3을 P2 위에 rebase하면서 헬퍼 이름 통일. 권장.
  2. **P3 재작업**: P3 worker에게 P2 헬퍼 이름(`_process_message_inner`)을 사용하도록 패치 디스패치.
  3. **하나로 묶기**: P2 + P3을 단일 PR로 통합. 04-plan의 "PR 분리" 원칙과 충돌. 비추천.

### [심각도: 중] `_dispatch`의 try/finally가 P1에만 있고 P2에 없음

- **현상**: P1은 `_dispatch`에 try/finally를 추가하여 done lifecycle을 보장. P2는 `_dispatch` 미변경으로 `_process_message` 안에서 pulse task lifecycle을 처리.
- **영향**: P1 단독 머지 시 done reaction은 잘 발행. P2 단독 머지 시 pulse cancel은 잘 작동. **하지만 P1+P2 동시 머지 시 두 try/finally가 nested 되어 동작 순서 확인 필요.**
- **권장 조치**: 머지 reconcile 시 outer(`_dispatch`)에서 done lifecycle, inner(`_process_message`)에서 pulse cancel이 자연스럽게 nested 되는지 통합 테스트로 검증.

### [심각도: 낮음] 사전 존재 환경 의존성

- `tests/test_claude_cli.py::TestBuildCmd::test_basic`, `test_add_dir_home` — `claude` 바이너리 PATH 가정. 본 PR과 무관, PR#2 이전부터 존재.
- `companio/core/context.py:121` `F541` ruff 위반 — 본 PR과 무관, PR#2 이전부터 존재.
- **권장 조치**: 별도 정리 PR로 분리. 본 작업 머지에는 영향 없음.

## 5. 후속사항 트리아지

### 즉시 처리 (완료)

없음. 3개 PR 모두 자체 검증 완료.

### 후속 계획 추가

- **머지 reconcile**: P1 → P2 → P3 순서로 머지하면서 헬퍼 이름 통일. control-agent가 머지 시 처리.
- **사전 존재 이슈 정리 PR**: `test_claude_cli.py` 환경 의존성 + `context.py:121` ruff 위반 → 별도 cleanup PR.

### 블로커 (사용자 확인 대기)

- **머지 전략 결정**:
  - (a) 3개 PR을 GitHub PR로 만들어 한 번에 리뷰/머지 (사용자가 GitHub UI에서 reconcile)
  - (b) control-agent가 로컬에서 P1 머지 → P2 rebase → P3 rebase → 통합된 단일 브랜치를 push하여 한 PR로 머지
  - (c) PR을 별도로 만들지 말고 control-agent가 직접 holmes에 순차 push (PR 안 만듦)
- **WIP 커밋의 정식화**: 현재 각 브랜치에 `WIP: ...` 임시 커밋이 있음. 정식 커밋 메시지로 amend 또는 squash 필요.
