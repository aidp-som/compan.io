# 피처 설계: Broadcast 트리거 재설계 (LLM tool → regex)

> 설계일: 2026-04-11
> 기반: 01-analysis.md

## 1. 목표

- 사용자가 슬랙 봇에게 "채널에도 공유해", "채널 본문에도 브리핑해" 등으로 요청하면 응답이 스레드 뿐 아니라 채널 본문에도 노출되게 한다.
- LLM이 도구 호출로 의사결정하는 (불가능한) 경로를 포기하고, **사용자 입력에 대한 결정적 regex 매칭**으로 전환한다.
- broadcast 발생 시 사용자가 인지할 수 있도록 응답에 명시적 마커를 추가한다.
- 04-plan에서 도입한 안전장치 3중 가드(`is_channel`, `thread_ts`, `broadcast_enabled`)와 `broadcast_blocked_channels`는 그대로 유지한다.

## 2. 스코프

### In-Scope

- `_detect_broadcast_intent` 함수 패턴 개선 — `채널 본문에도`, `채널에 알려`, `채널에 올려`, `채널에 브리핑` 등 다양한 동사·중간어 형태 매칭
- `_process_message`의 shadow detector를 **primary trigger**로 승격: intent 감지 시 최종 OutboundMessage metadata에 `reply_broadcast=True` 자동 주입
- 3중 안전장치 + blocked_channels 검사를 외부 코드(loop.py)에서 수행 (원래 MessageSender에 있던 로직 일부 이동)
- broadcast 적용 시 응답 텍스트에 마커 추가: `"\n\n_📢 채널에도 공유되었습니다_"`
- 구조화 로그 변경: `slack.broadcast.shadow_miss` → `slack.broadcast.auto_triggered` (audit 목적, INFO 레벨)
- 회귀 가드 테스트 업데이트
- 운영 검증 (wishket-aidp 테스트)

### Out-of-Scope (후속 또는 다른 PR)

- **`MessageSender.send(share_to_channel=...)` 코드 제거**: dead code이지만 MCP 브리지 미래 가능성을 위해 유지. docstring에 "current behavior: dead code, replaced by regex trigger" 추가만.
- **MCP 브리지 구현**: companio core 변경 큰 작업, 별도 PR
- **Block Kit 컨펌 버튼**: regex가 결정적이라 컨펌 단계가 필요한지 재평가 필요. 일단 보류
- **다국어 확장**: 한국어 + 영어만, 일본어/중국어 등은 후속
- **Role-based broadcast 권한** (`can_broadcast` 플래그): 04-plan과 동일 — `broadcast_blocked_channels`로 우선 대응

## 3. 설계안

### 3.1 사용자 흐름

```
1. 사용자: @AIDP 다음주 미팅 일정 정리해서 채널에도 공유해
2. (즉시) 봇: 사용자 메시지에 👀 리액션 (피처 1, 변경 없음)
3. (5초) 봇: "생각 중... (약 5초)" pulse (피처 3, 변경 없음)
4. Claude 처리 완료
5. 봇 응답:
   ┌─────────────────────────────────────┐
   │ (스레드 안)                          │
   │ 다음주 미팅 일정입니다:                │
   │ - 월: ...                            │
   │ - 화: ...                            │
   │                                     │
   │ _📢 채널에도 공유되었습니다_           │
   └─────────────────────────────────────┘
6. (Slack reply_broadcast=True 효과로) 채널 메인 피드에도
   "Also sent to #channel" 형태로 같은 메시지 노출
7. 봇: 👀 리액션 제거
```

### 3.2 컴포넌트 설계

#### 변경 컴포넌트

**`_detect_broadcast_intent` 함수 (`companio/core/loop.py`)**

기존 단일 거대 regex → 토큰 기반 검사로 변경:

```python
_BROADCAST_CHANNEL_TOKENS = ("채널", "channel")
_BROADCAST_VERB_TOKENS = (
    # 한국어
    "공지", "공유", "알려", "알림", "올려", "올리", "브리핑",
    "방송", "보내", "전달", "안내",
    # 영어
    "broadcast", "announce", "share", "post",
)
_BROADCAST_STANDALONE_TOKENS = (
    "다른 사람도", "팀에 알", "팀에 공유", "팀에 공지",
    "모두에게", "모두한테", "everyone", "@channel", "@here",
)


def _detect_broadcast_intent(content: str) -> bool:
    """Heuristic: detect user intent to broadcast bot reply to parent channel.
    
    True if EITHER:
    - text contains a "channel" token AND a broadcast verb token (proximity-free)
    - text contains a standalone strong trigger phrase
    """
    if not content:
        return False
    text = content.lower()
    has_channel = any(tok in text for tok in (t.lower() for t in _BROADCAST_CHANNEL_TOKENS))
    has_verb = any(tok in text for tok in (t.lower() for t in _BROADCAST_VERB_TOKENS))
    if has_channel and has_verb:
        return True
    return any(tok in text for tok in (t.lower() for t in _BROADCAST_STANDALONE_TOKENS))
```

