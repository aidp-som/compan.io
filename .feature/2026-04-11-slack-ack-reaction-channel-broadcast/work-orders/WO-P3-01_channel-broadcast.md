# WO-P3-01: Channel Broadcast 구현

> 대상 에이전트: **compan-io-agent** (compan.io)
> 우선순위: P3
> 상태: ⬜ 대기
> 생성일: 2026-04-11

## 1. 목적

LLM이 사용자 의도를 해석해 `share_to_channel=True`로 `MessageSender.send()` 호출 시 응답을 채널 본문에도 broadcast(`reply_broadcast=True`)하는 기능. 04-plan.md PR 3 (Feature 2)의 결정 사항을 따른다.

## 2. 컨텍스트

**필독:**
- `04-plan.md` — §1.6, §1.8, §1.12, §1.13, §3 PR 3

**참조 코드:**
- `companio/tools/message.py` — `MessageSender.send()` 시그니처
- `companio/channels/slack.py` — `send()` 메서드, split_message chunk 루프
- `companio/core/loop.py` — `_process_message`, MessageSender 컨텍스트 세팅 부분

**브랜치:**
- 시작: `git checkout holmes && git pull && git checkout -b feature/slack-broadcast`
- 작업 종료 시 브랜치 머물기, 커밋 X

## 3. 상세 요구사항

### 3-1. SlackConfig 스키마 확장 (`companio/config/schema.py`)

```python
broadcast_enabled: bool = False
broadcast_blocked_channels: list[str] = []  # chat_id 목록
```

### 3-2. `MessageSender.send()` 시그니처 확장 (`companio/tools/message.py`)

`share_to_channel: bool = False` 파라미터 추가. docstring에 보수 가이드:

```python
async def send(
    self,
    content: str,
    share_to_channel: bool = False,
    media: list[str] | None = None,
    # ... 기존 파라미터
) -> str:
    """Send message to current chat context.
    
    Args:
        content: 메시지 내용
        share_to_channel: True면 채널 본문에도 함께 공지 (스레드 컨텍스트에서만 유효).
            반드시 사용자가 명시적으로 "채널에 공유/공지/알려"를 요청한 경우에만 사용.
            애매하면 False로 두세요. 비가역 액션이라 한 번 broadcast된 메시지는
            취소할 수 없습니다.
        media: 첨부 파일
    """
```

### 3-3. 3중 안전장치

`MessageSender.send()` 안에서:

```python
inbound = self._current_inbound  # set_context로 저장된 InboundMessage
broadcast_metadata = {}
broadcast_skip_reason = None

if share_to_channel:
    is_channel = bool(inbound.metadata.get("is_channel"))
    has_thread = bool(inbound.metadata.get("thread_ts"))
    flag_enabled = self._broadcast_enabled  # set from SlackConfig at init
    blocked = inbound.chat_id in self._broadcast_blocked_channels
    
    if not is_channel:
        broadcast_skip_reason = "not a channel"
    elif not has_thread:
        broadcast_skip_reason = "no thread context"
    elif not flag_enabled:
        broadcast_skip_reason = "broadcast disabled"
    elif blocked:
        broadcast_skip_reason = "channel blocked"
    else:
        broadcast_metadata["reply_broadcast"] = True
    
    if broadcast_skip_reason:
        logger.warning("share_to_channel ignored: {}", broadcast_skip_reason)

msg = OutboundMessage.reply_to_inbound(
    inbound, content, extra_metadata=broadcast_metadata, media=media
)
```

`MessageSender.send()` 반환값에 skip 힌트 포함 (LLM이 다음 턴에 인지):
```python
if broadcast_skip_reason:
    return f"Sent. (Note: channel broadcast skipped — {broadcast_skip_reason})"
return "Sent."
```

### 3-4. SlackChannel.send() — `reply_broadcast` 처리

기존 `chat_postMessage` 호출 부분(slack.py 약 244 line) 변경:

```python
reply_broadcast_flag = (
    msg.metadata.get("reply_broadcast", False)
    and bool(thread_ts)
    and bool(msg.metadata.get("is_channel", False))
    and msg.chat_id not in self.config.broadcast_blocked_channels
)

# split_message로 여러 chunk가 되는 경우 마지막 chunk에만 broadcast
chunks = list(split_message(mrkdwn_text, SLACK_MAX_MESSAGE_LEN))
for i, chunk in enumerate(chunks):
    is_last = (i == len(chunks) - 1)
    result = await self._app.client.chat_postMessage(
        channel=msg.chat_id,
        text=chunk,
        thread_ts=thread_ts,
        reply_broadcast=reply_broadcast_flag if is_last else False,
    )
    
    # broadcast 발송 시 INFO 감사 로그
    if reply_broadcast_flag and is_last:
        logger.info(
            "slack.broadcast.sent chat_id={} thread_ts={} content_len={}",
            msg.chat_id, thread_ts, len(chunk)
        )
```

### 3-5. Shadow detector

`companio/core/loop.py:_process_message` 진입 시점 또는 종료 직후에 패턴 매칭:

