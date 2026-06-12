# 현황 분석: Slack progress 메시지 thread 누락

> 분석일: 2026-04-10
> 대상: Slack 채널에서 `@SOM Agent` 멘션 시 "생각 중..." progress 메시지가 스레드 안이 아니라 채널 본문에 게시되는 현상.

## 1. 현재 상태 요약

Slack 채널에서 봇을 멘션하면 응답 플로우는 세 단계로 나뉜다: (1) 즉시 ACK "생각 중..." 메시지, (2) 중간에 Claude CLI 가 `message` 툴로 보내는 임의의 진행 메시지, (3) 최종 답변. 이 중 **세 번째(최종 답변)만** 스레드 안에 안착하고, **첫 번째(생각 중 ACK)와 두 번째(message 툴 호출)는 채널 본문에 떨어진다**. 원인은 코드가 한 곳(최종 답변 경로)에서만 inbound metadata 를 outbound 로 전달하고, 나머지 경로들에서는 metadata 딕셔너리를 새로 만들면서 `thread_ts` 를 빠뜨리기 때문이다.

## 2. 관련 코드 맵

| 파일 | 줄 | 역할 | 비고 |
|---|---|---|---|
| `companio/bus.py` | 29-37 | `OutboundMessage` 데이터클래스 | `metadata: dict` 는 기본값 `{}`, 채널별 힌트 통로 |
| `companio/channels/slack.py` | 179-232 | `SlackChannel.send()` | `msg.metadata.get("thread_ts")` 를 그대로 `chat_postMessage(..., thread_ts=...)` 에 전달. metadata 에 없으면 `None` → 채널 본문 게시 |
| `companio/channels/slack.py` | 283-321 | `_on_app_mention()` | 멘션 수신 시 `thread_ts = event.get("thread_ts") or event["ts"]` 로 **inbound** metadata 에 thread_ts 를 심어 bus 에 publish. Inbound 쪽은 올바름 |
| `companio/channels/slack.py` | 104, 185-230 | `_progress_messages` dict + `chat_update` 분기 | 같은 `(chat_id, thread_ts)` 에 대해 첫 progress 는 `chat_postMessage`, 이후는 `chat_update`. **`thread_ts` 가 없으면 progress_key 가 `"chat:"` 로 고정**되어 DM/채널 구분이 무너지고 여러 스레드가 같은 key 를 공유하는 부작용도 발생 |
| `companio/core/loop.py` | 177-183 | ACK "생각 중..." 송출 | **`metadata={"_progress": True}` 만 설정. `msg.metadata` 의 `thread_ts` 등 전혀 전달 안 함** ← 주 버그 |
| `companio/core/loop.py` | 128-130 | `/stop` 응답 송출 | metadata 없음 |
| `companio/core/loop.py` | 144-148 | 예외 핸들러 송출 | metadata 없음 |
| `companio/core/loop.py` | 164-167 | `/help` 응답 송출 | metadata 없음 |
| `companio/core/loop.py` | 307-311 | **최종 답변 송출** | `metadata=msg.metadata or {}` 로 올바르게 inbound metadata 전체를 forward. 이 경로만 정상 |
| `companio/core/loop.py` | 321-324, 332 | `/new` 응답 2곳 | metadata 없음 |
| `companio/core/loop.py` | 173 | `message_sender.set_context(...)` | `msg.metadata.get("message_id")` 를 건네지만 Slack metadata 키는 `message_ts` — 항상 `None` 이 저장됨 (단, Slack 에서 이 값은 지금 사용되지 않아 기능상 영향 없음) |
| `companio/tools/message.py` | 57-65 | `MessageSender.send()` — 에이전트가 대화 중 임의 메시지 송출용 툴 | `metadata={"message_id": message_id}` 만 설정. **`thread_ts` 없음** ← 2차 버그: Claude CLI 안의 에이전트가 중간 메시지를 보내면 마찬가지로 채널 본문에 떨어짐 |
| `companio/channels/telegram.py` | 363-368 | Telegram `send()` 의 `message_thread_id` 처리 | Slack 과 달리 `_message_threads[(chat_id, reply_to_message_id)]` 캐시를 거쳐 thread 정보를 추론하는 fallback 경로가 있음. Slack 은 이런 캐시 없음 |
| `companio/channels/manager.py` | 121-128 | outbound dispatcher | `send_progress` 가 꺼져 있으면 `_progress` 메시지 드롭 |

