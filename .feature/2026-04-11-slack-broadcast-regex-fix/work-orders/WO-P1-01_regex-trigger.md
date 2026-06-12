# WO-P1-01: Broadcast 트리거 재설계 (LLM tool → regex)

> 대상 에이전트: **compan-io-agent** (compan.io)
> 우선순위: P1
> 상태: ⬜ 대기
> 생성일: 2026-04-11

## 1. 목적

직전 머지된 `1e10b8e` (channel broadcast feature)이 운영 검증에서 동작 불가능 판명. `MessageSender.send(share_to_channel=...)`는 dead code (Claude는 `claude -p` subprocess라 Python 클래스 호출 불가). regex 기반 결정적 트리거로 전환.

## 2. 컨텍스트

**필독:**
- `/home/holmes/compan.io/.feature/2026-04-11-slack-broadcast-regex-fix/04-plan.md` — 결정 사항 §1.1~1.10, 구현 계획 Phase A~I
- `/home/holmes/compan.io/.feature/2026-04-11-slack-broadcast-regex-fix/03-review.md` — Backend/UX/QA 리뷰 결과 (특히 critical 항목)
- `/home/holmes/compan.io/.feature/2026-04-11-slack-broadcast-regex-fix/02-design.md` — 컴포넌트 설계
- `/home/holmes/compan.io/.feature/2026-04-11-slack-broadcast-regex-fix/01-analysis.md` — 현황 분석

**참조 코드 (반드시 읽기):**
- `companio/core/loop.py` — 특히 `_BROADCAST_TRIGGER_PATTERN`, `_detect_broadcast_intent`, `_process_message`, `_invoke_claude_turn`, `_process_message_inner`, `__init__` (slack_cfg 처리 부분)
- `companio/tools/message.py` — `MessageSender.send()` dead code 영역
- `companio/cli.py:418` — "MessageSender cannot be injected into Claude CLI subprocess" 주석
- `companio/channels/slack.py` — `chat_postMessage` + `reply_broadcast` 처리 (chunk loop 부분)
- `companio/bus.py` — `_FORWARDED_METADATA_KEYS`, `OutboundMessage.reply_to_inbound`
- `tests/test_broadcast_shadow_detection.py` — 폐기/마이그레이션 대상
- `tests/test_message_sender_broadcast.py` — dead code contract로 재명명
- `tests/test_slack_broadcast.py` — 단일 chunk 가드 추가
- `tests/test_thread_metadata_propagation.py` — 회귀 가드 (변경 금지)

**브랜치:**
- 시작: `git checkout holmes && git pull && git checkout -b feature/slack-broadcast-regex-fix`
- 작업 종료 시 브랜치에 머물기, 커밋 X

## 3. 상세 요구사항

### 3-1. `_detect_broadcast_intent` 재작성 (`companio/core/loop.py`)

기존 `_BROADCAST_TRIGGER_PATTERN` 제거, 토큰 기반 + 명령형 어미 gate로 교체. **04-plan.md §1.2의 코드를 그대로 구현**:

```python
import re

_BROADCAST_CHANNEL_TOKENS = ("채널", "channel")
_BROADCAST_VERB_TOKENS_KO = (
    "공지", "공유", "알려", "알림", "올려", "올리", "브리핑",
    "방송", "보내", "전달", "안내",
)
_BROADCAST_STANDALONE_TOKENS = (
    "다른 사람도", "팀에 알", "팀에 공유", "팀에 공지",
    "모두에게", "모두한테", "@channel", "@here", "방송해주",
)

_IMPERATIVE_ENDING = re.compile(
    r"(해|해줘|해주세요|드려|드릴게|알려|알려줘|올려|올려줘|"
    r"공유해|공지해|브리핑해|전달해|보내|보내줘|보내주세요)"
    r"\s*[?!.~ㅋㅎ]*\s*$"
)

_ENGLISH_CHANNEL_VERB = re.compile(
    r"(\b(post|share|announce|broadcast)\b.*\bchannel\b|"
    r"\bto\s+(the\s+)?channel\b|"
    r"\bbroadcast\s+to\b)",
    re.IGNORECASE,
)

_REFERENTIAL_HINT = re.compile(
    r"(떴|떴는데|떴어|됐|됐어|있어|있었|봤|봤어|받았|"
    r"올라왔|있던|있는|었|왔|뜬\s*글|뜬\s*공지)"
)


def _detect_broadcast_intent(content: str) -> bool:
    if not content:
        return False
    text = content.strip().lower()
    if any(tok in text for tok in (t.lower() for t in _BROADCAST_STANDALONE_TOKENS)):
        return True
    has_channel = any(tok in text for tok in (t.lower() for t in _BROADCAST_CHANNEL_TOKENS))
    has_verb = any(tok in text for tok in _BROADCAST_VERB_TOKENS_KO)
    is_imperative = bool(_IMPERATIVE_ENDING.search(text))
    is_referential = bool(_REFERENTIAL_HINT.search(text))
    if has_channel and has_verb and is_imperative and not is_referential:
        return True
    if _ENGLISH_CHANNEL_VERB.search(text):
        return True
    return False
```

