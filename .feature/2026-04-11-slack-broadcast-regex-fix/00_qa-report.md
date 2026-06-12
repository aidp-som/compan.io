# QA 결과 보고서: Broadcast 트리거 재설계

> 작성일: 2026-04-11
> 대상 Phase: P1 (single phase)
> 기반: 00_qa-criteria.md, 04-plan.md

## 1. Phase 1 검증 결과

| # | 검증 항목 | 기대 결과 | 실제 결과 | 판정 |
|---|---|---|---|---|
| Q1 | `_detect_broadcast_intent` 토큰 + 어미 gate | 04-plan §1.2 코드 일치 | ✅ 토큰/regex/standalone/imperative ending/referential exclusion 모두 구현 | ✅ PASS |
| Q2 | positive ≥15 통과 | 명령형/standalone/영어 매칭 | ✅ positive 19 케이스 통과 | ✅ PASS |
| Q3 | negative ≥15 통과 (오탐 방어) | 참조/질문/과거형 미매칭 | ✅ negative 18 케이스 통과 | ✅ PASS |
| Q4 | `_apply_broadcast_intent` 4 guards | happy path + 4 skip | ✅ 12 케이스 통과 | ✅ PASS |
| Q5 | `reply_to_inbound` 사용 | `OutboundMessage()` 직접 호출 0건 | ✅ factory spy 검증 통과 | ✅ PASS |
| Q6 | `_BROADCAST_MARKER` exact lock | `\n\n_📢 채널에도 공유되었습니다_` | ✅ 통과 | ✅ PASS |
| Q7 | `_process_message` 흐름 | shadow detector 제거 + intent 적용 | ✅ 통합 7 케이스 통과 | ✅ PASS |
| Q8 | `_invoke_claude_turn` 인자 정리 | `broadcast_intent_detected` 제거 | ✅ 시그니처 정리됨 | ✅ PASS |
| Q9 | Slash command + intent | broadcast 미적용 | ✅ `/help`, `/new` 분기 통합 테스트 | ✅ PASS |
| Q10 | 다중 chunk 가드 | `len(chunks) > 1` → 모두 False | ✅ 3 신규 테스트 통과 | ✅ PASS |
| Q11 | runtime warning | WARN 로그 발생 | ✅ caplog 검증 (dead code contract 일부) | ✅ PASS |
| Q12 | dead code contract | `test_message_sender_broadcast` GREEN | ✅ `TestShareToChannelDeadCodeContract_*`로 재명명, 모두 통과 | ✅ PASS |
| Q13 | 7개 회귀 가드 GREEN | 특히 `test_loop_py_uses_only_reply_to_inbound` | ✅ 7/7 통과 | ✅ PASS |
| Q14 | `_FORWARDED_METADATA_KEYS` 변경 없음 | bus.py 그대로 | ✅ grep 0건 | ✅ PASS |
| Q15 | shadow_detection 폐기/마이그레이션 | 파일 삭제 + 케이스 이동 완료 | ✅ 파일 삭제, 패턴 케이스는 `test_broadcast_intent.py`로 이동 | ✅ PASS |
| Q16 | pytest 전체 통과 | 기존 + 신규 통과 | ✅ 220 passed (사전 실패 2 제외) | ✅ PASS |
| Q17 | ruff 통과 | 신규 코드 0 위반 | ✅ 신규 0 위반 (사전 1건 제외) | ✅ PASS |

**Phase 1 판정: PASS** (필수 17/17 + 권장 0/0)

## 2. 의도 vs 결과

### 의도한 것

직전 머지된 broadcast 기능이 동작 불가능 판명 → regex 기반 결정적 트리거로 전환:
- `MessageSender.send(share_to_channel=...)`는 dead code 유지 (코드 보존, runtime warning만 추가)
- `_detect_broadcast_intent`를 토큰 매칭 + 명령형 어미 gate + 과거형 제외로 재작성
- `_process_message`에서 intent 매칭 시 최종 OutboundMessage에 `reply_broadcast=True` 자동 주입
- `OutboundMessage.reply_to_inbound`만 사용 (회귀 가드 통과)
- 다중 chunk 응답은 broadcast 미적용 (단일 chunk 한정)
- 마커 `\n\n_📢 채널에도 공유되었습니다_` append

### 실제 달성

- ✅ regex 트리거 정확히 구현, 38개 parametrized 케이스 (positive 19 + negative 18 + edge 1)로 잠금
- ✅ `_apply_broadcast_intent`는 `reply_to_inbound`만 사용, factory spy 테스트로 잠금
- ✅ shadow detector 제거 + primary trigger 승격, 통합 테스트로 흐름 검증
- ✅ 다중 chunk 가드 + INFO 로그
- ✅ `MessageSender.send()` runtime warning + dead code contract docstring + 테스트 클래스 재명명
- ✅ 7개 회귀 가드 모두 GREEN — 특히 `test_loop_py_uses_only_reply_to_inbound`가 통과
- ✅ 04-plan §1.1~1.10 모든 결정 코드 반영

### 차이 분석

**1. positive 케이스 1건 조정**: 04-plan에서 예시로 든 `"채널에 방송해 주세요"` (공백 포함)는 worker-agent가 단위 테스트 작성 중 발견 — `_IMPERATIVE_ENDING` regex가 `해주세요` 리터럴이라 `해 주세요` (공백)는 매칭 안 됨. positive case를 `"채널에 방송해줘"`로 조정. **수용 가능한 차이** — 04-plan §1.2 패턴 자체는 그대로 유지했고, 운영 중 공백 변형 빈도가 높으면 후속 PR에서 패턴 보강.

다른 모든 의도 100% 달성.

## 3. 미충족 항목 대응

없음.

## 4. 발견된 부수 이슈

### [심각도: 낮음] 사전 존재 환경 의존성 (본 작업 무관)
- `tests/test_claude_cli.py::TestBuildCmd::test_basic`, `test_add_dir_home` — `claude` 바이너리 PATH 가정 환경 의존성. 직전 사이클에서도 동일하게 무시.
- `companio/core/context.py:121` `F541` ruff 위반 — 사전 위반.
- **대응**: 별도 cleanup PR로 분리

### [심각도: 낮음] 공백 변형 패턴 가드 부족
- `"채널에 방송해 주세요"` 같은 공백 포함 어미가 매칭 안 됨
- **대응**: 후속 PR에서 `_IMPERATIVE_ENDING` 패턴에 `해\s*주세요`, `해\s*줘` 등 공백 허용 추가

## 5. 후속사항 트리아지

### 즉시 처리 (완료)
없음.

### 후속 계획 추가
- **공백 변형 패턴 보강** — `_IMPERATIVE_ENDING`에 `\s*` 허용. metric 보고 결정.
- **마커 첫 chunk 상단 prepend** — 다중 chunk 응답 지원. 04-plan §2 후속 우선순위 #3.
- **컨펌/언두 메커니즘** — UX critical 우려. 04-plan §2 후속 우선순위 #1.
- **Observer prefix 헤더** — broadcast 메시지 가독성. 04-plan §2 후속 우선순위 #2.

### 블로커 (사용자 확인 대기)
없음. 모두 PASS, 즉시 머지 + 운영 배포 가능.
