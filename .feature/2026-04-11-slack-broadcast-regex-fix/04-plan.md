# 피처 플랜: Broadcast 트리거 재설계 (LLM tool → regex)

> 작성일: 2026-04-11
> 기반: 01-analysis.md, 02-design.md, 03-review.md
> 컨텍스트: 직전 머지된 `1e10b8e`의 broadcast 기능이 아키텍처상 동작 불가능 → regex 기반 결정적 트리거로 전환

## 1. 최종 결정 사항

### 1.1 트리거 메커니즘: regex (LLM 도구 호출 → dead code)

`MessageSender.send(share_to_channel=...)`는 dead code로 유지(향후 MCP 브리지 대비). 실제 broadcast 트리거는 `loop.py:_process_message`에서 `_detect_broadcast_intent(msg.content)` 매칭 시 자동 적용. **사유**: Backend/QA 컨센서스 — Claude `claude -p` subprocess는 Python 클래스 호출 불가, cli.py:418의 명시적 주석을 04-plan에서 누락했음.

### 1.2 토큰 매칭 + 명령형 어미 gate (오탐 방어)

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

# 명령형 어미: 사용자가 "지금 ~해줘"라고 요청하는 패턴
_IMPERATIVE_ENDING = re.compile(
    r"(해|해줘|해주세요|드려|드릴게|알려|알려줘|올려|올려줘|"
    r"공유해|공지해|브리핑해|전달해|보내|보내줘|보내주세요)"
    r"\s*[?!.~ㅋㅎ]*\s*$"
)

# 영어: 채널 + verb를 bigram으로 강제
_ENGLISH_CHANNEL_VERB = re.compile(
    r"(\b(post|share|announce|broadcast)\b.*\bchannel\b|"
    r"\bto\s+(the\s+)?channel\b|"
    r"\bbroadcast\s+to\b)",
    re.IGNORECASE,
)

# 과거형/참조형 (오탐 제외)
_REFERENTIAL_HINT = re.compile(
    r"(떴|떴는데|떴어|됐|됐어|있어|있었|봤|봤어|받았|"
    r"올라왔|있던|있는|었|왔|뜬\s*글|뜬\s*공지)"
)


def _detect_broadcast_intent(content: str) -> bool:
    """Heuristic: detect imperative request to broadcast bot reply.
    
    Strict gates to minimize false positives:
    - Standalone phrases trigger directly
    - Korean: needs (channel token) AND (verb token) AND (imperative ending)
              AND NOT (referential hint)
    - English: needs strong bigram pattern
    """
    if not content:
        return False
    text = content.strip().lower()
    
    # Standalone phrases (high confidence)
    if any(tok in text for tok in (t.lower() for t in _BROADCAST_STANDALONE_TOKENS)):
        return True
    
    # Korean: 4 conditions must all be met
    has_channel = any(tok in text for tok in (t.lower() for t in _BROADCAST_CHANNEL_TOKENS))
    has_verb = any(tok in text for tok in _BROADCAST_VERB_TOKENS_KO)
    is_imperative = bool(_IMPERATIVE_ENDING.search(text))
    is_referential = bool(_REFERENTIAL_HINT.search(text))
    
    if has_channel and has_verb and is_imperative and not is_referential:
        return True
    
    # English: stricter bigram
    if _ENGLISH_CHANNEL_VERB.search(text):
        return True
    
    return False
```

**사유**: Backend/UX/QA 모두 오탐 우려. Backend §5 + QA §2 + UX §3 컨센서스. 명령형 어미 gate가 가장 효과적인 단일 차단점.

### 1.3 `_apply_broadcast_intent`: `reply_to_inbound` 기반 (회귀 가드 통과)

```python
_BROADCAST_MARKER = "\n\n_📢 채널에도 공유되었습니다_"


def _apply_broadcast_intent(
    self, msg: InboundMessage, response: OutboundMessage
) -> OutboundMessage:
    """Inject reply_broadcast=True via reply_to_inbound (regression-safe).
    
    4 guards (priority order):
      1. is_channel
      2. thread_ts present
      3. broadcast_enabled config flag
      4. chat_id NOT in broadcast_blocked_channels
    
    On guard fail: log INFO with reason, return response unchanged.
    On success: log INFO 'auto_triggered', return new response with marker
                + reply_broadcast=True via reply_to_inbound.
    """
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
    
    # Guard against duplicate marker (defensive)
    if _BROADCAST_MARKER in response.content:
        logger.warning(
            "slack.broadcast.marker_already_present chat_id={}", msg.chat_id
        )
        return response
    
    # Build new outbound via reply_to_inbound to satisfy regression guard
    # (test_loop_py_uses_only_reply_to_inbound)
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

