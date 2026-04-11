# WO-P1-01: Slack Reaction ACK 구현

> 대상 에이전트: **compan-io-agent** (compan.io)
> 우선순위: P1
> 상태: ⬜ 대기
> 생성일: 2026-04-11
> 완료일: -

## 1. 목적

Slack 멘션 시 사용자 메시지에 `eyes` 리액션을 즉시(lock 밖) 발송하고, 응답 완료/에러/취소 시 제거하는 기능을 구현한다. 04-plan.md PR 1 (Feature 1)의 모든 결정 사항을 따른다.

## 2. 컨텍스트

**필독 문서:**
- `/home/holmes/compan.io/.feature/2026-04-11-slack-ack-reaction-channel-broadcast/04-plan.md` — 결정 사항 §1.1~1.14, 구현 계획 PR 1, 검증 §5
- `/home/holmes/compan.io/.feature/2026-04-11-slack-ack-reaction-channel-broadcast/02-design.md` — 컴포넌트 설계
- `/home/holmes/compan.io/.feature/2026-04-11-slack-ack-reaction-channel-broadcast/03-review.md` — 페르소나 리뷰 (Backend, QA 섹션 특히)

**참조 코드:**
- `companio/channels/slack.py` — 기존 `_progress_messages` 캐시, per-thread Lock 패턴 (이걸 모방)
- `companio/bus.py` — `OutboundMessage.reply_to_inbound`, `_FORWARDED_METADATA_KEYS` (절대 변경 금지)
- `companio/core/loop.py` — `_dispatch`, `_process_message` 흐름
- `tests/test_thread_metadata_propagation.py` — 단위 테스트 패턴 (`_build_slack_channel` helper, AsyncMock 패턴)

**브랜치:**
- 시작: `git checkout holmes && git pull && git checkout -b feature/slack-ack-reaction`
- 작업 종료 시 브랜치에 머물기, 커밋 X (control-agent가 검토 후 커밋)

## 3. 상세 요구사항

### 3-1. SlackConfig 스키마 확장 (`companio/config/schema.py`)

`SlackConfig`에 다음 필드 추가:
```python
ack_reactions_enabled: bool = False
```
default `False`. 인스턴스별 opt-in.

### 3-2. SlackChannel 상태 확장 (`companio/channels/slack.py`)

`__init__`에 추가:
```python
from collections import OrderedDict
import asyncio
from datetime import datetime, timedelta

# 상수
_ACK_REACTIONS_TTL = timedelta(hours=1)
_ACK_REACTIONS_MAX_SIZE = 500
_ACK_EYES = "eyes"

# instance state
self._active_ack_reactions: OrderedDict[tuple[str, str], dict] = OrderedDict()
# value: {"added_at": datetime, "task": asyncio.Task}
self._ack_reactions_lock = asyncio.Lock()
self._ack_reactions_runtime_disabled = False
```

### 3-3. 리액션 헬퍼 (`SlackChannel`)

```python
async def _add_reaction(self, chat_id: str, message_ts: str, name: str) -> bool:
    """Add a reaction. Returns True on success.
    
    Graceful error handling per 04-plan.md §1.11:
    - already_reacted, message_not_found, no_reaction: silent (DEBUG)
    - not_in_channel: WARN once
    - invalid_auth, missing_scope: ERROR + runtime disable
    - ratelimited: 1 retry with Retry-After
    - other: WARN
    """

async def _remove_reaction(self, chat_id: str, message_ts: str, name: str) -> bool:
    """Remove a reaction. Returns True on success. Same error handling."""
```

구현 가이드:
- `slack_sdk.errors.SlackApiError`로 에러 코드 분기
- `e.response.get("error")` 로 에러 코드 추출
- `e.response.headers.get("Retry-After")` 로 retry 대기
- `missing_scope` 또는 `invalid_auth` 발생 시 `self._ack_reactions_runtime_disabled = True` 세팅
- 모든 에러 케이스에서 raise 하지 않음 (graceful)

### 3-4. ACK 발송 (lock 밖, 즉시)

`_on_app_mention`, `_on_message`(DM 분기, channel thread 분기) **3곳 모두**에 다음 로직 추가 (`InboundMessage` publish 전 또는 publish와 병행):

