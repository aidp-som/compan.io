# 피처 설계: Slack 멘션 ACK 리액션 + 채널 공지 옵션

> 설계일: 2026-04-11
> 기반: 01-analysis.md

## 1. 목표

### 피처 1: ACK 리액션 이모지

- 사용자가 봇을 멘션한 즉시 가벼운 시각적 피드백("접수했음")을 제공
- 텍스트 ACK("생각 중...")가 채널/스레드를 어수선하게 만드는 문제 완화
- **부가 효과**: 리액션은 per-session lock 밖에서 발송 가능하므로, 직전 요청이 처리 중이어도 즉시 ACK가 표시됨 (사용자가 봇이 죽었다고 오해하는 문제 해소)

### 피처 2: 채널 공지 옵션

- 사용자가 "채널에도 공유해" 같은 의도를 표현하면 스레드 응답을 채널 본문에도 broadcast
- Slack의 `reply_broadcast=True` 파라미터 활용 (단일 메시지, 중복 없음)
- 의도적 정보 공유를 자연어로 트리거 가능하게 함

## 2. 스코프

### In-Scope

**피처 1:**
- Slack 멘션 수신 시 사용자 메시지에 처리 중 리액션(👀 `eyes`) 추가
- 처리 완료 시 처리 중 리액션 제거 + 완료 리액션(✅ `white_check_mark`) 추가
- 에러 발생 시 처리 중 리액션 제거 + 에러 리액션(❌ `x`) 추가
- 리액션 lifecycle 실패는 graceful — 메인 메시지 흐름에 영향 없음
- 텍스트 "생각 중..." ACK는 **선택적 fallback**: 처리가 5초 이상 걸리면 추가 발송 (config로 토글 가능)
- DM에서도 동일하게 적용 (Slack은 DM에도 리액션 가능)

**피처 2:**
- `MessageSender.send()` 툴에 `share_to_channel: bool = False` 파라미터 추가
- LLM이 사용자 의도("채널에도 공유해", "다른 사람도 알게 해" 등)를 해석해서 옵션 ON
- `OutboundMessage.metadata`에 `reply_broadcast` 키 추가 → `_FORWARDED_METADATA_KEYS`에는 추가하지 않음 (outbound 전용 옵션)
- `SlackChannel.send()`이 `reply_broadcast` 옵션을 `chat_postMessage`에 전달
- 채널 컨텍스트가 아닌 곳(`is_channel=False`, DM)에서는 무시
- 안전장치: `MessageSender.send()`가 channel/thread 컨텍스트에서만 broadcast 허용

### Out-of-Scope (후속 PR/티켓)

- **다양한 리액션 이모지 커스터마이징** (config로 이모지 변경) — 운영 편의 기능
- **리액션 기반 명령** (사용자가 ❌ 리액션 달면 봇이 응답 취소) — 별도 피처
- **자연어 트리거 정규식 fallback** — 일단 LLM 의존, 미탐 사례 누적 후 결정
- **Telegram 리액션** — Telegram Bot API의 reactions는 별개 인터페이스, 다른 PR
- **broadcast 권한 제어** (특정 역할만 broadcast 가능) — 필요시 RBAC 확장
- **`chat.postMessage` chunk 마지막 리액션 정리** — split된 메시지 중 어디에 ✅ 달지 결정 필요, 단순 케이스 먼저

## 3. 설계안

### 3.1 사용자 흐름

#### 피처 1 흐름

```
1. 사용자: @companio 오늘 일정 알려줘
2. (즉시, <500ms) 봇: 사용자 메시지에 👀 리액션 추가
3. (선택) 5초 후 처리 미완료 시: 스레드에 "생각 중..." 텍스트
4. Claude 처리 (수 초 ~ 수 분)
5. 봇: 스레드에 최종 응답 게시
6. 봇: 사용자 메시지에서 👀 제거 + ✅ 추가
   - 에러 발생 시: 👀 제거 + ❌ 추가
```

#### 피처 2 흐름

```
1. 사용자: @companio 다음주 미팅 일정 정리해서 채널에도 공유해
2. (1번 흐름과 동일하게 진행)
3. Claude 응답 생성하면서 share_to_channel=True 로 send() 툴 호출
4. 봇: 스레드에 응답 + 채널 본문에도 같은 메시지 노출 (reply_broadcast)
```

### 3.2 컴포넌트 설계

#### 새/수정 컴포넌트 (피처 1)