**사유**: QA §1 — `OutboundMessage(...)` 직접 생성자는 `test_loop_py_uses_only_reply_to_inbound`를 즉시 RED로 만듦. **반드시 `reply_to_inbound()` 사용**. 이 패턴은 PR#2에서 확립된 metadata 흐름과 일치.

### 1.4 chunk-split 다중 chunk 응답: broadcast 적용 안 함

`SlackChannel.send()`에 다중 chunk 가드 추가:

```python
chunks = list(split_message(mrkdwn_text, SLACK_MAX_MESSAGE_LEN))
# Broadcast only on single-chunk responses to keep marker + reply_broadcast aligned
can_broadcast = reply_broadcast_flag and len(chunks) == 1

for i, chunk in enumerate(chunks):
    is_last = (i == len(chunks) - 1)
    result = await self._app.client.chat_postMessage(
        channel=msg.chat_id,
        text=chunk,
        thread_ts=thread_ts,
        reply_broadcast=can_broadcast if is_last else False,
    )
```

다중 chunk 시 INFO 로그 추가:
```python
if reply_broadcast_flag and len(chunks) > 1:
    logger.info(
        "slack.broadcast.skipped_multi_chunk chat_id={} chunks={}",
        msg.chat_id, len(chunks)
    )
```

**사유**: Backend §1 + UX §7 + QA §4 컨센서스 — 다중 chunk + 마지막 chunk 한정 broadcast = observer 맥락 손실. MVP는 단일 chunk만 안전. 첫 chunk prepend는 후속.

### 1.5 marker 위치: 응답 끝 (MVP)

`response.content + _BROADCAST_MARKER`. 단일 chunk이므로 마커가 응답 끝에 자연스럽게 위치.

**사유**: §1.4의 단일 chunk 가드 채택으로 마커가 broadcast되는 청크에 자동 정합. 첫 chunk prepend + multi-chunk 지원은 후속 PR.

### 1.6 Shadow detector 제거 + intent → primary trigger

기존 `_process_message`의 try/finally 안 shadow_miss 로그 블록은 통째로 제거. `_invoke_claude_turn`의 `broadcast_intent_detected` 인자도 제거 (이미 unused). 새 흐름:

```python
async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
    preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
    logger.info("Processing from {}:{}: {}", msg.channel, msg.sender_id, preview)
    
    broadcast_intent = _detect_broadcast_intent(msg.content)
    
    key = msg.session_key
    session = await self._session_manager.get_or_create(key)
    
    # Slash commands — early return without intent application
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

`_invoke_claude_turn`은 기존 P2의 pulse task lifecycle만 담당 (`broadcast_intent_detected` 인자 제거).

### 1.7 Slash command + intent: 의도적으로 intent 미적용

`/new`, `/help`, `/stop`은 early return이므로 `_apply_broadcast_intent` 도달 안 함. 의도적 결정 — slash command 응답을 채널 본문에 broadcast할 이유 없음. 회귀 가드 테스트로 잠금.

### 1.8 `MessageSender.send(share_to_channel=True)` runtime warning

`tools/message.py`의 `MessageSender.send()`:

```python
async def send(
    self,
    content: str,
    share_to_channel: bool = False,
    ...
) -> str:
    """...
    
    NOTE (2026-04-11): The share_to_channel parameter is currently DEAD CODE.
    Claude runs as a subprocess (`claude -p`) without an MCP bridge to this
    Python class, so this method is never invoked from the LLM. Broadcast is
    triggered via regex on user input in AgentLoop._process_message instead.
    This signature is kept for a potential future MCP bridge.
    """
    if share_to_channel:
        logger.warning(
            "MessageSender.send(share_to_channel=True) called — this path is "
            "dead code as of 2026-04-11. Broadcast is now handled by regex "
            "trigger in AgentLoop. If you see this log, investigate the caller."
        )
    # ... rest unchanged
```

기존 3중 안전장치 / `_broadcast_called` 추적 / `extra["reply_broadcast"]` 세팅은 그대로 유지 (dead code contract). 미래 호출자가 생기면 동작.

### 1.9 `chat_id` 정규화

`broadcast_blocked_channels` config는 **Slack channel ID 형식만** 허용 (`C0123ABC`). `#channel-name` 형식 금지. SlackConfig docstring에 명시. 비교는 strict equality (현재 그대로, 정규화 불필요).

### 1.10 운영 가시성 (구조화 로그)