**`_process_message` (`companio/core/loop.py`)**

intent 감지 결과를 try/finally가 아닌 **최종 응답 metadata에 직접 주입**. 흐름:

```python
async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
    # ... 기존: preview log, session, slash commands ...
    
    broadcast_intent = _detect_broadcast_intent(msg.content)
    
    self.message_sender.set_context(msg)
    self.message_sender.start_turn()
    
    response = await self._invoke_claude_turn(msg, session, key, broadcast_intent)
    
    # broadcast intent 처리: response가 final outbound이면 metadata에 주입
    if response is not None and broadcast_intent:
        response = self._apply_broadcast_intent(msg, response)
    
    return response


def _apply_broadcast_intent(
    self, msg: InboundMessage, response: OutboundMessage
) -> OutboundMessage:
    """Inject reply_broadcast=True if all 3+1 guards pass.
    
    Guards:
    - is_channel
    - thread_ts present
    - broadcast_enabled config flag
    - chat_id not in broadcast_blocked_channels
    
    On guard fail: log INFO with reason, return response unchanged.
    On success: log INFO 'auto_triggered', append marker to content, set metadata.
    """
    inbound_meta = msg.metadata or {}
    is_channel = bool(inbound_meta.get("is_channel"))
    has_thread = bool(inbound_meta.get("thread_ts"))
    flag = self._slack_broadcast_enabled
    blocked = msg.chat_id in self._slack_broadcast_blocked_channels
    
    skip_reason = None
    if not is_channel:
        skip_reason = "not_channel"
    elif not has_thread:
        skip_reason = "no_thread"
    elif not flag:
        skip_reason = "disabled"
    elif blocked:
        skip_reason = "blocked"
    
    if skip_reason:
        logger.info(
            "slack.broadcast.intent_skipped chat_id={} reason={}",
            msg.chat_id, skip_reason
        )
        return response
    
    # All guards passed — apply broadcast
    new_metadata = dict(response.metadata)
    new_metadata["reply_broadcast"] = True
    
    new_content = response.content + _BROADCAST_MARKER
    
    logger.info(
        "slack.broadcast.auto_triggered chat_id={} thread_ts={}",
        msg.chat_id, inbound_meta.get("thread_ts")
    )
    
    return OutboundMessage(
        channel=response.channel,
        chat_id=response.chat_id,
        content=new_content,
        media=response.media,
        metadata=new_metadata,
    )


_BROADCAST_MARKER = "\n\n_📢 채널에도 공유되었습니다_"
```

**Shadow detector 제거**: 더 이상 audit 목적이 아님. intent 감지가 곧 행동이므로 try/finally 안의 shadow_miss 로그 블록 삭제. `_invoke_claude_turn` 시그니처에서 `broadcast_intent_detected` 인자 제거 (불필요).

**`MessageSender.send()` 변경**: 없음. dead code 그대로 유지. 단 docstring에 표시:
```python
"""...
NOTE (2026-04-11): The share_to_channel parameter is currently dead code.
Claude runs as a subprocess (`claude -p`) without an MCP bridge to this
Python class, so this method is never invoked from the LLM. Broadcast is
triggered via regex on user input in AgentLoop._process_message instead.
This signature is kept for a future MCP bridge.
"""
```

**`AgentLoop.__init__`**: 이미 `MessageSender(broadcast_enabled=..., broadcast_blocked_channels=...)`로 전달하고 있는데, AgentLoop 자체가 같은 값을 보유해야 함. 추가:
```python
self._slack_broadcast_enabled = ...  # from SlackConfig
self._slack_broadcast_blocked_channels = ...  # from SlackConfig
```

### 3.3 데이터 모델

변경 없음. `reply_broadcast`는 이미 outbound 전용 metadata로 정착됨. `_FORWARDED_METADATA_KEYS` 변경 없음.

### 3.4 API 설계

변경 없음. SlackChannel.send()의 `chat_postMessage(reply_broadcast=...)` 경로 그대로 활용.