### 3-2. `_apply_broadcast_intent` 헬퍼 (`AgentLoop` 메서드)

**04-plan.md §1.3의 코드를 그대로 구현**. 핵심 제약:

- **`OutboundMessage()` 생성자 직접 호출 금지** — `test_loop_py_uses_only_reply_to_inbound` 회귀 가드 위반
- **반드시 `OutboundMessage.reply_to_inbound(msg, content, extra_metadata={"reply_broadcast": True}, media=...)` 사용**
- 4 guards (priority 순서 고정): not_channel → no_thread → disabled → blocked
- skip 시 INFO 로그 (`slack.broadcast.intent_skipped reason=...`)
- 성공 시 INFO 로그 (`slack.broadcast.auto_triggered ...`)
- 마커 중복 방지 (`if _BROADCAST_MARKER in response.content: return`)

```python
_BROADCAST_MARKER = "\n\n_📢 채널에도 공유되었습니다_"

def _apply_broadcast_intent(
    self, msg: InboundMessage, response: OutboundMessage
) -> OutboundMessage:
    inbound_meta = msg.metadata or {}
    skip_reason: str | None = None
    if not inbound_meta.get("is_channel"):
        skip_reason = "not_channel"
    elif not inbound_meta.get("thread_ts"):
        skip_reason = "no_thread"
    elif not self._slack_broadcast_enabled:
        skip_reason = "disabled"
    elif msg.chat_id in self._slack_broadcast_blocked_channels:
        skip_reason = "blocked"
    
    if skip_reason:
        logger.info(
            "slack.broadcast.intent_skipped chat_id={} reason={} content={!r}",
            msg.chat_id, skip_reason, msg.content[:200]
        )
        return response
    
    if _BROADCAST_MARKER in response.content:
        logger.warning("slack.broadcast.marker_already_present chat_id={}", msg.chat_id)
        return response
    
    new_content = (response.content or "") + _BROADCAST_MARKER
    new_outbound = OutboundMessage.reply_to_inbound(
        msg,
        new_content,
        extra_metadata={"reply_broadcast": True},
        media=response.media,
    )
    
    logger.info(
        "slack.broadcast.auto_triggered chat_id={} thread_ts={} content={!r}",
        msg.chat_id, inbound_meta.get("thread_ts"), msg.content[:200]
    )
    return new_outbound
```

### 3-3. `AgentLoop.__init__`에 broadcast 설정 인스턴스 변수

기존에 `MessageSender(broadcast_enabled=..., broadcast_blocked_channels=...)`로 전달하던 값을 `AgentLoop` 자체에도 보유:

```python
self._slack_broadcast_enabled: bool = slack_cfg.broadcast_enabled if slack_cfg else False
self._slack_broadcast_blocked_channels: list[str] = (
    list(slack_cfg.broadcast_blocked_channels) if slack_cfg else []
)
```

### 3-4. `_process_message` 흐름 변경

기존 shadow detector try/finally 블록 **완전 제거**. 새 흐름 (04-plan §1.6):

