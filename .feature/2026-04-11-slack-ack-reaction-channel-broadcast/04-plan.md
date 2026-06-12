# 피처 플랜: Slack 멘션 ACK 리액션 + 채널 공지 옵션 + Progress Pulse

> 작성일: 2026-04-11
> 기반: 01-analysis.md, 02-design.md, 03-review.md
> 원본 사용자 요청:
> - 피처 1: "멘션 시 접수/진행 중 리액션 이모지"
> - 피처 2: "'채널에도 공유해' 발화 시 채널 공지 옵션"
> - 피처 3 (이터레이션 추가): "오래 기다릴 때 progress 메시지 살짝 변경"

## 1. 최종 결정 사항

### 1.1 피처를 두 개의 독립 PR로 분리

**결정**: 설계 문서는 하나로 유지(인프라 공유)하되 PR/배포/롤백을 분리.

**사유**:
- 피처 1 (리액션): 결정적 UX, scope 추가 동반 → reinstall 필요
- 피처 2 (broadcast): 확률적 UX, scope 추가 불필요 → 즉시 배포 가능
- 리스크 프로파일이 다른 두 기능을 같은 PR에 묶으면 하나의 문제로 둘 다 롤백되는 위험
- (PM/DevOps/Backend 컨센서스)

### 1.2 ACK 리액션 발송 경로: 채널 내부 직접 호출

**결정**: `SlackChannel._on_app_mention`에서 `await self._add_reaction(...)`을 직접 호출. `BaseChannel.send_ack/send_done` 인터페이스는 **도입하지 않음**.

**사유**:
- 설계 §3.2(loop.py에서 호출)와 설계 §7(lock 밖 즉시 발송)이 자체 모순
- `loop.py`가 channel 인스턴스를 직접 참조하면 PR#2의 "loop는 bus만 안다" 경계 위반
- lock 밖 즉시 발송은 직전 요청 처리 중일 때도 사용자에게 ACK가 보이는 핵심 가치 — 포기 불가
- (Backend/PM 지적)

### 1.3 done 리액션 발송 경로: metadata-driven OutboundMessage

**결정**: 응답 생성 후 `OutboundMessage.reply_to_inbound(msg, "", extra_metadata={"_reaction_lifecycle": "done_success|done_error"})` 발행. `SlackChannel.send()`이 metadata 보고 리액션 finalize. 빈 content는 "reaction-only message" 의미.

**사유**:
- bus 경계 유지, 순서 보장(FIFO 큐), 테스트 용이
- Telegram 등 다른 채널은 metadata 무시하면 자동 no-op
- `_reaction_lifecycle` 키는 outbound 전용 → `_FORWARDED_METADATA_KEYS`에 추가 X
- (Backend 제안 (1) 채택)

### 1.4 리액션 lifecycle 단순화 (MVP)

**결정**: 2단계 — `eyes` 추가 → 응답 후 `eyes` 제거. **완료 리액션 `white_check_mark`와 에러 리액션은 Phase 2**로 연기.

**사유**:
- UX 지적: `eyes → check` 전환은 사용자 시선 밖(스레드 하단 응답을 보느라 상단 원본 미관찰)
- PM 지적: MVP 스코프 축소, 사용자 원문은 "접수/진행 중 리액션"까지
- 복잡도/API 호출/race 경로 모두 절반으로

### 1.5 텍스트 "생각 중..." 발송을 유지

**결정**: 기존 PR#2의 "생각 중..." 텍스트 메시지 발송 로직은 **그대로 유지**. 리액션은 이 위에 추가되는 기능.