### 3.5 상태 관리

추가 상태 없음. broadcast 결정은 stateless — 매 메시지마다 패턴 매칭 + 가드 검사.

## 4. 구현 전략

### 변경 파일 목록

| 파일 | 변경 |
|---|---|
| `companio/core/loop.py` | `_BROADCAST_TRIGGER_PATTERN` 제거, 토큰 기반 함수 + `_apply_broadcast_intent` 추가, `_process_message` 흐름 변경, shadow detector 제거, `_invoke_claude_turn` 시그니처 정리, `__init__`에 broadcast 설정 보유 |
| `companio/tools/message.py` | docstring에 "dead code" 명시 (코드 변경 없음). 또는 `share_to_channel` 파라미터 제거 (선택, 하단 대안 §5 참조) |
| `tests/test_broadcast_shadow_detection.py` | shadow_miss 테스트 → auto_triggered 테스트로 변경. 패턴 확장 케이스 추가 |
| `tests/test_message_sender_broadcast.py` | `share_to_channel`이 더 이상 사용되지 않음을 반영. 기존 테스트가 dead code를 검증하는 형태 — 일부 제거 또는 "dead code regression"으로 재명명 |
| `tests/test_loop_broadcast_intent.py` (신규) | `_apply_broadcast_intent` 단위 테스트 (모든 guard 조합) |
| `tests/test_thread_metadata_propagation.py` | 회귀 가드 변경 없음 (`reply_broadcast` 비-화이트리스트 잠금 유지) |

### 구현 순서

1. **A. 패턴 함수 개선** — `_detect_broadcast_intent` 토큰 기반으로 재작성, 단위 테스트 확장
2. **B. `_apply_broadcast_intent` 헬퍼 추가** — guard 검사 + metadata 주입 + content 마커 + 로그
3. **C. `_process_message` 흐름 변경** — shadow detector 블록 제거, broadcast intent 처리 추가
4. **D. `AgentLoop.__init__` broadcast 설정 보유** — 이미 MessageSender에 전달 중, AgentLoop 인스턴스 변수로도 노출
5. **E. `MessageSender` docstring 업데이트** — dead code 명시
6. **F. 테스트 업데이트** — shadow_miss → auto_triggered, 새 패턴 케이스, guard 조합
7. **G. 회귀 가드 검증** — `_FORWARDED_METADATA_KEYS` 변경 없는지, `reply_broadcast` 전파 차단 유지
8. **H. 운영 검증** — wishket-aidp 재배포 + 슬랙 멘션 테스트

### 마이그레이션

DB 마이그레이션 없음. config.json 호환 유지 (필드 변경 없음).

## 5. 대안 및 트레이드오프

### 대안 1: `MessageSender.send(share_to_channel=...)` 코드 완전 제거

- **장점**: dead code 정리, 시그니처 단순화, 미래 혼란 방지
- **단점**: 향후 MCP 브리지 도입 시 다시 추가 비용. 이미 작성된 코드 유실
- **결정**: **유지 + dead code docstring**. 정리는 별도 cleanup PR로 분리

### 대안 2: 패턴을 더 정교한 NLU로 확장 (예: 작은 LLM 분류기)

- **장점**: 정확도 ↑, 오탐/미탐 감소
- **단점**: 외부 의존 추가, 비용/지연 증가, 운영 복잡도 ↑
- **결정**: 현재 토큰 매칭으로 시작, 미탐/오탐 누적 관찰 후 재평가

### 대안 3: 사용자 컨펌 단계 추가 (Block Kit 버튼)

- **장점**: 비가역 액션의 사용자 통제, UX 개선
- **단점**: 구현 복잡도, 컨펌 클릭 마찰
- **결정**: regex가 결정적이라 우선 보류. 오탐 발생 시 다시 검토

### 대안 4: 응답에 마커 추가 X (조용한 broadcast)

- **장점**: 응답 텍스트가 더 깔끔
- **단점**: 사용자가 인지 못함, 학습 불가, 의도치 않은 노출 시 알아차리기 어려움
- **결정**: **마커 추가** — 사용자 신뢰와 학습성 우선

### 대안 5: shadow detector를 audit으로 유지 + LLM tool도 병행

- **장점**: 미래 LLM 경로 활성화 시 자동 호환
- **단점**: 두 경로 동시에 매칭되면 중복 broadcast. 복잡도 ↑
- **결정**: shadow detector 제거. 단일 경로만 유지. LLM 경로는 dead code 보존만