```python
if (
    self.config.ack_reactions_enabled
    and not self._ack_reactions_runtime_disabled
    and metadata.get("message_ts")
):
    asyncio.create_task(
        self._send_ack_reaction(chat_id, metadata["message_ts"])
    )
```

`_send_ack_reaction` 헬퍼:
```python
async def _send_ack_reaction(self, chat_id: str, message_ts: str) -> None:
    key = (chat_id, message_ts)
    async with self._ack_reactions_lock:
        # Cleanup stale (TTL)
        now = datetime.now()
        expired = [
            k for k, v in self._active_ack_reactions.items()
            if now - v["added_at"] > _ACK_REACTIONS_TTL
        ]
        for k in expired:
            self._active_ack_reactions.pop(k, None)
        # Maxsize evict (oldest)
        while len(self._active_ack_reactions) >= _ACK_REACTIONS_MAX_SIZE:
            self._active_ack_reactions.popitem(last=False)
        # Dedup
        if key in self._active_ack_reactions:
            return
        # Create task
        task = asyncio.create_task(
            self._add_reaction(chat_id, message_ts, _ACK_EYES)
        )
        self._active_ack_reactions[key] = {"added_at": now, "task": task}
```

### 3-5. Done lifecycle (`SlackChannel.send` 분기)

`send()` 메서드 진입부에 추가:
```python
lifecycle = msg.metadata.get("_reaction_lifecycle")
if lifecycle in ("done_success", "done_error"):
    message_ts = msg.metadata.get("message_ts")
    if message_ts:
        await self._finalize_reaction(msg.chat_id, message_ts)
    if not msg.content:
        return  # reaction-only outbound, skip text dispatch
```

`_finalize_reaction` 헬퍼:
```python
async def _finalize_reaction(self, chat_id: str, message_ts: str) -> None:
    key = (chat_id, message_ts)
    async with self._ack_reactions_lock:
        entry = self._active_ack_reactions.pop(key, None)
    if entry:
        try:
            await entry["task"]  # ensure add ran first
        except Exception:
            pass
    await self._remove_reaction(chat_id, message_ts, _ACK_EYES)
```

**MVP 결정 (04-plan.md §1.4)**: ✅/❌ 추가 없이 eyes 제거만. 완료/에러 구분 표시는 Phase 2로.

### 3-6. `_dispatch` try/finally (`companio/core/loop.py`)

기존 `_dispatch`를 다음 구조로 변경:

```python
async def _dispatch(self, msg: InboundMessage) -> None:
    """Process under per-session lock."""
    success: bool | None = None
    async with self._session_locks[msg.session_key]:
        try:
            response = await self._process_message(msg)
            if response is not None:
                await self.bus.publish_outbound(response)
            success = True
        except asyncio.CancelledError:
            logger.info("Task cancelled for session {}", msg.session_key)
            success = False
            raise
        except Exception:
            logger.exception("Error processing message for session {}", msg.session_key)
            success = False
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

**주의**: `_process_message` 내부의 slash command early return(`/new`, `/help`, `/stop`)도 이 try/finally를 통과하므로 done lifecycle이 발행됨. 추가 변경 없음.

### 3-7. 신규 테스트 (`tests/test_slack_reactions.py`)

다음 테스트 케이스 작성. 기존 `tests/test_thread_metadata_propagation.py`의 `_build_slack_channel()` 패턴 재사용:

- `test_send_ack_reaction_calls_reactions_add_with_message_ts`
- `test_send_ack_reaction_idempotent_on_duplicate_key`
- `test_finalize_reaction_removes_eyes_after_task_await`
- `test_finalize_reaction_pops_dict_entry`
- `test_finalize_reaction_silent_when_no_prior_ack`
- `test_reactions_add_already_reacted_is_silent`
- `test_reactions_add_message_not_found_is_silent`
- `test_reactions_add_invalid_auth_disables_runtime`
- `test_reactions_add_missing_scope_disables_runtime`
- `test_reactions_add_rate_limited_retries_once_then_silent`
- `test_send_ack_reaction_disabled_flag_skips`
- `test_send_ack_reaction_disabled_runtime_skips`
- `test_ttl_evicts_stale_entries`
- `test_maxsize_evicts_oldest`
- `test_two_quick_mentions_have_independent_lifecycles`
- `test_send_with_done_success_metadata_finalizes_and_skips_text`
- `test_send_with_done_error_metadata_finalizes_and_skips_text`

### 3-8. 신규 회귀 가드 (`tests/test_dispatch_finally.py`)

- `test_dispatch_normal_path_publishes_done_success`
- `test_dispatch_exception_path_publishes_done_error`
- `test_dispatch_cancellation_path_publishes_done_error_before_reraise`
- `test_dispatch_skips_done_when_no_message_ts`

### 3-9. 회귀 가드 추가 (`tests/test_thread_metadata_propagation.py`)

기존 파일에 다음 추가 (또는 새 클래스로):
- `test_reaction_lifecycle_not_in_whitelist`: `assert "_reaction_lifecycle" not in _FORWARDED_METADATA_KEYS`
- `test_inbound_with_reaction_lifecycle_does_not_leak`: inbound에 `_reaction_lifecycle` 넣어도 `reply_to_inbound`이 떨어트리는지

### 3-10. Grep guard (`tests/test_slack_inbound_metadata.py` 신규)

```python
def test_slack_app_mention_populates_message_ts():
    slack_py = Path(__file__).parent.parent / "companio" / "channels" / "slack.py"
    source = slack_py.read_text(encoding="utf-8")
    assert '"message_ts"' in source
    assert re.search(r'"message_ts"\s*:\s*event(?:\.get)?\s*\(\s*["\']ts', source), \
        "_on_app_mention must populate metadata['message_ts'] from event['ts']"
