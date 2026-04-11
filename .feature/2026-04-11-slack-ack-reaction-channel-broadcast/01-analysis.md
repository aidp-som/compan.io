# 현황 분석: Slack 멘션 ACK 리액션 + 채널 공지 옵션

> 분석일: 2026-04-11
> 대상:
> - 피처 1: Slack 멘션 시 사용자 메시지에 리액션 이모지(👀/⏳)로 ACK
> - 피처 2: 사용자가 "채널에도 공유해" 등을 요청하면 스레드 응답을 채널 본문에도 broadcast

## 1. 현재 상태 요약

PR #2 (`904e2bc`) 머지로 thread isolation은 해결됐다. 모든 봇 응답(ACK, 중간, 최종)이 트리거 스레드 내부에 안전하게 떨어지며, `bus.py`에 `OutboundMessage.reply_to_inbound()` + `_FORWARDED_METADATA_KEYS` 화이트리스트가 도입돼 metadata 흐름이 명시적이다.

이 위에서 두 기능을 추가한다:
- 피처 1은 **Slack 전용**, 새 API(`reactions.add/remove`)와 새 scope(`reactions:write`) 필요
- 피처 2는 **이미 존재하는 Slack `reply_broadcast` 파라미터 활용**, scope 추가 불필요

두 기능 모두 핵심 데이터(`thread_ts`, `message_ts`)가 이미 화이트리스트에 있어 metadata 인프라는 그대로 재사용 가능하다.

## 2. 관련 코드 맵

| 파일 | 줄 수 | 역할 | 비고 |
|---|---|---|---|
| `companio/bus.py` | 118 | InboundMessage, OutboundMessage, `reply_to_inbound`, `_FORWARDED_METADATA_KEYS` | PR#2에서 도입됨. `message_ts`는 이미 화이트리스트 포함 |
| `companio/core/loop.py` | 434 | 메시지 처리 루프, ACK 발송, Claude CLI 호출, 최종 응답 | 7개 OutboundMessage 생성 지점 모두 `reply_to_inbound` 사용 |
| `companio/channels/slack.py` | 456 | Slack Socket Mode 채널, 이벤트 핸들러, `send()`, progress cache | `_progress_messages` per-thread asyncio.Lock 도입됨 |
| `companio/channels/telegram.py` | ~700 | Telegram 채널 (참고용) | typing 인디케이터 있음, 리액션 없음 |
| `companio/channels/base.py` | 130 | BaseChannel 추상 클래스 | 변경 없음 |
| `companio/tools/message.py` | 97 | LLM 노출 `MessageSender.send()` 툴 | LLM이 보낼 수 있는 옵션 추가 가능 |
| `companio/config/schema.py` | ~100 | SlackConfig, TelegramConfig 스키마 | 새 옵션 필드 추가 위치 |

### 핵심 진입점 (PR#2 이후 기준)

| 흐름 | 위치 |
|---|---|
| Slack `app_mention` 이벤트 수신 | `slack.py:_on_app_mention()` (mention 제거 + thread_ts 추출) |
| Slack 메시지/DM 수신 | `slack.py:_on_message()` |
| InboundMessage 발행 | `slack.py:_handle_message()` (base.py 경유) |
| 메시지 처리 | `loop.py:_dispatch()` → `_process_message()` |
| ACK "생각 중..." 발송 | `loop.py:_process_message()` 내 `OutboundMessage.reply_to_inbound(msg, "생각 중...", extra_metadata={"_progress": True})` |
| 최종 응답 발송 | 동일 `reply_to_inbound` 패턴 |
| Slack `send()` | `slack.py:send()` — `_progress` 분기, `chat_postMessage(thread_ts=...)` |
| Slack progress 캐시 | `slack.py:_progress_messages` + per-thread `asyncio.Lock` |

## 3. 아키텍처 현황

### 메시지 흐름 (PR#2 이후)