## 3. 아키텍처 현황

### 레이어 관점

companio 의 outbound 흐름은 다음과 같다:

```
[AgentLoop 또는 MessageSender 툴]
      │ publish_outbound(OutboundMessage)
      ▼
   MessageBus.outbound (asyncio.Queue)
      │
      ▼
[ChannelManager._dispatch_outbound]
      │ 채널별 라우팅
      ▼
[SlackChannel.send() 또는 TelegramChannel.send()]
      │ metadata 에서 thread 힌트 추출
      ▼
   Slack/Telegram API
```

**계약(implicit contract)**: 채널 쪽 `send()` 는 `msg.metadata` 안의 채널별 thread 키(Slack 의 `thread_ts`, Telegram 의 `message_thread_id`)를 읽어 해당 스레드에 메시지를 넣는다. 즉 outbound 를 만드는 쪽(코어 루프, 메시지 툴, 기타)이 **inbound 에서 받은 metadata 를 상속**해 주어야 한다.

이 계약이 문서로도 타입으로도 강제되지 않는다. `OutboundMessage.metadata: dict[str, Any]` 라 무엇이 들어가야 하는지가 호출자 관습에만 의존한다. 그래서 새 outbound 경로를 추가할 때마다 metadata 상속을 깜빡할 유인이 크다.

### 상태 관리

- `SlackChannel._progress_messages`: `{chat_id:thread_ts → message_ts}` 매핑. 동일 스레드 내 progress 메시지를 chat_update 로 덮어쓰기 위한 캐시. **thread_ts 가 None 이면 키가 `"C12345:"` 같은 모양이 되어** DM 이나 다른 스레드와 충돌할 위험 있음.
- `AgentLoop._active_tasks`, `_session_locks`: thread_ts 무관, session_key 로 직렬화.

### 도메인 컨텍스트

- Slack app_mention 이벤트는 **멘션이 발생한 메시지의 `ts` 와, 그 메시지가 이미 스레드 안이면 `thread_ts` 를 함께 준다**. `_on_app_mention` 은 `thread_ts = event.get("thread_ts") or event["ts"]` 로 올바르게 **"항상 스레드로 응답"** 의도를 코드화했다. 즉 채널 루트 멘션이면 그 멘션 자체가 새 스레드의 루트가 된다.
- Telegram 에는 "스레드" 개념이 포럼 토픽 외에는 없고, Slack 과 메타키 이름도 다르다(`message_thread_id` vs `thread_ts`). 두 채널은 metadata 상속을 필요로 한다는 점은 같지만, 키 이름은 다르다.

## 4. 제약 사항 및 리스크