```python
import re

_BROADCAST_TRIGGER_PATTERN = re.compile(
    r"(채널에도|채널에 공유|채널에 공지|채널에 알|다른 사람도|broadcast|공지해|"
    r"방송해|모두에게|팀에 알|everyone)",
    re.IGNORECASE
)

def _detect_broadcast_intent(content: str) -> bool:
    return bool(_BROADCAST_TRIGGER_PATTERN.search(content))
```

처리 흐름:
1. `_process_message` 진입 시 inbound에 트리거 패턴 있으면 `intent_detected = True` 플래그
2. `MessageSender`가 처리 중 `share_to_channel=True`로 호출되었는지 추적 (예: `MessageSender.set_context(inbound)` 후 `self._broadcast_called: bool = False`, `send(share_to_channel=True)` 시 True)
3. `_process_message` 종료 직전 (`finally` 또는 정상 return 직전):
   - `intent_detected and not broadcast_called` → `slack.broadcast.shadow_miss` WARN 로그
   - 로그에 `chat_id`, `content[:200]` 포함

### 3-6. SlackConfig 통합

`MessageSender`가 broadcast 설정에 접근해야 함. 두 옵션:
- (a) `MessageSender.__init__`에 `broadcast_enabled`, `broadcast_blocked_channels` 인자 추가
- (b) `set_context(inbound)` 시 SlackConfig를 함께 전달

`AgentLoop`가 MessageSender를 어떻게 생성하는지 확인 후 자연스러운 방법 선택. **권장: (a)**.

### 3-7. 신규 테스트

#### `tests/test_message_sender_broadcast.py`

- `test_share_to_channel_sets_reply_broadcast_metadata`
- `test_share_to_channel_ignored_in_dm` (`is_channel=False`)
- `test_share_to_channel_ignored_without_thread_ts`
- `test_share_to_channel_ignored_when_flag_disabled`
- `test_share_to_channel_blocked_channel_strict_skip`
- `test_skip_hint_in_return_value_when_ignored`
- `test_share_to_channel_false_default_no_broadcast`

#### `tests/test_slack_broadcast.py`

`_build_slack_channel()` 패턴 사용:

- `test_slack_send_passes_reply_broadcast_to_api`
- `test_slack_send_drops_broadcast_when_metadata_missing`
- `test_slack_send_drops_broadcast_when_not_channel` (defense in depth)
- `test_slack_send_broadcasts_only_last_chunk` (split된 경우)
- `test_slack_send_logs_broadcast_at_info` (caplog)
- `test_slack_send_blocked_channel_strict_drop`

#### `tests/test_broadcast_shadow_detection.py`

- `test_shadow_logger_warns_on_unmatched_trigger`
- `test_shadow_logger_silent_when_tool_used`
- `test_shadow_pattern_matches_korean_phrases` (parameterized)
- `test_shadow_pattern_matches_english_phrases`

#### `tests/test_thread_metadata_propagation.py`에 회귀 가드 추가

- `test_reply_broadcast_not_in_whitelist`
- `test_inbound_with_reply_broadcast_does_not_leak`

## 4. 영향 파일 (예상)

| 파일 | 변경 유형 | 설명 |
|---|---|---|
| `companio/config/schema.py` | 수정 | `broadcast_enabled`, `broadcast_blocked_channels` |
| `companio/tools/message.py` | 수정 | `share_to_channel` 파라미터, 3중 안전장치, 반환값 힌트 |
| `companio/channels/slack.py` | 수정 | `chat_postMessage`에 `reply_broadcast` + 마지막 chunk 분기 + INFO 로그 |
| `companio/core/loop.py` | 수정 | Shadow detector 패턴 + 처리 종료 시 검사 |
| `tests/test_message_sender_broadcast.py` | 신규 | 7 테스트 |
| `tests/test_slack_broadcast.py` | 신규 | 6 테스트 |
| `tests/test_broadcast_shadow_detection.py` | 신규 | 4 테스트 |
| `tests/test_thread_metadata_propagation.py` | 수정 | 회귀 가드 2 개 추가 |

## 5. 검증 기준

- [ ] `pytest tests/` 전체 통과
- [ ] `ruff check companio/ tests/` 통과
- [ ] 04-plan.md §1.6, §1.8, §1.12, §1.13 모든 항목 코드로 구현
- [ ] 00_qa-criteria.md Phase 3 (Q3.1~Q3.11) 모두 충족
- [ ] `_FORWARDED_METADATA_KEYS`에 `reply_broadcast` 추가하지 않음 (회귀 가드)

## 6. 주의사항

- `reply_broadcast`는 outbound 전용 metadata. **`_FORWARDED_METADATA_KEYS`에 추가 금지**
- 마지막 chunk에만 `reply_broadcast=True` (중복 broadcast 방지)
- DM (`is_channel=False`)에서는 silent skip + 반환값 힌트
- Shadow detector는 **WARN 레벨만**, 처리 흐름 차단 금지
- `MessageSender.send()`의 기존 시그니처 호환성 유지 (kwargs 추가만)
- 커밋 금지

## 7. 결과 (작업 후 기록)

### 변경 파일
### 검증 결과
### 발견된 이슈
### 후속 작업