| 컴포넌트 | 종류 | 역할 |
|---|---|---|
| `SlackChannel._add_reaction()` | 신규 메서드 | `reactions.add` API 래퍼, 실패 graceful |
| `SlackChannel._remove_reaction()` | 신규 메서드 | `reactions.remove` API 래퍼, 실패 graceful |
| `SlackChannel.send_ack_reaction(inbound)` | 신규 메서드 | InboundMessage 받아서 즉시 처리 중 리액션 발송 |
| `SlackChannel.send_done_reaction(inbound, success)` | 신규 메서드 | 처리 완료 후 리액션 전환 |
| `loop.py` ACK 단계 | 수정 | 채널이 Slack이면 리액션 발송 + (선택) 텍스트 fallback 지연 발송 |
| `loop.py` 최종 응답 직후 | 수정 | 성공/실패에 따라 done reaction 호출 |
| `BaseChannel` | 신규 메서드 추가 | `async def send_ack(inbound) -> None` (no-op 기본), `async def send_done(inbound, success) -> None` (no-op 기본) |

**왜 BaseChannel에 추가하나?** — loop.py가 채널 종류를 직접 알지 않게 하기 위함. Telegram은 no-op이고 Slack만 리액션 구현. 나중에 다른 채널이 추가돼도 자기 ACK 전략을 자유롭게 결정.

#### 새/수정 컴포넌트 (피처 2)

| 컴포넌트 | 종류 | 역할 |
|---|---|---|
| `MessageSender.send(..., share_to_channel=False)` | 시그니처 확장 | LLM 노출 옵션. True면 metadata에 `reply_broadcast` 세팅 |
| `OutboundMessage.metadata["reply_broadcast"]` | 새 키 | outbound 전용, 화이트리스트 추가 X |
| `SlackChannel.send()` | 수정 | `metadata.get("reply_broadcast", False)` 읽어서 `chat_postMessage`에 전달, 단 `is_channel=True`일 때만 |
| `MessageSender.send()` 안전장치 | 신규 로직 | 현재 컨텍스트가 채널 스레드가 아니면 `share_to_channel` 무시 + 경고 로그 |

### 3.3 데이터 모델

#### 메타데이터 키 (변경)

| 키 | 위치 | 화이트리스트 | 용도 |
|---|---|---|---|
| `thread_ts` | inbound→outbound | ✅ 기존 | 스레드 응답 |
| `message_ts` | inbound→outbound | ✅ 기존 | 리액션 대상 |
| `is_channel` | inbound→outbound | ✅ 기존 | DM/채널 분기, broadcast 안전장치 |
| **`_progress`** | outbound 전용 | ❌ | 진행 중 메시지 표시 (기존) |
| **`_reaction_lifecycle`** (신규) | outbound 전용 | ❌ | "ack" / "done_success" / "done_error" 중 하나. send() 분기용 (선택) |
| **`reply_broadcast`** (신규) | outbound 전용 | ❌ | True면 채널 본문에 broadcast |

> 신규 키들은 outbound 전용이라 화이트리스트에 추가하지 않는다 — bus 경계를 건너지 않으므로.

#### LLM 도구 시그니처 (피처 2)

```python
class MessageSender:
    async def send(
        self,
        content: str,
        share_to_channel: bool = False,  # 신규
        media: list[str] | None = None,
        # ...기존 파라미터
    ) -> str:
        """Send message to current chat context.
        
        Args:
            content: 메시지 내용
            share_to_channel: True면 채널 본문에도 함께 공지 (스레드 컨텍스트에서만 유효).
                              사용자가 "채널에도 공유/공지/알려" 등을 명시할 때 사용.
            media: 첨부 파일
        """
```

### 3.4 API 설계 (Slack)

#### 새 API 호출

```python
# 리액션 추가
await self._app.client.reactions_add(
    channel=chat_id,
    name="eyes",  # without colons
    timestamp=message_ts,  # 사용자 원본 메시지의 ts
)

# 리액션 제거
await self._app.client.reactions_remove(
    channel=chat_id,
    name="eyes",
    timestamp=message_ts,
)

# broadcast 메시지 (기존 호출에 파라미터 추가)
await self._app.client.chat_postMessage(
    channel=chat_id,
    text=text,
    thread_ts=thread_ts,
    reply_broadcast=True,  # 신규
)
```

#### 추가 scope

- `reactions:write` (필수, 피처 1)
- `reactions:read` (선택, 기존 리액션 확인용 — 우선 미포함)

### 3.5 상태 관리

#### 리액션 상태 추적

```python
# SlackChannel에 신규 dict
self._active_ack_reactions: dict[tuple[str, str], str] = {}
# key: (chat_id, message_ts)
# value: 현재 달려있는 처리 중 이모지 이름 (예: "eyes")
```

처리 완료 시 이 dict를 보고 어떤 리액션을 제거할지 결정. 봇이 재시작되면 dict가 비어서 일부 리액션이 영구히 남을 수 있으나, **이건 허용 가능한 손실** — 다음 메시지 처리 시 새 리액션으로 덮임.

#### `_progress_messages`와 분리

기존 `_progress_messages` 캐시는 텍스트 progress 메시지 lifecycle용. 리액션은 별도 dict로 관리. 이름 충돌 없음.

## 4. 구현 전략

### 변경 파일 목록