```python
async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
    preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
    logger.info("Processing from {}:{}: {}", msg.channel, msg.sender_id, preview)
    
    broadcast_intent = _detect_broadcast_intent(msg.content)
    
    key = msg.session_key
    session = await self._session_manager.get_or_create(key)
    
    # Slash commands — early return without intent application (의도적)
    cmd = msg.content.strip().lower()
    if cmd in ("/new", "!new"):
        return await self._handle_new(msg, session)
    if cmd in ("/help", "!help"):
        return OutboundMessage.reply_to_inbound(msg, "...help text...")
    
    self._maybe_consolidate(session)
    self.message_sender.set_context(msg)
    self.message_sender.start_turn()
    
    response = await self._invoke_claude_turn(msg, session, key)
    
    # Apply broadcast intent only if response is a final outbound
    if response is not None and broadcast_intent:
        response = self._apply_broadcast_intent(msg, response)
    
    return response
```

`_invoke_claude_turn` 시그니처에서 `broadcast_intent_detected` 인자 제거 (이미 `del`로 unused). 시그니처: `async def _invoke_claude_turn(self, msg, session, key) -> OutboundMessage | None`. 본문은 그대로 (pulse task lifecycle 유지).

### 3-5. `SlackChannel.send` 다중 chunk 가드 (`companio/channels/slack.py`)

기존 chunk loop를 **04-plan §1.4 코드로 변경**:

```python
chunks = list(split_message(mrkdwn_text, SLACK_MAX_MESSAGE_LEN))
# Broadcast only on single-chunk responses
can_broadcast = reply_broadcast_flag and len(chunks) == 1

if reply_broadcast_flag and len(chunks) > 1:
    logger.info(
        "slack.broadcast.skipped_multi_chunk chat_id={} chunks={}",
        msg.chat_id, len(chunks)
    )

for i, chunk in enumerate(chunks):
    is_last = (i == len(chunks) - 1)
    result = await self._app.client.chat_postMessage(
        channel=msg.chat_id,
        text=chunk,
        thread_ts=thread_ts,
        reply_broadcast=can_broadcast if is_last else False,
    )
    # ... 기존 progress cache 처리 등 ...
```

### 3-6. `MessageSender.send` runtime warning + docstring (`companio/tools/message.py`)

`send()` 메서드:
- Docstring 상단에 `NOTE (2026-04-11): The share_to_channel parameter is currently DEAD CODE...` 블록 추가 (04-plan §1.8)
- 메서드 본문 진입부에 다음 추가:
```python
if share_to_channel:
    logger.warning(
        "MessageSender.send(share_to_channel=True) called — this path is "
        "dead code as of 2026-04-11. Broadcast is now handled by regex "
        "trigger in AgentLoop. If you see this log, investigate the caller."
    )
```
- **나머지 본문은 변경 X** — 3중 가드, `_broadcast_called` 추적, `extra["reply_broadcast"]` 세팅 모두 dead code contract로 유지

### 3-7. 신규 단위 테스트

#### `tests/test_broadcast_intent.py` (신규)

`_detect_broadcast_intent` 함수 단위 테스트. parametrized:

**Positive (≥15):**
- `"채널에도 공유해"`, `"채널 본문에도 브리핑해"` (원 버그 케이스), `"이 결과 채널에 공지해줘"`, `"요약해서 채널에 올려줘"`, `"채널에 방송해 주세요"`, `"채널에 브리핑 부탁드려"`
- standalone: `"팀에 알려줘"`, `"모두에게 알려주세요"`, `"다른 사람도 볼 수 있게 해줘"`, `"@channel 공지"`, `"@here 긴급"`, `"방송해주세요"`
- English: `"please broadcast this to the channel"`, `"share with the channel"`, `"announce to everyone"`, `"post this to #general"`

**Negative (≥15) — 오탐 방어 필수:**
- 무관: `""`, `None`, `"hello world"`, `"버그 수정해줘"`
- 참조 발화 (referential): `"어제 #general 채널에 공지 올라왔어?"`, `"이 채널에 PR 공지가 떴는데 뭔지 알아?"`, `"announce 채널에서 broadcast 알림 못 받았어"`, `"'채널 공지' 기능 어떻게 써?"`, `"broadcast API 문서 어디 있어?"`, `"채널 주제가 뭐였지?"`
- 명령형 아님: `"이 코드 공유 폴더에 넣어줘"` (공유 있지만 채널 아님), `"파일 공유 링크 뽑아줘"`, `"채널 본 적 있어?"`

`test_broadcast_marker_exact_string_lock`:
```python
def test_broadcast_marker_exact_string_lock():
    from companio.core.loop import _BROADCAST_MARKER
    assert _BROADCAST_MARKER == "\n\n_📢 채널에도 공유되었습니다_"
```

