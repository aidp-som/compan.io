# QA 기준: Broadcast 트리거 재설계

> 작성일: 2026-04-11
> 기반: 00_overview.md, 04-plan.md §5

## Phase 1: Regex 트리거

| # | 검증 항목 | 기대 결과 | 검증 방법 | 우선순위 |
|---|---|---|---|---|
| Q1 | `_detect_broadcast_intent` 토큰 매칭 + 명령형 어미 gate | 04-plan §1.2 코드와 일치 | grep + 단위 테스트 | 필수 |
| Q2 | positive 케이스 ≥15 통과 | 모든 명령형/standalone/영어 발화 매칭 | parametrized 단위 테스트 | 필수 |
| Q3 | negative 케이스 ≥15 통과 (오탐 방어) | 참조/질문/과거형 발화 미매칭 | parametrized 단위 테스트 | 필수 |
| Q4 | `_apply_broadcast_intent` 4 guards | 모두 통과 시 marker + reply_broadcast=True | 8 핵심 케이스 단위 테스트 | 필수 |
| Q5 | `_apply_broadcast_intent`가 `reply_to_inbound` 사용 | `OutboundMessage()` 직접 호출 0건 | 단위 테스트 spy | 필수 |
| Q6 | `_BROADCAST_MARKER` exact string | `\n\n_📢 채널에도 공유되었습니다_` | exact lock 단위 테스트 | 필수 |
| Q7 | `_process_message` 흐름: shadow detector 제거 + intent 적용 | 04-plan §1.6 | grep + 통합 테스트 | 필수 |
| Q8 | `_invoke_claude_turn`에서 `broadcast_intent_detected` 인자 제거 | 시그니처 정리 | grep | 필수 |
| Q9 | Slash command + intent → broadcast 미적용 | early return 경로 | 통합 테스트 | 필수 |
| Q10 | `SlackChannel.send` 다중 chunk 가드 | `len(chunks) > 1` → broadcast 모두 False | 단위 테스트 (split 케이스) | 필수 |
| Q11 | `MessageSender.send(share_to_channel=True)` runtime warning | WARN 로그 발생 | caplog 단위 테스트 | 권장 |
| Q12 | dead code contract 보존 | `tests/test_message_sender_broadcast.py` 모두 GREEN | pytest | 필수 |
| Q13 | 7개 회귀 가드 GREEN | 특히 `test_loop_py_uses_only_reply_to_inbound` | pytest | 필수 |
| Q14 | `_FORWARDED_METADATA_KEYS` 변경 없음 | bus.py:12-19 그대로 | grep + 단위 테스트 | 필수 |
| Q15 | `tests/test_broadcast_shadow_detection.py` 폐기 또는 마이그레이션 | 파일 삭제 또는 케이스 이동 완료 | 파일 존재/내용 확인 | 필수 |
| Q16 | pytest 전체 통과 | 기존 + 신규 통과 | `pytest tests/` | 필수 |
| Q17 | ruff 통과 | 신규 코드 0 위반 | `ruff check` | 필수 |

## 전체 품질 기준

### 기능적

- [ ] 04-plan.md §2 "포함" 항목 모두 코드로 구현
- [ ] negative test 잠금: 01-analysis §2의 오탐 예시 모두 negative case로 추가
- [ ] positive test 잠금: 원 버그 케이스(`채널 본문에도 브리핑해`) 포함

### 비기능적

- [ ] pytest 전체 통과 (사전 실패 2건 무시 가능)
- [ ] ruff check 통과 (사전 위반 1건 무시 가능)
- [ ] 7개 회귀 가드 GREEN (Phase 1 §Q13)
- [ ] `_FORWARDED_METADATA_KEYS` 변경 없음
- [ ] PR#2 패턴(`reply_to_inbound`, per-thread Lock, `_progress_messages`) 우회 금지