| 레이어 | 파일 | 변경 |
|---|---|---|
| Channel | `companio/channels/slack.py` | `_add_reaction`, `_remove_reaction`, `send_ack`, `send_done`, `send()`에 `reply_broadcast` 처리 |
| Channel base | `companio/channels/base.py` | `async def send_ack(inbound)` no-op, `async def send_done(inbound, success)` no-op |
| Channel | `companio/channels/telegram.py` | (선택) `send_ack`/`send_done` no-op 명시 또는 base 상속 그대로 |
| Core | `companio/core/loop.py` | ACK 단계: 채널의 `send_ack` 호출. 응답 직후: `send_done` 호출. 텍스트 progress fallback 지연 발송 옵션 |
| Tool | `companio/tools/message.py` | `send()`에 `share_to_channel` 파라미터, 안전장치 (is_channel 체크) |
| Schema | `companio/config/schema.py` | `SlackConfig`에 `ack_reactions: dict` 필드 (이모지 이름 커스터마이징, 기본 eyes/check/x), `text_ack_fallback_seconds: int = 5` |
| Tests | `tests/test_slack_reactions.py` (신규) | reactions add/remove, lifecycle, graceful failure |
| Tests | `tests/test_message_sender_broadcast.py` (신규) | share_to_channel 옵션, is_channel 안전장치 |
| Tests | `tests/test_thread_metadata_propagation.py` | 회귀: 새 키들이 화이트리스트에 안 들어가는지 |

### 구현 순서

1. **Phase A: 기반 인프라**
   - `BaseChannel.send_ack` / `send_done` no-op 메서드 추가
   - `SlackConfig` 스키마에 옵션 필드 추가
2. **Phase B: 피처 1 (리액션)**
   - `SlackChannel._add_reaction`, `_remove_reaction` 헬퍼
   - `SlackChannel.send_ack`, `send_done` 구현
   - `loop.py` ACK 단계: `await channel.send_ack(msg)` 호출
   - `loop.py` 응답 직후 / 에러 핸들러: `await channel.send_done(msg, success=...)` 호출
   - 텍스트 fallback 지연 발송 (asyncio.create_task로 5초 후 progress 메시지)
   - 단위 테스트 작성
3. **Phase C: 피처 2 (broadcast)**
   - `MessageSender.send()`에 `share_to_channel` 파라미터 추가
   - 컨텍스트 안전장치 (is_channel=False면 무시)
   - `OutboundMessage.metadata["reply_broadcast"]` 세팅
   - `SlackChannel.send()`에서 `reply_broadcast` 읽어서 `chat_postMessage`에 전달
   - 단위 테스트 작성
4. **Phase D: 회귀 가드**
   - `_FORWARDED_METADATA_KEYS`에 신규 키가 추가되지 않았는지 검증하는 테스트
   - `is_channel=False`에서 broadcast 무시되는지 검증
5. **Phase E: 운영 적용**
   - 봇 앱 설정에서 `reactions:write` scope 추가 (운영자가 수동, SOM Agent 등)
   - 봇 reinstall
   - 인스턴스 재시작

### 마이그레이션

- DB 마이그레이션 없음
- 기존 config.json 호환 — 새 필드들 모두 기본값 있음

## 5. 대안 및 트레이드오프

### 대안 1: 피처 1에서 텍스트 ACK 완전 제거

- **장점**: 더 깔끔한 UX, 채널 노이즈 최소화
- **단점**: 처리가 매우 길어질 때 사용자 불안. 리액션이 작아 못 보고 지나칠 수 있음
- **결정**: 하이브리드 (즉시 리액션 + 5초 후 텍스트 fallback) 채택. config로 텍스트 fallback off 가능

### 대안 2: 피처 2 트리거를 정규식으로

- **장점**: 결정적, 빠름, 비용 0
- **단점**: 자연어 변형 다양함 ("채널에도", "사람들한테", "공유해줘", "broadcast" 등). 미탐 다수 예상
- **결정**: LLM 도구 옵션 채택. 미탐 사례 누적되면 정규식 fallback 추가 고려

### 대안 3: 피처 1 리액션을 lock 안에서 발송

- **장점**: 코드 흐름 단순, 기존 ACK와 동일 위치
- **단점**: 직전 요청이 처리 중이면 ACK 지연 → 사용자 체감 문제 그대로
- **결정**: 채널이 직접 `_on_app_mention`에서 lock 무관하게 즉시 리액션 발송하는 구조 채택. loop.py의 ACK 단계는 텍스트 fallback과 done reaction만 담당

### 대안 4: send_ack/send_done을 BaseChannel이 아닌 helper 함수로

- **장점**: BaseChannel 인터페이스 부풀리지 않음
- **단점**: 채널별 분기를 helper에서 다시 함 (isinstance 등). PR#2의 화이트리스트 철학과 안 맞음
- **결정**: BaseChannel 메서드로. 기본 no-op, Slack만 override