#### `tests/test_apply_broadcast_intent.py` (신규)

`_apply_broadcast_intent` 8 핵심 케이스:

1. `test_all_guards_pass_injects_broadcast` — happy path, marker append + reply_broadcast=True
2. `test_skip_when_not_channel` — DM, skip_reason="not_channel"
3. `test_skip_when_no_thread` — channel root, skip_reason="no_thread"
4. `test_skip_when_flag_disabled` — `_slack_broadcast_enabled=False`
5. `test_skip_when_channel_blocked` — chat_id in blocked list
6. `test_guard_priority_earliest_reason_wins` — `not_channel + disabled` → `not_channel`
7. `test_skip_returns_response_untouched` — skip 시 동일 객체 반환 (mutation 없음)
8. `test_marker_already_present_returns_unchanged` — 마커 중복 방지

추가:
- `test_apply_uses_reply_to_inbound_not_constructor` — `OutboundMessage(...)` 호출 안 됨 검증 (mock counter 또는 spy)
- `test_auto_triggered_logs_with_chat_id_thread_ts_content` — caplog 검증
- `test_intent_skipped_logs_with_reason` — caplog

#### `tests/test_loop_broadcast_integration.py` (신규)

`_process_message` 통합 흐름:
- `test_intent_matched_happy_path_includes_marker_and_metadata`
- `test_intent_matched_dm_skips_silently`
- `test_slash_help_with_intent_does_not_apply_broadcast`
- `test_slash_new_with_intent_does_not_apply_broadcast`
- `test_response_none_skips_intent_application`
- `test_progress_pulse_messages_not_broadcast` — pulse messages가 broadcast 적용 받지 않음 (회귀 가드)

#### `tests/test_slack_broadcast.py` 확장 (기존 파일)

추가:
- `test_broadcast_skipped_when_split_into_multiple_chunks` — len(chunks) > 1 → reply_broadcast 모두 False
- `test_broadcast_applied_when_single_chunk` — len(chunks) == 1 → 마지막(=유일) chunk만 reply_broadcast=True
- `test_skipped_multi_chunk_logged_at_info` — caplog

기존 `test_slack_send_broadcasts_only_last_chunk`는 **단일 chunk 케이스로 의미 유지**되도록 수정 (또는 위 새 테스트가 cover하면 폐기).

### 3-8. 기존 테스트 마이그레이션

#### `tests/test_broadcast_shadow_detection.py` 폐기

전체 파일 삭제 또는 다음으로 처리:
- `test_shadow_logger_warns_on_unmatched_trigger` → 삭제
- `test_shadow_logger_silent_when_tool_used` → 삭제  
- `test_shadow_pattern_matches_korean_phrases` (parametrized) → `tests/test_broadcast_intent.py`로 이동, 명령형 어미 추가
- `test_shadow_pattern_matches_english_phrases` (parametrized) → 동일

`"방송해주세요"`는 standalone 토큰 `"방송해주"`로 매칭됨을 확인.

#### `tests/test_message_sender_broadcast.py` 재명명

- 클래스명 `Test...` → `TestShareToChannelDeadCodeContract`
- 파일 상단에 docstring 추가:
```python
"""DEAD CODE CONTRACT (2026-04-11)

The MessageSender.send(share_to_channel=...) parameter is currently dead
code — Claude runs as a `claude -p` subprocess and has no MCP bridge to
this Python class, so the LLM cannot invoke this path. Broadcast is now
triggered by regex matching on user input in AgentLoop._process_message.

These tests are kept as a contract to lock the behavior in case a future
MCP bridge resurrects this code path. They should still all pass — they
verify the metadata-injection logic works correctly when the path is
called directly (e.g., from tests or future bridges).
"""
```
- 테스트 자체는 유지 (모두 GREEN)

### 3-9. 회귀 가드 (`tests/test_thread_metadata_propagation.py`)