```

## 4. 영향 파일 (예상)

| 파일 | 변경 유형 | 설명 |
|---|---|---|
| `companio/config/schema.py` | 수정 | `ack_reactions_enabled` 필드 |
| `companio/channels/slack.py` | 수정 | 헬퍼, lifecycle, 상태, lock |
| `companio/core/loop.py` | 수정 | `_dispatch` try/finally |
| `tests/test_slack_reactions.py` | 신규 | 17 개 단위 테스트 |
| `tests/test_dispatch_finally.py` | 신규 | 4 개 단위 테스트 |
| `tests/test_thread_metadata_propagation.py` | 수정 | 회귀 가드 2 개 추가 |
| `tests/test_slack_inbound_metadata.py` | 신규 | grep guard 1 개 |

## 5. 검증 기준

- [ ] `pytest tests/` 전체 통과 (기존 104 + 신규 24 이상)
- [ ] `ruff check companio/ tests/` 통과
- [ ] 04-plan.md §1.1~1.14, §3 PR 1 모든 항목 코드로 구현
- [ ] 00_qa-criteria.md Phase 1 (Q1.1~Q1.13) 모두 충족
- [ ] `_FORWARDED_METADATA_KEYS`가 `bus.py:12-19` 그대로 유지

## 6. 주의사항

### 절대 하지 말 것

- `_FORWARDED_METADATA_KEYS`에 새 키 추가 금지 (회귀 가드)
- ACK 리액션 발송 경로를 `loop.py`에 추가 금지 (lock 밖이 핵심, 04-plan §1.2)
- `BaseChannel`에 `send_ack`/`send_done` 메서드 추가 금지 (04-plan §1.3에서 metadata-driven으로 결정)
- 완료/에러 구분 리액션(`✅`/`❌`/`warning`) 추가 금지 (MVP 스코프 밖, 04-plan §1.4)
- 5초 텍스트 fallback 타이머 추가 금지 (PR 2의 progress pulse가 별도로 처리)
- 커밋 금지 — 코드 변경만, 결과 보고

### 반드시 따를 것

- 04-plan.md §1.1~1.14의 결정 사항을 임의로 변경하지 말 것. 의문이 들면 보고만 하고 그대로 구현
- `_progress_messages` 캐시와 `_active_ack_reactions`는 별개. 충돌 없음
- per-thread Lock 패턴은 PR#2의 `_progress_locks`를 모방
- 단위 테스트는 실제 Slack API 호출 없이 `AsyncMock` 사용
- DM과 channel 모두에서 ACK 리액션 발송 (04-plan은 DM 명시적 허용)

## 7. 결과 (작업 후 기록)

### 변경 파일

(에이전트가 채움)

### 검증 결과

(에이전트가 채움)

### 발견된 이슈

(에이전트가 채움)

### 후속 작업

(에이전트가 채움)