```
Slack app_mention event
  → _on_app_mention
      metadata = {
        "user_id", "thread_ts", "message_ts",   ← 둘 다 이미 캡처됨
        "is_channel": True
      }
  → InboundMessage (session_key="slack:{chat_id}:{thread_ts}")
  → bus.inbound queue
  → loop._dispatch (per-session lock)
  → loop._process_message
      ├─ ACK: OutboundMessage.reply_to_inbound(msg, "생각 중...",
      │       extra_metadata={"_progress": True})
      │       → metadata에 thread_ts, message_ts 자동 상속
      ├─ Claude CLI 호출
      └─ 최종 응답: OutboundMessage.reply_to_inbound(msg, result_text)
  → bus.outbound queue
  → channels.manager dispatcher
  → SlackChannel.send(msg)
      ├─ thread_ts = msg.metadata.get("thread_ts")
      ├─ if _progress and progress_key in cache:
      │     chat.update(...)  ← per-thread lock 보호
      └─ else:
            chat_postMessage(channel, text, thread_ts=thread_ts)
```

### Slack 토큰 scope (현재 추정)

| Scope | 사용 위치 |
|---|---|
| `app_mentions:read` | `app_mention` 이벤트 수신 |
| `chat:write` | `chat_postMessage`, `chat_update` |
| `im:history`, `im:read`, `im:write` | DM 채널 |
| `channels:history`, `groups:history` | 채널 메시지 수신 |
| `files:read`, `files:write` | 파일 업로드/다운로드 |
| `users:read` | (일부 봇만, 봇별로 다름) |

**부족한 scope:**
- `reactions:write` — 피처 1을 위해 추가 필요
- (선택) `reactions:read` — 기존 리액션 확인용

## 4. 도메인 컨텍스트

### 기존 패턴: progress lifecycle

PR#2의 `_progress_messages` 캐시는 **post → update → cleanup** 라이프사이클을 가진다:
1. 첫 ACK: `chat_postMessage` → cache에 ts 저장
2. 추가 progress: cache hit → `chat_update`
3. 최종 응답: cache pop

리액션도 동일한 lifecycle을 따라야 한다:
1. 멘션 수신 즉시: `reactions.add` (예: `eyes` 👀)
2. 처리 완료 시: `reactions.remove(eyes)` + 선택적으로 `reactions.add(white_check_mark)` ✅
3. 에러 시: `reactions.add(x)` 등 실패 표시

### `reply_broadcast` Slack API 동작

```python
chat_postMessage(
    channel=parent_channel_id,
    text=content,
    thread_ts=thread_ts,
    reply_broadcast=True,
)
```

- 메시지는 스레드 내부에 작성됨
- 동시에 채널 본문에도 "Also sent to #channel" 형태로 노출
- 단일 메시지 객체, 중복 없음
- `thread_ts` 없으면 무시됨 (DM이나 루트 메시지에서는 효과 없음)

## 5. 관련 이슈 및 히스토리

- **wishket-aidp/compan.io#2** (방금 머지됨, `904e2bc`): Slack thread isolation 버그 fix. 이 PR이 만든 인프라(`reply_to_inbound`, 화이트리스트, per-thread lock)가 두 신규 피처의 토대.
- 사용자 피드백 (대화 히스토리): "원래 (생각중) 이게 먼저 달렸던 거 같은데 안달려서" — progress 텍스트 메시지가 lock 대기에 묶여 보이지 않던 사례가 있었음. **리액션은 lock 밖에서 즉시 달 수 있어 이 문제를 해결한다.**
- 사용자 피드백: "생각 중..." 텍스트가 스레드를 어수선하게 만들고, 리액션 같은 가벼운 표시를 선호하는 정황.

## 6. 제약 사항 및 리스크

### 제약

1. **scope 추가 = 봇 reinstall 필요** — `reactions:write` 추가 시 모든 SOM Agent 등 운영 봇을 재설치해야 함. 운영 중단 동반.
2. **`message_ts` 정확성** — 리액션은 사용자 원본 메시지의 ts에 달려야 함. 현재 `_FORWARDED_METADATA_KEYS`에 `message_ts` 있어 inbound에서 outbound로 전달되지만, **이건 사용자 원본 메시지의 ts가 맞는지 검증 필요** (slack.py의 `_on_app_mention`에서 `event.get("ts")` 그대로 저장하는지).
3. **DM에서는 리액션도 가능하지만 reply_broadcast는 무의미** — 채널 vs DM 분기 필요. `is_channel` 화이트리스트 키로 판단 가능.
4. **Slack 리액션 권한** — 워크스페이스 정책에 따라 봇이 자기 메시지가 아닌 사용자 메시지에 리액션 추가하는 게 제한될 수 있음 (드물지만 확인 필요).