| 이벤트 | 레벨 | 필드 |
|---|---|---|
| `slack.broadcast.auto_triggered` | INFO | chat_id, thread_ts, content[:200] |
| `slack.broadcast.intent_skipped` | INFO | chat_id, reason, content[:200] |
| `slack.broadcast.marker_already_present` | WARN | chat_id |
| `slack.broadcast.skipped_multi_chunk` | INFO | chat_id, chunks |
| `slack.broadcast.sent` | INFO | chat_id, thread_ts, content_len (기존, 변경 없음) |

shadow_miss 로그는 제거.

## 2. 최종 스코프

### 포함 (MVP)

- §1.2 토큰 매칭 + 명령형 어미 gate + 영어 bigram + 과거형 제외
- §1.3 `_apply_broadcast_intent` (`reply_to_inbound` 기반)
- §1.4 단일 chunk만 broadcast 가드
- §1.5 마커 끝 append
- §1.6 shadow detector 제거 + primary trigger 승격
- §1.7 slash command 분기
- §1.8 `MessageSender.send` runtime warning
- §1.10 구조화 로그
- 기존 회귀 가드 7개 모두 GREEN 유지
- 단위 테스트 + parametrized negative cases
- 운영 검증 (wishket-aidp 재배포 + 11개 E2E)

### 제외 (후속 PR)

- **마커 첫 chunk 상단 prepend** (다중 chunk 지원) — UX §4 + §6, Backend §1 — 단일 chunk 가드로 MVP 충분
- **Block Kit 컨펌 버튼** / **언두 메커니즘** — UX §1, §2 — 비가역 액션의 사용자 안전망, 별도 PR
- **Observer prefix 헤더** (`_📢 @user1 요청으로...`) — UX §6 — broadcast 메시지에 요청자 컨텍스트
- **미탐 시 스레드 각주** (`_(채널 공유 요청 — DM이라 생략)_`) — UX §8
- **영어 마커 분기 (i18n)** — UX §9
- **`MessageSender.send` 코드 완전 제거** — Backend 대안 1 — cleanup PR
- **연속 broadcast rate limit** (스레드 수명 동안 N회) — QA §추가 엣지
- **NLU 분류기로 패턴 정확도 ↑** — Backend 대안 2 — 미탐/오탐 누적 후

### 후속 우선순위

1. (가장 시급) Block Kit 언두 또는 컨펌 reaction — 오탐 1회 = 팀 전체 노출이라 사용자 신뢰 최우선
2. Observer prefix 헤더 — 채널 본문 가독성
3. 다중 chunk 지원 (마커 첫 chunk prepend)
4. 미탐 시 사용자 신호 (스레드 각주)
5. i18n
6. NLU 분류기

## 3. 구현 계획

**브랜치**: `feature/slack-broadcast-regex-fix` (또는 `holmes`에 직접 — 사용자 결정)

### Phase A: 패턴 함수 재작성

- [ ] `companio/core/loop.py` 상단에 토큰 상수, regex, `_detect_broadcast_intent` 새 구현 (§1.2)
- [ ] 기존 `_BROADCAST_TRIGGER_PATTERN` 제거
- [ ] 단위 테스트: `tests/test_broadcast_intent.py` (신규)
  - parametrized positive (≥15 케이스, 한·영, 명령형, standalone)
  - parametrized negative (≥15 케이스, 참조 발화, 질문, 과거형, 무관)
  - 빈 문자열, None 방어

### Phase B: `_apply_broadcast_intent` 헬퍼

- [ ] `companio/core/loop.py`에 `_BROADCAST_MARKER` 상수 + `_apply_broadcast_intent` 메서드 (§1.3)
- [ ] `AgentLoop.__init__`에 `_slack_broadcast_enabled`, `_slack_broadcast_blocked_channels` 인스턴스 변수 추가 (SlackConfig에서 읽기)
- [ ] 단위 테스트: `tests/test_apply_broadcast_intent.py` (신규)
  - 8 핵심 케이스 (happy path, 4 guards, priority, bypass, marker exact, dup-marker)

### Phase C: `_process_message` 흐름 변경

- [ ] shadow detector try/finally 블록 제거
- [ ] `_invoke_claude_turn`에서 `broadcast_intent_detected` 인자 제거
- [ ] `_process_message` 마지막에 intent 적용 호출 (§1.6)
- [ ] 단위 테스트: `tests/test_loop_broadcast_integration.py` (신규)
  - intent 매칭 + happy path → response에 marker + metadata 포함
  - intent 매칭 + DM → skip
  - slash command + intent → broadcast 미적용
  - response None 분기 → silent

