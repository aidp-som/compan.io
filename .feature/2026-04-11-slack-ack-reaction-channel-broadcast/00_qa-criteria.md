# QA 기준: Slack ACK 리액션 + Progress Pulse + Channel Broadcast

> 작성일: 2026-04-11
> 기반: 00_overview.md, 04-plan.md §5

## Phase별 검증 기준

### Phase 1 (PR 1): Reaction ACK

| # | 검증 항목 | 기대 결과 | 검증 방법 | 우선순위 |
|---|---|---|---|---|
| Q1.1 | `_add_reaction` 헬퍼 존재 + 9가지 에러 코드 분기 | slack.py에 메서드 + try/except | 코드 grep + 단위 테스트 | 필수 |
| Q1.2 | `_remove_reaction` 헬퍼 존재 + idempotent | 동일 패턴 | 코드 grep + 단위 테스트 | 필수 |
| Q1.3 | `_on_app_mention`에서 리액션 fire-and-store (lock 밖) | `asyncio.create_task` 또는 직접 await | 코드 grep | 필수 |
| Q1.4 | `_on_message` (DM, channel thread)도 동일 처리 | 동일 패턴 | 코드 grep | 필수 |
| Q1.5 | `SlackChannel.send()` `_reaction_lifecycle` metadata 분기 | done_success/done_error 처리 | 단위 테스트 | 필수 |
| Q1.6 | `_active_ack_reactions` OrderedDict + TTL + maxsize + Lock | 자료구조 + 주변 락 | 단위 테스트 (TTL evict, maxsize evict) | 필수 |
| Q1.7 | `loop.py:_dispatch` try/finally + done 이벤트 발행 보장 | 정상/예외/취소 모든 경로 | 단위 테스트 (3 경로) | 필수 |
| Q1.8 | `SlackConfig.ack_reactions_enabled: bool = False` flag | 스키마 필드 + 런타임 분기 | 코드 grep + 단위 테스트 | 필수 |
| Q1.9 | scope 누락 시 런타임 자동 비활성화 | `missing_scope`/`invalid_auth` → flag off + ERROR | 단위 테스트 (caplog) | 필수 |
| Q1.10 | `_reaction_lifecycle` 키가 화이트리스트에 없음 | `_FORWARDED_METADATA_KEYS` 변경 없음 | 회귀 가드 테스트 | 필수 |
| Q1.11 | grep guard: `_on_app_mention`이 `event.get("ts")` → metadata `message_ts` | grep 정규식 일치 | grep guard 테스트 | 권장 |
| Q1.12 | pytest 전체 통과 | 기존 104 passed + 신규 추가 통과 | `pytest tests/` | 필수 |
| Q1.13 | ruff 통과 | 변경 파일 + 신규 파일 모두 | `ruff check` | 필수 |

### Phase 2 (PR 2): Progress Pulse

| # | 검증 항목 | 기대 결과 | 검증 방법 | 우선순위 |
|---|---|---|---|---|
| Q2.1 | `_progress_pulse_loop` 메서드 존재 + 5초 4회 캡 | 상수 + asyncio loop | 단위 테스트 | 필수 |
| Q2.2 | `loop.py:_dispatch`에서 task 생성 + finally cancel + await | try/finally 패턴 | 단위 테스트 (cancel 경로) | 필수 |
| Q2.3 | 빠른 응답(<5s) 시 pulse 0회 | sleep 도달 전 cancel | 단위 테스트 | 필수 |
| Q2.4 | 메시지 형식 `"생각 중... (약 N초)"` | format string | 단위 테스트 | 필수 |
| Q2.5 | `SlackConfig.progress_pulse_enabled: bool = False` flag | 스키마 + 런타임 분기 | 코드 + 단위 테스트 | 필수 |
| Q2.6 | Slack 외 채널은 pulse 미발송 | channel == "slack" 체크 | 단위 테스트 | 필수 |
| Q2.7 | pytest 전체 통과 | 기존 + 신규 통과 | `pytest tests/` | 필수 |
| Q2.8 | ruff 통과 | 변경 파일 모두 | `ruff check` | 필수 |

### Phase 3 (PR 3): Channel Broadcast

| # | 검증 항목 | 기대 결과 | 검증 방법 | 우선순위 |
|---|---|---|---|---|
| Q3.1 | `MessageSender.send(share_to_channel=False)` 파라미터 | 시그니처 확장 | 코드 grep + 단위 테스트 | 필수 |
| Q3.2 | 3중 안전장치 (is_channel + thread_ts + flag) | 모두 True일 때만 broadcast | 단위 테스트 (4 케이스) | 필수 |
| Q3.3 | 차단 시 reply string에 skip 힌트 포함 | "(Note: ... skipped)" | 단위 테스트 | 필수 |
| Q3.4 | `SlackChannel.send()`에서 마지막 chunk에만 `reply_broadcast=True` | for loop index 비교 | 단위 테스트 (split 케이스) | 필수 |
| Q3.5 | `broadcast_blocked_channels` 강제 차단 | 차단 채널은 무시 | 단위 테스트 | 필수 |
| Q3.6 | Shadow detector — 트리거 패턴 매칭 + 도구 호출 여부 → WARN | inbound 처리 직후 검사 | 단위 테스트 (caplog) | 필수 |
| Q3.7 | `SlackConfig.broadcast_enabled: bool = False` + `broadcast_blocked_channels: list[str]` | 스키마 필드 | 코드 grep | 필수 |
| Q3.8 | `reply_broadcast` 키가 화이트리스트에 없음 | 회귀 가드 | 회귀 테스트 | 필수 |
| Q3.9 | 구조화 로그 `slack.broadcast.sent` (INFO) + `slack.broadcast.shadow_miss` (WARN) | logger 호출 검증 | 단위 테스트 (caplog) | 필수 |
| Q3.10 | pytest 전체 통과 | 기존 + 신규 통과 | `pytest tests/` | 필수 |
| Q3.11 | ruff 통과 | 변경 파일 모두 | `ruff check` | 필수 |

## 전체 품질 기준

### 기능적

- [ ] 04-plan.md §2 "포함" 항목 모두 코드로 구현 (Feature 1, 2, 3)
- [ ] 04-plan.md §5 단위 테스트 항목 모두 작성 + 통과
- [ ] 04-plan.md §1.1~1.14a 결정 사항 준수

### 비기능적

- [ ] `pytest tests/` 전체 통과 (PR#2 베이스라인 104 passed 유지)
- [ ] `ruff check companio/ tests/` 통과
- [ ] `_FORWARDED_METADATA_KEYS` 정의 변경 없음 (회귀 가드)
- [ ] PR#2 도입 패턴(`reply_to_inbound`, per-thread Lock, `_progress_messages`) 재활용, 우회 금지
- [ ] 어떤 PR도 다른 PR의 변경에 의존하지 않음 (`holmes` 브랜치에서 독립 분기)
- [ ] worker-agent는 커밋 금지 — 코드 변경만, 커밋은 control-agent가 사용자 승인 후