**사유**:
- UX 지적: 리액션은 모바일에서 작고 스크린 리더 미지원 → 접근성 측면에서 텍스트 필수
- 5초 fallback 타이머 복잡도 추가는 YAGNI (PM 지적)
- 사용자가 "리액션만으로 충분하다"고 판단하면 후속 PR에서 텍스트 제거 토글 추가
- lock 밖 즉시 리액션이 "봇이 살았는지 죽었는지" 불안의 핵심 해결이고, 텍스트는 스레드 안 정상 위치(PR#2 fix)에 오니 어수선함 없음

### 1.6 피처 2 트리거: LLM 도구 옵션 + Shadow 감지 로거 병행

**결정**:
- `MessageSender.send(..., share_to_channel: bool = False)` 파라미터 노출
- **Shadow 감지 로거** 추가: inbound에 `채널에도|공지해|공유해|알려|다른 사람도|broadcast|팀에` 등의 문구가 있는데 LLM이 도구를 호출하지 않았으면 WARN 로그 (metric 수집 목적)
- 2주 후 (2026-04-25) metric 리뷰해서 미탐률 측정. 임계값 미달 시 정규식 fallback 도입 검토

**사유**:
- LLM 판단의 비결정성을 관측 가능하게 만드는 유일한 수단 (QA/PM 컨센서스)
- 컨펌 버튼(Block Kit)은 구현 복잡도 높음 → Phase 2로
- (PM/QA/UX 지적)

### 1.7 Feature flag (kill switch) 도입

**결정**: `SlackConfig`에 2개 플래그 추가.

```python
class SlackConfig(Base):
    # ... 기존 필드
    ack_reactions_enabled: bool = False   # 피처 1 마스터
    broadcast_enabled: bool = False        # 피처 2 마스터
```

**사유**:
- 운영 중 문제 발생 시 reinstall 없이 config 한 줄로 off (DevOps 지적)
- default OFF로 머지 → 인스턴스별 단계적 ON → canary 롤아웃
- `scope 없는 인스턴스`는 ack_reactions_enabled=False 유지하면 런타임 에러 회피

### 1.8 Broadcast 안전장치 3중 방어

**결정**: `share_to_channel=True`가 실효하려면 **모두** 만족:
1. `inbound.metadata["is_channel"] == True` (채널)
2. `inbound.metadata["thread_ts"]` 존재 (스레드 컨텍스트)
3. `SlackConfig.broadcast_enabled == True` (인스턴스별 flag)

중 하나라도 False면 `MessageSender.send()`가 warning 로그 + `share_to_channel` 무시 + 반환값에 `"(Note: channel broadcast skipped)"` 힌트 포함 (Claude가 다음 턴에 인지).

**사유**:
- Backend 지적: `is_channel` 체크만으로는 부족 (thread_ts 없으면 reply_broadcast 무의미)
- UX 지적: 사용자에게 silent 실패 방지
- DevOps 지적: 운영자가 인스턴스별 제어 필요

### 1.9 `_active_ack_reactions` 메모리 가드

**결정**: dict 구조를 다음으로 변경.

```python
self._active_ack_reactions: OrderedDict[tuple[str, str], dict] = OrderedDict()
# key: (chat_id, message_ts)
# value: {"added_at": datetime, "task": asyncio.Task}
# Bounded: maxsize=500, TTL=1 hour
```

`send_ack` 호출 시:
1. 현재 size 체크, 500 초과 시 oldest evict
2. TTL 초과 entry 정리 (lazy cleanup)
3. asyncio.Task로 감싸서 저장

`finalize_reaction` 호출 시: `await task` → 순서 보장 → pop.

**사유**:
- Backend: race condition (add 전에 remove 호출) 방지
- DevOps: `Claude CLI hang/timeout`으로 `send_done` 누락 시 메모리 누수 방지
- (Backend 제안 (2) 단순화)

### 1.10 `_dispatch` try/finally로 done reaction 보장

**결정**: `loop.py:_dispatch`에 try/finally 추가. 모든 경로(정상/에러/취소/slash command early return)에서 done 이벤트 발행 보장.

```python
async def _dispatch(self, msg: InboundMessage) -> None:
    success: bool | None = None
    async with self._session_locks[msg.session_key]:
        try:
            response = await self._process_message(msg)
            if response is not None:
                await self.bus.publish_outbound(response)
            success = True
        except asyncio.CancelledError:
            success = False
            raise
        except Exception:
            success = False
            logger.exception("Error processing message for session {}", msg.session_key)
            await self.bus.publish_outbound(
                OutboundMessage.reply_to_inbound(msg, "Sorry, I encountered an error.")
            )
        finally:
            if success is not None and msg.metadata.get("message_ts"):
                lifecycle = "done_success" if success else "done_error"
                await self.bus.publish_outbound(
                    OutboundMessage.reply_to_inbound(
                        msg, "", extra_metadata={"_reaction_lifecycle": lifecycle}
                    )
                )
```

**사유**:
- Backend/QA 지적: 현재 여러 경로에서 done 누락 가능

### 1.11 Graceful error handling 정의 (9가지 에러 코드)

| 에러 코드 | 동작 | 로그 레벨 | 이유 |
|---|---|---|---|
| `already_reacted` | silent, idempotent | DEBUG | 중복 이벤트 정상 처리 |
| `message_not_found` | silent | DEBUG | 사용자가 메시지 삭제 — 정상 상황 |
| `not_reacted` | silent (remove 시) | DEBUG | 이미 제거됨 |
| `no_reaction` | silent | DEBUG | 동일 |
| `not_in_channel` | silent 1회 | WARN | 봇 강퇴 — 이후 동일 채널 메시지도 실패할 것 |
| `invalid_auth`, `missing_scope` | **런타임 비활성화** | ERROR | 설정 실수, 폭발 방지 |
| `ratelimited` | 1회 retry with `Retry-After`, 실패 시 silent | WARN | 피크 대응 |
| Network timeout | 1회 retry, 실패 시 silent | WARN | 일시적 |
| 기타 `SlackApiError` | silent | WARN | 알려지지 않은 에러 방어 |

### 1.12 Broadcast는 마지막 chunk에만 적용

**결정**: `split_message`로 여러 chunk 발송 시, **마지막 chunk에만** `reply_broadcast=True` 전달.

```python
for i, chunk in enumerate(chunks):
    is_last = (i == len(chunks) - 1)
    result = await self._app.client.chat_postMessage(
        channel=msg.chat_id,
        text=chunk,
        thread_ts=thread_ts,
        reply_broadcast=reply_broadcast if is_last else False,
    )
```

**사유**: Backend 지적, 채널 본문 중복 노출 방지

### 1.13 구조화 로그 이벤트

**결정**: 다음 이벤트들을 고정 prefix로 로그.

- `slack.reaction.add` (status, chat_id, message_ts)
- `slack.reaction.remove` (status, chat_id, message_ts)
- `slack.broadcast.sent` (chat_id, thread_ts, content_len) — **INFO 레벨, 감사 목적**
- `slack.broadcast.shadow_miss` (chat_id, content_preview) — Shadow 감지 로거
- `slack.ack_reactions.pending_size` (count) — 5분 주기 gauge

**사유**: DevOps 지적, journalctl grep 기반 운영 환경에서 관찰성 확보

### 1.14a 피처 3 — Progress Pulse (이터레이션 추가)

**결정**: 첫 "생각 중..." 발송 후 5초 간격으로 4번까지 메시지를 edit하여 경과 시간을 표시. 그 이후는 멈춤.

**스케줄**:
| 시각 | 메시지 | 비고 |
|---|---|---|
| t=0 | "생각 중..." | 기존 loop.py ACK (변경 없음) |
| t=5s | "생각 중... (약 5초)" | pulse 1 |
| t=10s | "생각 중... (약 10초)" | pulse 2 |
| t=15s | "생각 중... (약 15초)" | pulse 3 |
| t=20s | "생각 중... (약 20초)" | pulse 4 (마지막) |
| t>20s | (변경 없음) | 메시지는 "생각 중... (약 20초)" 로 고정 |

**사유**:
- 사용자가 "봇이 죽었나?" 불안 해소 (특히 Claude CLI가 30~60초 걸리는 케이스)
- "약 N초" 표시는 정직한 정보 → 사용자가 `!stop` 결정 가능
- 4번 캡으로 `(edited)` 배지 누적 제한 (Slack edit 시 (edited) 표시는 불가피)
- 5분 이상 매우 긴 작업은 자체적으로 다른 신호(중간 progress, 도구 호출 메시지)에 의존

**기존 인프라 재사용**:
- PR#2의 `_progress_messages` 캐시가 이미 `chat.update` 경로 보유 → `_progress=True` metadata로 publish하면 자동 edit됨
- per-thread `asyncio.Lock`도 PR#2에서 도입됨 → race 안전
- 새 Slack scope/API 불필요

**구현 위치**: `loop.py:_process_message` 또는 `_dispatch`에서 pulse task 생성·관리. Slack 외 채널은 metadata 무시하면 자동 no-op.

**Cancel 전략**:
- 응답 도착/에러/`!stop` 시 task `cancel()` + `await task` (최종 메시지 전에 pulse 종료 보장)
- 1.10의 try/finally 구조 안에서 pulse task lifecycle 관리

**Feature flag**: `slack.progress_pulse_enabled: bool = False` (기본 off, 인스턴스별 ON)

### 1.14 이모지 선택

**결정**: 처리 중 = `eyes` (👀). MVP에서는 이것만 사용.

Phase 2에서 완료/에러 이모지 추가 시 후보:
- 완료: `white_check_mark` (✅) — Slack 커뮤니티에서 가장 일반적
- 에러: `warning` (⚠️) — UX 지적: `x`는 peer-to-peer 반대 투표 중첩 회피

## 2. 최종 스코프

### 포함 (Feature 1: Reaction ACK)

- `SlackChannel._add_reaction()`, `_remove_reaction()` 헬퍼 (graceful error handling)
- `SlackChannel._on_app_mention` 및 `_on_message`(DM, thread) 경로에서 **lock 밖 즉시** `_add_reaction("eyes")` 호출
- `SlackChannel.send()`이 `metadata["_reaction_lifecycle"]`을 보고 finalize:
  - `"done_success"` 또는 `"done_error"` → `_remove_reaction("eyes")`
  - content가 빈 문자열이면 reaction-only outbound로 간주, 일반 텍스트 발송 skip
- `_active_ack_reactions: OrderedDict` + TTL + maxsize + `asyncio.Lock`
- `loop.py:_dispatch` try/finally로 done 이벤트 발행 보장
- `SlackConfig.ack_reactions_enabled: bool = False` flag
- Scope 자동 감지 (`missing_scope`/`invalid_auth` → 런타임 비활성화)
- 구조화 로그 이벤트

### 포함 (Feature 3: Progress Pulse)

- `loop.py` (또는 helper 모듈)에 progress pulse async task
- 5초 간격, 최대 4 pulse, 메시지 형식 `"생각 중... (약 N초)"`
- task lifecycle: `_dispatch` try/finally 안에서 cancel + await
- `SlackConfig.progress_pulse_enabled: bool = False` flag
- 기존 `_progress_messages` 캐시 + `chat.update` 경로 재사용 (코드 추가 없이 metadata로 트리거)

### 포함 (Feature 2: Broadcast)

- `MessageSender.send(..., share_to_channel: bool = False)` 파라미터
- 3중 안전장치 (is_channel + thread_ts + config flag)
- `SlackChannel.send()`에서 마지막 chunk에만 `reply_broadcast=True` 전달
- **Shadow 감지 로거**: `core/loop.py` 진입부에서 inbound content 트리거 패턴 매칭 + 처리 완료 후 도구 호출 여부 확인 → 불일치 시 `slack.broadcast.shadow_miss` WARN
- `SlackConfig.broadcast_enabled: bool = False` flag
- `SlackConfig.broadcast_blocked_channels: list[str] = []` (감사 안전장치)
- 구조화 로그 이벤트

### 제외 (후속 PR / 백로그)

**Feature 1 후속:**
- 완료 리액션 (`white_check_mark`) + 에러 리액션 (`warning`) 전환 — Phase 2
- 5초 텍스트 fallback 타이머 + cancel 로직 — 필요 증명 후
- 이모지 config 커스터마이징 (`ack_reactions: dict`) — 필요 증명 후
- `reactions:read` scope + 봇 시작 시 stale 리액션 cleanup — 필요 증명 후

**Feature 2 후속:**
- Block Kit 컨펌 버튼 (`[공유] [취소]`) — 2주 metric 리뷰 후 결정
- 정규식 fallback 트리거 — Shadow 감지 미탐률 측정 후 결정
- Broadcast 메시지에 "@요청자의 질문 답변:" 컨텍스트 프리픽스 — UX 지적
- Role-based broadcast 권한 (`can_broadcast` 플래그) — 사고 발생 후 우선순위 재평가

**공통 후속:**
- Slack chunk split 시 progress 캐시 마지막 chunk override 버그 수정 — 분석 문서에서 별도 언급된 것
- BaseChannel 레벨 리액션 추상화 (Telegram reactions 등 다른 채널 추가 시) — 실제 수요 발생 후
- Telegram `message_threads` fallback 정리 — 별개

## 3. 구현 계획

### PR 1: Feature 1 — ACK 리액션 (MVP)

**브랜치**: `feature/slack-ack-reaction`

**Phase A: 기반**
- [ ] `SlackConfig`에 `ack_reactions_enabled: bool = False` 필드 추가 (`config/schema.py`)
- [ ] `SlackChannel.__init__`에 `_active_ack_reactions: OrderedDict` + `_ack_reactions_lock: asyncio.Lock` + TTL/maxsize 상수
- [ ] `SlackChannel._add_reaction(chat_id, message_ts, name)` 헬퍼 — 9가지 에러 코드 분기, 구조화 로그
- [ ] `SlackChannel._remove_reaction(chat_id, message_ts, name)` 헬퍼 — 동일

**Phase B: Lifecycle**
- [ ] `SlackChannel._on_app_mention`에서 `ack_reactions_enabled` 체크 후 `_add_reaction("eyes")` 직접 호출 (lock 밖, asyncio.create_task로 fire-and-store)
- [ ] `SlackChannel._on_message` DM/thread 경로도 동일 처리
- [ ] `SlackChannel.send()`에 `_reaction_lifecycle` metadata 분기 추가
  - `"done_success"` or `"done_error"` → dict에서 task await → remove eyes → pop
  - content=="" 이면 텍스트 발송 skip
- [ ] Scope 자동 감지 로직: `_add_reaction`에서 `missing_scope`/`invalid_auth` 발생 시 `self._ack_reactions_runtime_disabled = True` 세팅 + ERROR 로그

**Phase C: Loop 통합**
- [ ] `companio/core/loop.py:_dispatch`에 try/finally 추가 → done 이벤트 발행 보장
- [ ] `_process_message` early return 경로(`/new`, `/help`, `/stop`) 에서도 done 이벤트 발행되는지 확인 (try/finally가 커버하는지 검증)

**Phase D: 회귀 가드 + 테스트**
- [ ] `tests/test_slack_reactions.py` 신규:
  - `test_send_ack_calls_reactions_add_with_message_ts`
  - `test_reaction_lifecycle_removes_eyes_on_done_success`
  - `test_reaction_lifecycle_removes_eyes_on_done_error`
  - `test_reactions_add_message_not_found_is_silent` (+ caplog 레벨 검증)
  - `test_reactions_add_invalid_auth_disables_runtime`
  - `test_reactions_add_rate_limited_retries_once`
  - `test_send_ack_idempotent_on_duplicate_event` (Slack retry)
  - `test_two_quick_mentions_have_independent_lifecycles` (다른 message_ts)
  - `test_ack_reactions_disabled_flag_skips_send` (flag=False)
  - `test_ttl_evicts_stale_entries`
  - `test_maxsize_evicts_oldest`
- [ ] `tests/test_dispatch_finally_guarantees_done.py` 신규 (or 기존 파일에 추가):
  - `test_exception_path_publishes_done_error`
  - `test_cancellation_path_publishes_done_error_before_reraise`
  - `test_normal_path_publishes_done_success`
  - `test_early_return_slash_command_still_publishes_done`
- [ ] `tests/test_thread_metadata_propagation.py`에 회귀 가드 추가:
  - `test_reaction_lifecycle_not_in_whitelist`
  - `test_inbound_with_reaction_lifecycle_is_dropped`
- [ ] `tests/test_slack_inbound_metadata.py` grep guard:
  - `test_slack_app_mention_populates_message_ts` (정규식 grep 가드)

**Phase E: Rollout**
- [ ] `docs/ops/slack-reactions-rollout.md` 작성 — scope 추가 절차, reinstall 체크리스트, canary 순서, 롤백
- [ ] 각 Slack App에 `reactions:write` + `reactions:read`(미래 대비) scope 추가 → reinstall
- [ ] canary: `companio-holmes` 인스턴스에 `ack_reactions_enabled: true` 설정 → 24시간 관찰
- [ ] 확산: holmes-family, incheon-family 등 저위험 → SOM Agent, wishket-aidp

### PR 2: Feature 3 — Progress Pulse (MVP)

**브랜치**: `feature/slack-progress-pulse`

**근거**: 피처 1과 같은 "waiting experience" 카테고리. PR 1과 인접 시점 배포 권장. 단, 롤백 독립성 확보 위해 별도 PR.

**Phase A: 기반**
- [ ] `SlackConfig`에 `progress_pulse_enabled: bool = False` 필드 추가
- [ ] 상수 정의: `PULSE_INTERVAL_SECONDS = 5`, `PULSE_MAX_COUNT = 4`, `PULSE_TEXT_FORMAT = "생각 중... (약 {n}초)"`

**Phase B: Pulse Loop**
- [ ] `loop.py`에 helper 메서드 추가:
  ```python
  async def _progress_pulse_loop(self, msg: InboundMessage) -> None:
      """Send up to 4 progress updates at 5-second intervals."""
      try:
          for i in range(1, PULSE_MAX_COUNT + 1):
              await asyncio.sleep(PULSE_INTERVAL_SECONDS)
              elapsed = i * PULSE_INTERVAL_SECONDS
              await self.bus.publish_outbound(
                  OutboundMessage.reply_to_inbound(
                      msg,
                      PULSE_TEXT_FORMAT.format(n=elapsed),
                      extra_metadata={"_progress": True},
                  )
              )
      except asyncio.CancelledError:
          pass  # Cancelled when response arrives
  ```
- [ ] `_dispatch`에서 pulse task 생성 + finally에서 cancel:
  ```python
  pulse_task = None
  if self._should_pulse(msg):  # channel == "slack" and config flag
      pulse_task = asyncio.create_task(self._progress_pulse_loop(msg))
  try:
      ...
  finally:
      if pulse_task:
          pulse_task.cancel()
          try:
              await pulse_task
          except asyncio.CancelledError:
              pass
      # 기존 done lifecycle 발행
  ```

**Phase C: 회귀 가드 + 테스트**
- [ ] `tests/test_progress_pulse.py` 신규:
  - `test_pulse_sends_up_to_4_messages_at_5s_intervals` (asyncio.sleep mock)
  - `test_pulse_cancelled_on_fast_response` (1초 응답 → 0 pulse)
  - `test_pulse_cancelled_on_error` (예외 시 pulse task 정리)
  - `test_pulse_cancelled_on_cancellation` (`/stop` 시)
  - `test_pulse_disabled_flag_skips_loop`
  - `test_pulse_skipped_for_non_slack_channels`
  - `test_pulse_text_format_includes_elapsed_seconds`
  - `test_pulse_max_count_respected` (60초 처리 시에도 4 pulse만)

**Phase D: Rollout**
- [ ] Scope 변경 없음 → 코드 머지만으로 배포 가능
- [ ] canary: `companio-holmes`에 `progress_pulse_enabled: true` 설정 → 24시간 관찰
- [ ] 관찰 항목:
  - `(edited)` 배지가 거슬리는지 사용자 피드백
  - `chat.update` 에러율 (Tier 3 rate limit)
  - pulse 도착 후 최종 응답이 정상 위치에 오는지 (cancel 검증)

### PR 3: Feature 2 — Broadcast (MVP)

**브랜치**: `feature/slack-broadcast`

**Phase A: 기반**
- [ ] `SlackConfig`에 `broadcast_enabled: bool = False` + `broadcast_blocked_channels: list[str] = []` 필드 추가
- [ ] `OutboundMessage.metadata["reply_broadcast"]` 컨벤션 문서화 (주석만)

**Phase B: Tool & Channel**
- [ ] `MessageSender.send()`에 `share_to_channel: bool = False` 파라미터 추가
- [ ] 3중 안전장치: `is_channel`, `thread_ts` 존재, config flag 체크. 미충족 시 WARN 로그 + reply string에 `"(Note: channel broadcast skipped)"` 추가
- [ ] `SlackChannel.send()`에서 마지막 chunk에만 `reply_broadcast=True` 전달 (for loop index 체크)
- [ ] `broadcast_blocked_channels`에 포함된 chat_id는 reply_broadcast 강제 False + WARN 로그

**Phase C: Shadow 감지**
- [ ] `loop.py:_process_message` 진입 시점에 inbound content에 트리거 패턴 매칭 (`re.search`)
  - 패턴: `(채널에도|공지해|공유해|알려.*다른|다른 사람도|broadcast|everyone|@channel)`
- [ ] 트리거 매칭됐는데 MessageSender가 `share_to_channel=True`로 호출되지 않았으면 → `slack.broadcast.shadow_miss` WARN 로그 (chat_id, content_preview 포함)
- [ ] 구현 방식: `loop.py`에 `_shadow_trigger_matched: bool` 플래그 세팅 → `send_callback`에서 도구 호출 여부 관찰 → 처리 종료 시점에 판정

**Phase D: 테스트**
- [ ] `tests/test_message_sender_broadcast.py` 신규:
  - `test_share_to_channel_sets_reply_broadcast_metadata`
  - `test_share_to_channel_ignored_in_dm` (is_channel=False)
  - `test_share_to_channel_ignored_without_thread_ts`
  - `test_share_to_channel_ignored_when_flag_disabled`
  - `test_share_to_channel_blocked_channel`
  - `test_skip_hint_in_return_value_when_ignored`
- [ ] `tests/test_slack_broadcast.py` 신규:
  - `test_slack_channel_passes_reply_broadcast_to_api`
  - `test_slack_channel_drops_broadcast_when_not_channel`
  - `test_slack_channel_broadcasts_only_last_chunk`
  - `test_broadcast_logged_at_info_level` (caplog)
- [ ] `tests/test_broadcast_shadow_detection.py` 신규:
  - `test_shadow_logger_warns_on_unmatched_trigger`
  - `test_shadow_logger_silent_when_tool_used`
- [ ] `tests/test_thread_metadata_propagation.py`에 회귀 가드:
  - `test_reply_broadcast_not_in_whitelist`
  - `test_inbound_with_reply_broadcast_is_dropped`

**Phase E: Rollout**
- [ ] Scope 변경 없음 → 코드 머지만으로 배포 가능
- [ ] canary: `companio-holmes`에 `broadcast_enabled: true` 설정 → 24시간 관찰
- [ ] 2주 후 (2026-04-25) metric 리뷰:
  - `slack.broadcast.sent` 발생 횟수
  - `slack.broadcast.shadow_miss` 발생 횟수
  - 미탐률 = shadow_miss / (shadow_miss + sent)
  - 임계값: 미탐률 > 30% 이면 정규식 fallback 도입 검토

### PR 4: 사용자 공지 (선택, PR 1·2·3 배포 후)

- [ ] 각 인스턴스 대표 채널에 1회 공지 메시지 송신 스크립트
- [ ] 공지 문구:
  > "@companio 업데이트 — 이제 멘션하면 즉시 👀 리액션으로 접수 확인을 드립니다. 그리고 답변을 '채널에도 공유해'라고 하면 스레드 답변이 채널 본문에도 함께 노출됩니다."
- [ ] companio-holmes의 `/help` 응답에 사용법 추가

## 4. 리스크 및 대응

| 리스크 | 가능성 | 영향 | 대응 |
|---|---|---|---|
| Scope 누락 상태로 배포 → 첫 멘션에 `missing_scope` 에러 | 중 | 중 | 런타임 자동 비활성화 + ERROR 로그. ack_reactions_enabled flag도 함께 동작 |
| `_active_ack_reactions` 메모리 누수 (send_done 누락) | 저 | 저 | TTL + maxsize + try/finally 3중 방어 |
| ACK 리액션과 done 리액션 순서 역전 (add 전에 remove) | 중 | 저 | dict value를 asyncio.Task로 → `await task` 후 remove |
| LLM이 민감 정보 `share_to_channel=True`로 broadcast | 저 | 높 | 3중 안전장치 + blocked_channels + 감사 로그. Phase 2 컨펌 버튼 도입 |
| LLM이 트리거를 아예 안 잡음 (피처 2 미탐) | 높 | 저 | Shadow 감지 로거로 2주 측정 → 정규식 fallback 결정 |
| Broadcast 메시지가 chunk split되면 중복 노출 | 중 | 중 | 마지막 chunk에만 `reply_broadcast=True` |
| 사용자가 트리거 문구를 몰라서 피처 2 사용 0건 | 높 | 저 | 릴리스 공지 메시지 + `/help` 업데이트 |
| Pulse `(edited)` 배지가 시각적 노이즈 | 중 | 저 | 4번 캡으로 제한, 사용자 피드백 보고 비활성화 가능 |
| Pulse cancel 누락 → 최종 응답 후 "생각 중..." 등장 | 저 | 중 | `_dispatch` try/finally + `await task` 패턴, 단위 테스트 필수 |
| Pulse `chat.update` rate limit | 저 | 저 | Tier 3 50/min, 세션당 4회 캡으로 자연 cap. maxConcurrent=5에서 worst-case 20/min |
| Slack rate limit (Tier 2, 피크 시간) | 저 | 저 | 1회 retry + silent fail. 2주 관찰 |
| 봇 재시작 시 고아 👀 리액션 | 중 | 저 | "허용 가능한 손실". 필요 시 startup cleanup hook을 Phase 2에 |
| `reactions:write` scope 조직 보안 승인 지연 | 중 | 중 | rollout runbook에 사전 승인 절차 명시. canary는 내부 인스턴스부터 |

## 5. 검증 계획

### 단위 테스트 (CI 머지 게이트)

Phase D의 모든 테스트 파일. 결정적/관측 가능한 동작만 검증.

### Integration / E2E (수동, canary 배포 후)

**PR 1 Acceptance Criteria:**
- [ ] AC1 — Slack 채널에서 봇 멘션 시 500ms 이내 👀 리액션 등장
- [ ] AC2 — 응답 완료 후 👀 제거됨
- [ ] AC3 — DM에서 동일 동작
- [ ] AC4 — 직전 요청 처리 중에도 새 멘션에 👀 즉시 달림 (lock 회피 검증)
- [ ] AC5 — 멘션 후 메시지 삭제 시 봇 크래시 없음 (silent)
- [ ] AC6 — scope 누락 인스턴스에서 ERROR 로그 + 봇 running 유지
- [ ] AC7 — `ack_reactions_enabled=False` 인스턴스는 리액션 발송 안 함
- [ ] AC8 — 빠른 연속 2번 멘션 → 각자 독립 lifecycle
- [ ] AC9 — Claude CLI 처리 중 에러 발생 시에도 👀 제거됨 (try/finally 검증)
- [ ] AC10 — `/stop` 명령으로 취소 시에도 👀 제거됨

**PR 2 Acceptance Criteria (Progress Pulse):**
- [ ] AC11p — 멘션 후 30초 이상 걸리는 작업에서 5/10/15/20초 시점에 메시지 텍스트가 "생각 중... (약 N초)" 로 변경됨
- [ ] AC12p — 4번 pulse 후 더 이상 변경 없음 (20초에 멈춤)
- [ ] AC13p — 빠른 응답(<5초)에서는 pulse 0회, 메시지가 그대로 최종 응답으로 대체됨
- [ ] AC14p — `!stop` 명령 시 pulse 즉시 정지
- [ ] AC15p — `progress_pulse_enabled=False` 인스턴스는 pulse 발송 안 함
- [ ] AC16p — pulse 도중 Claude 에러 발생 시 pulse 정지 + 에러 메시지가 최종 위치에 정상 표시

**PR 3 Acceptance Criteria (Broadcast):**
- [ ] AC21 — 채널 스레드에서 "이거 채널에도 공유해" → 채널 본문에 "Also sent to #channel" + 답변 노출
- [ ] AC22 — DM에서 같은 발화 → broadcast 무시, 답변에 skip 힌트 포함
- [ ] AC23 — 채널 루트(스레드 아닌 메시지)에서 같은 발화 → broadcast 무시
- [ ] AC24 — `broadcast_enabled=False` 인스턴스에서는 무시
- [ ] AC25 — `broadcast_blocked_channels`에 포함된 채널은 강제 무시
- [ ] AC26 — 긴 응답(>3000자) 시 마지막 chunk만 broadcast, 나머지는 일반 thread reply
- [ ] AC27 — `slack.broadcast.sent` INFO 로그에 chat_id/thread_ts/content_len 기록
- [ ] AC28 — Shadow 감지: "채널에도 공유해"라고 했는데 Claude가 도구 미호출 → `slack.broadcast.shadow_miss` WARN 로그

### 메트릭 리뷰 (2주 후, 2026-04-25)

- [ ] PR 3: `slack.broadcast.sent` 발생 ≥ 1 (성공 게이트). 0건이면 실패 선언 + Phase 2 정규식 fallback 결정.
- [ ] PR 3: 미탐률 = `shadow_miss / (shadow_miss + sent)` 측정. >30% 면 fallback 도입.
- [ ] PR 1: `slack.reaction.add` 에러율 < 1%. 초과 시 원인 분석 (scope 상태, rate limit, 네트워크).
- [ ] PR 1: `slack.ack_reactions.pending_size` 평균 < 10. 누수 감지.
- [ ] PR 2: `chat.update` 에러율 < 1%. `(edited)` 배지에 대한 사용자 피드백 수집.

## 6. 리뷰 반영 이력

| 피드백 | 출처 | 반영 여부 | 사유 |
|---|---|---|---|
| 두 피처 분리 (PR/배포) | PM, DevOps, Backend | ✅ | 리스크 프로파일 다름, 롤백 독립성 필요 |
| MVP 2단계 리액션 (`eyes`만, ✅/❌ 제거) | PM, UX | ✅ | 사용자 원문 범위, YAGNI |
| 5초 텍스트 fallback 제거 | PM, UX | ✅ | YAGNI, 기존 "생각 중..." 유지 |
| BaseChannel.send_ack 도입 대신 metadata 기반 | Backend, QA | ✅ | bus 경계 유지, 순서 보장, 테스트 용이 |
| ACK 리액션은 `_on_app_mention`에서 직접 | Backend, PM | ✅ | 설계 자체 모순 해소, lock 회피 |
| Feature flag (kill switch) | DevOps, PM | ✅ | 운영 롤백 수단 |
| `_active_ack_reactions` TTL/maxsize | Backend, DevOps, QA | ✅ | 메모리 누수 방지 |
| `_dispatch` try/finally | Backend, QA | ✅ | done 누락 경로 차단 |
| 9가지 에러 코드 명시 | QA, Backend | ✅ | 표로 잠금 |
| Broadcast 마지막 chunk만 | Backend | ✅ | 중복 노출 방지 |
| 3중 안전장치 (is_channel + thread_ts + flag) | Backend, DevOps | ✅ | 방어 심도 |
| Shadow 감지 로거 | PM, QA | ✅ | 피처 2 관측성의 유일한 수단 |
| 구조화 로그 이벤트 | DevOps | ✅ | 운영 관찰성 |
| 운영자 runbook | DevOps, PM | ✅ | Phase E 산출물 |
| 사용자 공지 계획 | PM | ✅ | PR 3으로 분리 |
| `reactions:read` 함께 추가 | DevOps | ✅ | 미래 재작업 회피 |
| Block Kit 컨펌 버튼 (broadcast) | UX | ⏸️ 보류 | Phase 2, 2주 metric 리뷰 후 결정 |
| 완료/에러 리액션 전환 (✅/❌) | 설계 원안 | ⏸️ 보류 | MVP 스코프 밖, 필요 증명 후 |
| 리액션이 모바일에서 작아 안 보임 | UX | ⚠️ 인지 | 텍스트 "생각 중..." 유지로 완화. 이후 메트릭 보고 결정 |
| 스레드 밖 observer 컨텍스트 프리픽스 | UX | ⏸️ 후속 | Phase 2 |
| Role-based broadcast 권한 | Backend | ⏸️ 후속 | `broadcast_blocked_channels`로 우선 대응. 사고 발생 시 우선순위 재평가 |
| LLM trigger eval 스크립트 (20 pos + 20 neg) | QA | ⏸️ 후속 | CI 외 주기 스크립트, Phase 2 |
| 봇 시작 시 stale 리액션 cleanup | DevOps | ⏸️ 후속 | `reactions:read` scope 필요, 실제 문제 발생 후 |
| ❌ 아이콘 대신 `warning` | UX | ⏸️ 후속 | Phase 2에 적용 |
| Broadcast 컨펌 질문 (애매할 때) | UX | ⏸️ 후속 | Phase 2 |
| Progress pulse (5s × 4회 경과시간 표시) | 사용자 이터레이션 추가 | ✅ | 피처 3으로 별도 PR. 기존 `_progress_messages` 캐시 재사용, scope 추가 없음 |