- **채널 불가지론(channel agnostic) 필요**: 코어 루프는 "Slack" 을 몰라야 한다. `msg.metadata["thread_ts"]` 를 코어가 직접 복사하도록 하드코딩하면 Telegram 키(`message_thread_id`)까지 나열하는 부담이 생긴다. 일반적인 metadata 상속(dict 통째 복사) 쪽이 원칙적으로 맞다.
- **`_progress` 플래그와의 충돌 없음**: 기존 호출자들은 `metadata={"_progress": True}` 로 덮어쓰고 있어서, 상속한 뒤에 `_progress` 만 추가하는 패턴으로 바꾸면 하위 호환성 문제 없음. 채널 `send()` 는 metadata 를 **읽기만** 하므로 추가 키가 섞여 들어가도 무해.
- **`MessageSender` 툴의 metadata 확장**: 툴이 호출될 시점에는 "지금 어떤 inbound 를 처리 중인가" 라는 컨텍스트가 필요하다. 현재 `set_context(channel, chat_id, message_id)` 로만 전달되고 있어 thread_ts 가 빠져 있다. 시그니처를 확장해야 함.
- **`_progress_messages` 키 충돌**: thread_ts 가 `None` 일 때 키가 `"chat_id:"` 로 납작해지는 엣지 케이스가 남아 있어, 설령 ACK 가 정상적으로 스레드에 들어가더라도 과거 잔여 상태가 있으면 `chat_update` 가 엉뚱한 메시지를 덮어쓸 수 있다. 캐시 키 정규화 또는 방어 코드 필요.
- **역사적 맥락**: `d44c1ae feat: Slack 채널 지원 추가 (Socket Mode)` 이 Slack 초기 도입 커밋. 그 전까지는 Telegram 중심으로 설계되어 있었고, Telegram 은 `reply_to_message_id → message_thread_id` 캐시 fallback 덕분에 metadata 누락이 상대적으로 덜 드러났다. Slack 에서는 fallback 이 없어서 곧바로 증상이 나타남.
- **GitHub 이슈 부재**: upstream `wishket-aidp/compan.io` 는 issues 가 비활성이라 이 버그가 트래킹되고 있지 않음. PR 본문이 유일한 기록 수단.

## 5. 분석 소견

1. **단일 root cause, 다중 증상**: ACK "생각 중..." 이 채널 본문에 뜨는 건 `core/loop.py:177-183` 의 metadata 생성 실수이고, 이 같은 패턴이 `loop.py` 안의 5개 이상 다른 OutboundMessage 생성 지점에서 반복되고 있다. `tools/message.py:57-65` 도 같은 계열. 즉 **계약을 코드가 강제하지 못해서 생긴 시스템적 누수**로 봐야 한다.

2. **최종 답변이 정상 동작하는 것은 우연이 아닌 유일한 예외**: `loop.py:307-311` 에서만 `metadata=msg.metadata or {}` 로 inbound 전체를 상속한다. 다른 모든 경로는 metadata 를 "새로 만드는" 실수를 저질렀다. 즉 **수정 방향은 "나머지 경로도 상속하게 한다"** 로 명확하다.

3. **`_progress_messages` 캐시 키의 잠재적 위험**: 이번 버그를 고쳐서 `thread_ts` 가 항상 실릴 것이라고 가정하더라도, 과거 버그로 이미 캐시에 쌓였을 `"chat_id:"` 키는 리스타트 없이 그대로 남고, 새로운 정상 키 `"chat_id:1234567890.000100"` 와 공존한다. 런타임 캐시라 봇 재기동 시 사라지므로 치명적이진 않으나, 한 번쯤 정규화 정책을 명시하는 게 좋겠다.

4. **`MessageSender` 의 컨텍스트 API 가 작다**: `set_context(channel, chat_id, message_id)` 시그니처가 Slack/Telegram 양쪽의 thread 개념을 모두 담기에 부족하다. 해결책은 두 가지 — (A) 파라미터로 `thread_ts`, `message_thread_id` 등을 늘리거나, (B) 아예 `metadata: dict` 하나로 받아 통째 저장. (B) 가 채널 확장성 면에서 더 깔끔하다.

5. **리그레션 방지 필요**: 이 패턴의 누수는 새 outbound 지점이 추가될 때마다 또 발생할 수 있다. 테스트로 "inbound metadata 가 outbound 에 forward 되는가" 를 고정하거나, `OutboundMessage` 생성을 도와주는 헬퍼 함수(`OutboundMessage.reply_to(inbound, content, ...)`)를 도입해 호출자가 metadata 를 빠뜨릴 수 없게 만드는 방향이 유효하다.