### 리스크

| 리스크 | 영향 |
|---|---|
| 리액션 추가 시점에 사용자가 메시지 삭제 → API 에러 | 로그만 남기고 무시 (graceful fallback) |
| 처리 완료 후 리액션 제거 실패 | UX 약간 어색하나 기능 영향 없음 |
| `reply_broadcast` 트리거 오탐 (사용자 의도 아닌데 채널 노출) | 채널 노이즈, 정보 누설 위험 |
| `reply_broadcast` 트리거 미탐 (요청했는데 안 됨) | 사용자 불만, 재요청 |

### 트리거 판단의 어려움 (피처 2 핵심 난점)

"채널에도 공유해" 같은 자연어 트리거를 누가 판단할 것인가? 두 가지 옵션:

- **(A) loop.py에서 사용자 메시지 텍스트 매칭** — 단순 정규식, 빠르고 비용 0, 그러나 오탐/미탐 가능성
- **(B) Claude(LLM)가 판단** — `MessageSender.send()` 툴에 `share_to_channel: bool` 파라미터 추가, Claude가 의도를 해석해 호출. 정확하지만 LLM 추론에 의존, 비결정적

이 결정이 피처 2의 핵심 트레이드오프이며 페르소나 리뷰에서 갈릴 수 있다.

## 7. 분석 소견

1. **인프라는 거의 다 준비됐다.** PR#2 머지로 metadata 흐름이 명시적이 됐고, `message_ts`가 이미 화이트리스트에 있어 리액션 구현이 깔끔하다. 두 피처 모두 핵심 변경은 50~150줄 수준으로 추정된다.

2. **피처 1의 진짜 가치는 UX 개선이 아니라 lock 회피.** 리액션은 `_dispatch`의 per-session lock과 무관한 별도 코드 경로(`_on_app_mention` 직후)에서도 추가 가능하다. 이렇게 하면 직전 요청이 처리 중이라 lock 대기 상태여도 사용자에게 즉시 ACK가 보인다 → 사용자가 봇이 죽었는지 살았는지 헷갈리는 문제 해결.

3. **피처 1은 텍스트 ACK를 완전히 대체할 수 있나?** 부분 가능. 처리 시간이 오래 걸릴 때 (>10초) 사용자는 리액션만으로 안심하기 어렵다. **하이브리드 권장**: 즉시 리액션 + 일정 시간 후 텍스트 fallback (예: 5초 이상 걸리면 "생각 중..." 텍스트도 송신).

4. **피처 2 트리거는 LLM 옵션이 우월할 가능성.** 사용자 의도가 미묘하다 — "이거 다른 사람도 알면 좋겠어"는 broadcast 대상이지만 정규식으로는 못 잡는다. Claude는 이미 컨텍스트를 이해하고 있으니 도구 옵션으로 노출하는 게 합리적. 단, LLM이 도구를 안 쓰는 케이스를 위해 **fallback 정규식도 병행** 고려.

5. **scope 추가 = 운영 부담.** SOM Agent를 막 켜놓은 상황에서 봇 reinstall이 동반된다는 점을 사용자가 인지해야 함. 한 번에 처리하는 게 좋다 — `reactions:write` + 다른 미래 scope(`reactions:read`)도 같이 미리 추가.

6. **회귀 가드 필요.** PR#2의 `TestNoBareOutboundInLoop`처럼 "리액션 ACK가 lock 밖에서 처리되는지" / "broadcast 옵션이 화이트리스트만 통과하는지" 같은 회귀 테스트를 함께 만들어야 다음 변경 때 망가지지 않는다.
