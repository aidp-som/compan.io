# 페르소나 리뷰: Broadcast 트리거 재설계 (LLM tool → regex)

> 리뷰일: 2026-04-11
> 리뷰 깊이: deep (Backend, QA), shallow (UX)
> 기반: 01-analysis.md, 02-design.md

## 페르소나 선정 근거

좁은 스코프(regex 패턴 + 트리거 위치 변경)이라 3명만:
- **Backend**: 구현 정확성, 회귀 가드, mutation 안전성
- **QA**: 결정적 테스트 가능성, 회귀 가드 잠금, 16-vs-8 케이스 균형
- **UX**: 비가역 액션의 사용자 인지·신뢰, 마커 위치, 미탐/오탐 복구

DevOps/PM은 직전 사이클에서 충분히 검토했고 본 fix는 운영 절차나 스코프 영향이 거의 없어 생략.

---

## 평가 요약

| 페르소나 | 평가 | 핵심 지적 |
|---|---|---|
| Backend | 🟡 | 회귀 가드 위반 위험, chunk-split + marker 정합성 미보장, 토큰 매칭 오탐 확대 |
| UX | 🟡 | 비가역 액션에 언두 없음, 마커 하단 배치, 신뢰 붕괴 1회 시 침묵 회피, 멘탈 모델 학습 vs regex 한계 |
| QA | 🟡 | `OutboundMessage(...)` 직접 생성자 → 기존 회귀 가드 즉시 위반, 16개 guard 조합 → 8개 핵심으로, 마커 텍스트 exact lock 테스트 필요 |

---

## 페르소나별 핵심 피드백

### Backend 핵심
1. **🔴 Chunk-split 실효성 구멍**: 마커는 `response.content` 끝에 붙고 `reply_broadcast=True`는 마지막 chunk에만 적용됨. 긴 응답이 N chunks로 split되면 채널 본문에 노출되는 것은 마지막 chunk(꼬리 + 마커)뿐. observer는 맥락을 잃음. **해결: 단일 chunk 응답에서만 broadcast 적용** (`slack.py`에 `len(chunks) == 1` 가드 추가) 또는 마커를 첫 chunk로 prepend.
2. **🔴 `OutboundMessage` 신규 인스턴스 → mutation으로**: 02-design §3.2의 코드 샘플은 `OutboundMessage(channel=..., chat_id=..., ...)`로 생성. 이는 `reply_to` 등 필드 유실 위험. **해결: shallow-copy mutation** 또는 `reply_to_inbound()` 재사용.
3. **🟡 토큰 매칭 오탐 확대**: `has_channel and has_verb` proximity-free → "이 채널에서 어제 공지 본 거 요약해줘" 같은 참조 발화도 매칭. **해결: 명령형 어미 gate** (`해`/`해줘`/`주세요` 끝남) + 영어 bigram 요구 (`to channel` / `share with channel`) + 과거 시제 제외 힌트 (`떴`, `됐`, `있어`).
4. **🟡 `chat_id` 정규화**: blocked_channels가 `#channel-name` vs `C0123ABC` 혼용 가능. config schema docstring에 "channel ID만 사용" 명시 + 비교 시 정규화.
5. **🟡 `share_to_channel` runtime warning 추가**: dead code 정리에 docstring만 부족. `if share_to_channel: logger.warning("dead code path...")` 런타임 가시화.
6. **🟢 `broadcast_intent_detected` 인자 제거 안전**: `_invoke_claude_turn`에서 이미 `del`로 unused. 외부 호출자 없음.

### UX 핵심
1. **🔴 비가역 액션 + 언두 부재**: 오탐 1회 = 팀 전체 오발송. 마커 1개로 감당 불가.
2. **🔴 오탐 사용자 복구 경로 없음**: 사용자가 마커를 봐도 할 수 있는 게 수동 삭제뿐.
3. **🔴 토큰 조합 매칭 오탐률 과소평가**: 슬랙 멘션 맥락에서 "채널" + verb 토큰은 흔한 조합. 오탐률 "중"이 아니라 "높".
4. **🟡 마커 위치가 응답 하단**: 긴 브리핑은 끝까지 스크롤해야 마커 인지. 비가역 액션 인지가 너무 늦음. **상단 prepend** 권장.
5. **🟡 멘탈 모델 학습 vs regex 한계**: "한 번 성공 → LLM처럼 이해하리라 믿음 → regex 한계로 배신" 패턴. 신뢰 붕괴 1회로 사용자가 모든 channel 언급 회피하게 됨.
6. **🟡 Observer 맥락 부족**: broadcast 본문만 본 팀원은 "누가 왜 물어봤는지" 모름. **prefix 헤더** 권장: `_📢 @user1 요청으로 스레드에서 공유됨_`.
7. **🟡 미탐 사용자 신호 없음**: guard로 skip되면 INFO 로그만. 사용자는 "왜 안 됐지?" 모름. **스레드 내 조용한 각주** (`_(채널 공유 요청 감지 — DM이라 생략)_`).
8. **🟢 마커 한국어 고정**: 영어 trigger 매칭하면 마커도 영어로 분기 필요.