### Phase D: `SlackChannel.send` 다중 chunk 가드

- [ ] `companio/channels/slack.py`에 `len(chunks) == 1` 가드 추가 (§1.4)
- [ ] 다중 chunk 시 INFO 로그
- [ ] 단위 테스트: `tests/test_slack_broadcast.py` 확장
  - `test_broadcast_skipped_when_split_into_multiple_chunks`
  - `test_broadcast_applied_when_single_chunk`
  - 기존 `test_slack_send_broadcasts_only_last_chunk` 의미 유지 (단일 chunk 케이스로 변경)

### Phase E: `MessageSender` runtime warning

- [ ] `companio/tools/message.py`의 `send()` 진입부에 warning 로그 (§1.8)
- [ ] docstring NOTE 추가
- [ ] 기존 `tests/test_message_sender_broadcast.py` 클래스명 → `TestShareToChannelDeadCodeContract`, 파일 상단 docstring에 "DEAD CODE CONTRACT (2026-04-11)" 명시

### Phase F: 기존 테스트 마이그레이션

- [ ] `tests/test_broadcast_shadow_detection.py`:
  - `test_shadow_logger_warns_on_unmatched_trigger` → 폐기
  - `test_shadow_logger_silent_when_tool_used` → 폐기
  - `test_shadow_pattern_matches_korean_phrases` → `test_broadcast_intent.py`로 이동 + 명령형 어미 케이스로 보강
  - `test_shadow_pattern_matches_english_phrases` → 동일
  - `"방송해주세요"` → `_BROADCAST_STANDALONE_TOKENS`에 `"방송해주"` 추가했으므로 standalone으로 분류

### Phase G: 회귀 가드 검증

- [ ] `_FORWARDED_METADATA_KEYS` 변경 없음 확인
- [ ] 다음 7개 테스트 모두 GREEN:
  1. `test_whitelist_is_the_source_of_truth`
  2. `test_reply_broadcast_not_in_whitelist`
  3. `test_inbound_with_reply_broadcast_does_not_leak`
  4. `test_loop_py_uses_only_reply_to_inbound` ← 가장 중요
  5. `test_slack_send_broadcasts_only_last_chunk` (또는 `test_broadcast_applied_when_single_chunk`로 의미 유지)
  6. `test_slack_send_drops_broadcast_when_not_channel`
  7. `test_slack_send_blocked_channel_strict_drop`

### Phase H: `_BROADCAST_MARKER` exact-string lock

- [ ] `tests/test_broadcast_intent.py` 또는 `test_apply_broadcast_intent.py`에 추가:
  ```python
  def test_broadcast_marker_exact_string_lock():
      from companio.core.loop import _BROADCAST_MARKER
      assert _BROADCAST_MARKER == "\n\n_📢 채널에도 공유되었습니다_"
  ```

### Phase I: 운영 검증

- [ ] `cd /home/holmes/compan.io && git pull` (editable install이라 즉시 반영)
- [ ] `systemctl --user restart companio-wishket-aidp`
- [ ] 11개 E2E (QA §운영 검증):
  1. `채널에도 공유해` → broadcast OK
  2. `채널 본문에도 브리핑해` → broadcast OK (원 버그 케이스)
  3. DM에서 동일 발화 → skip `not_channel`
  4. 채널 루트 (스레드 X) → skip `no_thread`
  5. flag off 인스턴스 → skip `disabled`
  6. blocked 채널 → skip `blocked`
  7. **오탐 방어**: `이 채널에 공지 떴는데 요약해줘` → 매칭 안 됨
  8. 6KB+ 응답 → `skipped_multi_chunk` 로그
  9. `/help 채널에도 공유해` → broadcast 미적용
  10. `auto_triggered` 로그에 chat_id, thread_ts, content 포함
  11. 피처 1·3과 동시 동작 (👀 + pulse + broadcast)

## 4. 리스크 및 대응

| 리스크 | 가능성 | 영향 | 대응 |
|---|---|---|---|
| 토큰 매칭 오탐 (의도치 않은 broadcast) | 중 | 높 | 명령형 어미 gate + 과거형 제외 + parametrized negative test 잠금. 발생 시 hot fix로 패턴 추가 |
| 토큰 매칭 미탐 (의도했는데 안 됨) | 중 | 저 | 사용자 재요청. metric 수집 후 패턴 보강 |
| 다중 chunk 응답에서 broadcast 누락 | 높 | 저 | 의도적 결정. 다중 chunk 지원은 후속 PR. 사용자에게 "응답이 길면 채널 공유 안 됨" 안내 (release note) |
| 회귀 가드 위반 | 저 | 높 | Phase G 명시적 검증. PR 머지 전 7개 GREEN 확인 |
| `MessageSender.send` 누군가 호출 | 저 | 중 | runtime warning 로그로 감지 |
| chunk split 경계가 marker를 잘림 | 저 | 저 | 단일 chunk 가드로 자연 회피. 추가로 marker 포함 여부 단위 테스트 |
| 사용자가 broadcast 인지 못함 (오탐 발견 지연) | 중 | 중 | marker로 1차 방어. 후속 PR에서 컨펌/언두 |