**변경 금지**. 다음 7개 모두 GREEN 유지:
1. `test_whitelist_is_the_source_of_truth`
2. `test_reply_broadcast_not_in_whitelist`
3. `test_inbound_with_reply_broadcast_does_not_leak`
4. `test_loop_py_uses_only_reply_to_inbound` ← **본 fix가 깨지면 안 되는 가장 중요한 가드**
5. `test_slack_send_broadcasts_only_last_chunk` (또는 `test_broadcast_applied_when_single_chunk`로 의미 유지)
6. `test_slack_send_drops_broadcast_when_not_channel`
7. `test_slack_send_blocked_channel_strict_drop`

특히 #4는 `_apply_broadcast_intent`가 `OutboundMessage()` 직접 호출하면 즉시 RED. **반드시 `reply_to_inbound()` 사용**.

## 4. 영향 파일 (예상)

| 파일 | 변경 유형 | 설명 |
|---|---|---|
| `companio/core/loop.py` | 수정 | 패턴 함수 재작성, `_apply_broadcast_intent` 추가, `_process_message` 흐름 변경, `_invoke_claude_turn` 시그니처 정리, `__init__` broadcast 설정 보유 |
| `companio/channels/slack.py` | 수정 | `chat_postMessage` chunk loop에 단일 chunk 가드 + multi-chunk INFO 로그 |
| `companio/tools/message.py` | 수정 | `send()`에 dead code warning + docstring NOTE |
| `tests/test_broadcast_intent.py` | 신규 | parametrized positive/negative + marker exact lock |
| `tests/test_apply_broadcast_intent.py` | 신규 | 8 핵심 guard 케이스 + reply_to_inbound 검증 |
| `tests/test_loop_broadcast_integration.py` | 신규 | `_process_message` 흐름 통합 |
| `tests/test_slack_broadcast.py` | 수정 | 단일 chunk 가드 테스트 추가 |
| `tests/test_broadcast_shadow_detection.py` | 삭제 | 패턴 테스트는 `test_broadcast_intent.py`로 이동 |
| `tests/test_message_sender_broadcast.py` | 수정 | DEAD CODE CONTRACT docstring + 클래스 재명명 |

## 5. 검증 기준

- [ ] `pytest tests/` 전체 통과 (사전 실패 `test_claude_cli.py` 2건은 무시 가능)
- [ ] `ruff check companio/ tests/` 통과 (사전 위반 `context.py:121` 1건은 무시 가능)
- [ ] 7개 회귀 가드 모두 GREEN (특히 `test_loop_py_uses_only_reply_to_inbound`)
- [ ] `_FORWARDED_METADATA_KEYS` 정의 변경 없음
- [ ] 04-plan.md §1.1~1.10 모두 코드로 구현
- [ ] negative test (오탐 방어) ≥15 케이스 모두 통과
- [ ] `tests/test_broadcast_intent.py::test_broadcast_marker_exact_string_lock` 통과
- [ ] `tests/test_apply_broadcast_intent.py::test_apply_uses_reply_to_inbound_not_constructor` 통과

## 6. 주의사항

### 절대 하지 말 것

- **`OutboundMessage(...)` 직접 생성자 호출 금지** (회귀 가드 위반). `reply_to_inbound()` 사용
- `_FORWARDED_METADATA_KEYS`에 키 추가 금지
- `MessageSender.send()` 본문의 기존 로직(3중 가드, `_broadcast_called`, `extra["reply_broadcast"]`) 제거 금지 (dead code contract)
- shadow detector 자리에 새 로그 메시지 변형으로 남기지 말 것 — 통째로 제거
- chunk loop에서 `reply_broadcast`를 마지막 chunk가 아닌 곳에 적용 금지
- 마커 텍스트(`\n\n_📢 채널에도 공유되었습니다_`)를 임의로 변경 금지 (exact lock 테스트가 잡음)

### 반드시 따를 것

- 04-plan.md §1.1~1.10 결정 사항 임의 변경 금지
- 기존 PR#2 + P1 + P2 + P3의 모든 기능(reactions, pulse, broadcast 인프라)이 회귀하지 않음을 단위 테스트로 검증
- 명령형 어미 gate 패턴은 04-plan §1.2 그대로 사용
- 영어 bigram 패턴(`to (the )?channel`)은 04-plan §1.2 그대로 사용
- 과거형/참조형 (`_REFERENTIAL_HINT`)은 04-plan §1.2 그대로 사용
- 커밋 금지

## 7. 결과 (작업 후 기록)

### 변경 파일
### 검증 결과
### 발견된 이슈
### 후속 작업