### QA 핵심
1. **🔴 `OutboundMessage(...)` 직접 생성자 → 즉시 회귀 가드 위반**: `test_loop_py_uses_only_reply_to_inbound`가 `loop.py`에서 bare 생성자를 금지. 02-design §3.2 코드 샘플은 머지 즉시 RED. **반드시 `reply_to_inbound()` 또는 `dataclasses.replace()` 또는 mutation** 사용.
2. **🔴 negative 케이스 부재**: 01-analysis §2의 오탐 예시(`"이 채널에 PR 공지가 떴는데"`)가 그대로 남음. parametrized negative test로 잠금 필수.
3. **🟡 4-guard 조합 → 8 핵심 케이스**: 16 = 2^4 전체는 fragile. 우선순위 순서 검증 + 단독 fail 4개 + happy path + bypass = **8 케이스**가 의미 있음. 우선순위 불변조건 docstring 명시.
4. **🟡 chunk split × marker 정합성 테스트 필수**: `test_broadcast_marker_lands_in_last_slack_chunk` (또는 단일 chunk 가드 채택 시 그에 맞는 테스트).
5. **🟡 `_BROADCAST_MARKER` exact-string lock**: 의도치 않은 변경 방지.
6. **🟡 `response is None` 분기 명시화**: `_sent_in_turn=True` (dead code) + slash command + 에러 경로 — 각 경로에서 intent 적용 안 됨을 테스트로 잠금.
7. **🟡 기존 테스트 처분 결정**:
   - `test_shadow_logger_warns_on_unmatched_trigger` → 폐기/재작성 (`test_auto_triggered_logs_on_intent`로)
   - `test_shadow_logger_silent_when_tool_used` → 폐기 (LLM 경로 dead code)
   - `test_message_sender_broadcast.py::*` → "DEAD CODE CONTRACT" 클래스명으로 재명명, docstring 명시
8. **🟡 slash command + intent 복합**: `/help 채널에도 공유해` → broadcast 미적용 검증 테스트
9. **🟢 `방송해주세요` 케이스 재분류**: 토큰 기반에서 채널 토큰 없으면 매칭 안 됨. standalone 리스트에 추가.

---

## 크로스 커팅 분석

### 공통 우려 (3명 중 2명 이상)

| 주제 | 페르소나 |
|---|---|
| chunk-split + 마커 정합성 (실효성 구멍) | Backend, UX, QA |
| 토큰 매칭 오탐 확대 / negative 가드 부족 | Backend, UX, QA |
| `OutboundMessage` 신규 생성자 vs mutation/`reply_to_inbound` | Backend, QA |
| 마커 위치/포맷 (하단 vs 상단, observer 맥락) | UX (Backend는 부수적 언급) |
| `MessageSender.send()` dead code 잠금 부재 | Backend, QA |
| `response is None` 분기 명시 | Backend, QA |
| slash command + intent 분기 | Backend, QA |

### 의견 충돌

| 쟁점 | 입장 A | 입장 B | 해소 |
|---|---|---|---|
| 마커 위치 | UX: 첫 chunk 상단 prepend | Backend: 마지막 chunk가 broadcast되니 정합 필요 | **마커는 상단 prepend + broadcast는 첫 chunk에만 적용** (둘 다 해결) |
| chunk-split 처리 | Backend: 단일 chunk만 broadcast 허용 | UX: 첫 chunk 상단 prepend로 해결 | 둘 다 채택 — 단순한 운영을 위해 **MVP는 단일 chunk만 broadcast**, 마커 상단 prepend는 후속 |
| `share_to_channel` dead code | Backend: runtime warning 추가 | QA: dead code contract 테스트로 잠금 | 둘 다 채택 |
| 컨펌 단계 | UX: 언두 또는 컨펌 reaction 사전 필수 | (Backend·QA 의견 없음) | MVP는 마커 + 미탐 각주만, 컨펌/언두는 후속 PR |

### 컨센서스 (필수 반영)

1. **`OutboundMessage` 직접 생성자 금지** — `reply_to_inbound()` 사용 또는 mutation으로 회귀 가드 통과
2. **chunk-split에서 marker + broadcast 정합 보장** — 가장 안전한 방법은 **단일 chunk 응답에서만 broadcast 적용**. 다중 chunk면 intent skipped (`reason=chunked`).
3. **negative test parametrized 잠금** — 01-analysis §2의 오탐 예시들을 모두 negative case로 추가, 명령형 어미 gate 추가
4. **`response is None`, slash command 분기 명시 + 테스트**
5. **`_BROADCAST_MARKER` exact-string lock 테스트**
6. **`MessageSender.send(share_to_channel=True)` runtime warning 로그**
7. **기존 회귀 가드 7개 모두 GREEN 유지**:
   - `test_whitelist_is_the_source_of_truth`
   - `test_reply_broadcast_not_in_whitelist`
   - `test_inbound_with_reply_broadcast_does_not_leak`
   - `test_loop_py_uses_only_reply_to_inbound`
   - `test_slack_send_broadcasts_only_last_chunk` (또는 단일 chunk 가드 채택 시 적절히 수정)
   - `test_slack_send_drops_broadcast_when_not_channel`
   - `test_slack_send_blocked_channel_strict_drop`
8. **8 핵심 guard 케이스** (16 전수 X)
9. **운영 검증 11개 E2E** (QA 리뷰 §운영 검증 체크리스트)

### 컨센서스 (보류 — 후속)

- 마커 첫 chunk 상단 prepend (단일 chunk 가드 채택 시 자동 해결, 다중 chunk 케이스는 후속 PR에서)
- 컨펌 reaction / 언두 메커니즘
- Observer prefix 헤더 (`_📢 @user1 요청으로...`)
- 미탐 시 스레드 각주
- 영어 마커 분기 (i18n)
- `share_to_channel` 완전 제거