## 5. 검증 계획

### 단위 (CI 게이트)

Phase A~H 모든 테스트 통과. 특히:
- positive: 15+ parametrized
- negative: 15+ parametrized (오탐 방어)
- guard 8 핵심 케이스
- chunk split 가드 케이스 2개
- marker exact lock
- 7개 회귀 가드 GREEN

### Integration (수동, canary 배포 후)

11개 E2E (Phase I)

### 머지 후 1주일 metric 리뷰

- `slack.broadcast.auto_triggered` 발생 횟수 (≥1 = 성공 게이트)
- `slack.broadcast.intent_skipped` 분포 (각 reason별)
- `slack.broadcast.skipped_multi_chunk` 빈도 (높으면 다중 chunk 지원 우선순위 ↑)
- 사용자 피드백 (오탐 신고 / 미탐 신고)
- 임계값:
  - 오탐 신고 ≥1건 → 즉시 패턴 보강 hot fix
  - 미탐 신고 누적 → standalone 토큰 추가
  - skipped_multi_chunk ≥30% → 다중 chunk 지원 우선순위 ↑

## 6. 리뷰 반영 이력

| 피드백 | 출처 | 반영 | 사유 |
|---|---|---|---|
| `OutboundMessage(...)` 직접 생성자 → `reply_to_inbound` | Backend §2, QA §1 | ✅ | 회귀 가드 위반 즉시 감지, 가장 critical |
| 토큰 매칭에 명령형 어미 gate + 과거형 제외 | Backend §5, UX §3, QA §2 | ✅ | 오탐 방어 핵심 |
| 다중 chunk = broadcast 미적용 | Backend §1, UX §7, QA §4 | ✅ | 마커 + reply_broadcast 정합성 보장 |
| `MessageSender.send` runtime warning | Backend §5 | ✅ | dead code 가시화 |
| 8 핵심 guard 케이스 (16 X) | QA §3 | ✅ | fragile 회피 |
| `_BROADCAST_MARKER` exact-string lock | QA §5 | ✅ | 의도치 않은 변경 차단 |
| slash command + intent 분기 명시 | Backend §3, QA §5 | ✅ | early return으로 자연 처리, 테스트로 잠금 |
| 기존 7개 회귀 가드 GREEN 강제 | Backend §1, QA §1 | ✅ | Phase G 명시 |
| 영어 bigram 패턴 (`to channel`) | Backend §6 | ✅ | 영어 오탐 방지 |
| 마커 첫 chunk 상단 prepend | UX §4, §6 | ⏸️ 후속 | MVP는 단일 chunk만 broadcast → 자동 해결 |
| 컨펌 reaction / 언두 메커니즘 | UX §1, §2, §5 | ⏸️ 후속 | 별도 PR, 비가역 액션 안전망 |
| Observer prefix 헤더 | UX §6 | ⏸️ 후속 | broadcast 메시지 가독성 |
| 미탐 시 스레드 각주 | UX §8 | ⏸️ 후속 | 사용자 신호 |
| 영어 마커 분기 (i18n) | UX §9 | ⏸️ 후속 | 한국어로 시작 |
| `MessageSender.send` 완전 제거 | Backend 대안 1 | ⏸️ 후속 | cleanup PR |
| `chat_id` 정규화 (블록 리스트) | Backend §4 | ✅ (docstring) | 코드 변경 없이 schema 명시 |
| `auto_triggered` 로그에 content snippet | QA §7 | ✅ | 디버깅 기반 |
| `dead code contract` 테스트 클래스명 | QA §6 | ✅ | Phase E |
| Telegram 경로 cross-channel 테스트 | QA 추가 엣지 | ✅ | Phase D 단위 테스트 |
| Config reload 시 broadcast flag 동기화 | Backend 놓친 부분 | ⚠️ 인지 | 현 구조는 process restart 기반. 명시 |
| Rate limit (스레드당 N회) | QA 추가 엣지 | ⏸️ 후속 | 운영 데이터 보고 결정 |
