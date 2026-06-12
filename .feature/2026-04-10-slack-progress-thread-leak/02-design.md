# 피처 설계: Slack progress 메시지 thread 누락 수정

> 설계일: 2026-04-10
> 기반: 01-analysis.md

## 1. 목표

- **직접 증상 해결**: Slack 채널에서 봇 멘션 시 "생각 중..." ACK 와 중간 progress 메시지가 스레드 안에 남도록 한다.
- **파생 증상 해결**: `/stop`, `/help`, `/new`, 에러 핸들러, `MessageSender` 툴 경로까지 전부 원래 inbound 가 속한 스레드로 응답하도록 한다.
- **리그레션 방지**: 새 outbound 경로가 추가될 때 metadata 상속을 빠뜨리지 않도록 호출 관습을 잡는다.

## 2. 스코프

### In-Scope

- `companio/core/loop.py` 안의 5개 이상 outbound 생성 지점에서 inbound metadata 를 상속
- `companio/tools/message.py` 의 `MessageSender` 가 thread 컨텍스트를 보존
- `companio/core/loop.py:173` 의 `set_context` 호출을 새 API 에 맞춰 수정
- `companio/channels/slack.py` 의 `_progress_messages` 캐시 키가 `thread_ts=None` 일 때 붕괴되는 엣지 케이스 방어
- 해당 변경에 대한 단위 테스트 추가 (bus 레벨에서 outbound metadata 가 inbound 를 상속하는지 검증)

### Out-of-Scope

- Telegram 의 thread 처리 로직 재설계 — 현재 `_message_threads` fallback 으로 동작 중이므로 손대지 않음. 다만 이번 수정이 Telegram 의 기존 동작을 깨면 안 됨(테스트로 보장)
- `OutboundMessage` 데이터 클래스 자체를 metadata 강제 필드로 바꾸는 등의 스키마 변경 — 파급 범위 큼, 이번 스코프 밖
- `OutboundMessage.reply_to(inbound, ...)` 팩토리 메서드 도입 — 채택하면 깔끔하지만 선택 사항. 아래 Option C 참조.
- Slack `_progress_messages` 캐시의 상한·TTL 등 전반적인 memory hygiene — 이미 세션 종료 시 pop 하므로 당장 문제 없음
- GitHub 이슈 생성(upstream 이 issues 비활성)

### 후속 과제 (이번에 안 하지만 나중에 할 것)

- Telegram 쪽도 `_message_threads` fallback 없이 metadata 상속만으로 동작하는지 검토해 fallback 제거
- `OutboundMessage.reply_to()` 팩토리가 이번 결과로 충분히 효용을 보이면 다른 호출처에도 확산
- Slack 채널 메시지에서 봇 멘션 외의 경로(DM 등)에서도 `thread_ts` 의미가 정확한지 재검토 (현재 DM 도 metadata 에 thread_ts 를 실어 보내는데, DM 은 스레드 개념이 사실상 불필요)

## 3. 설계안

### 3.1 동작 변경 요약

| 상황 | 현재 | 변경 후 |
|---|---|---|
| Slack 채널 멘션 → "생각 중..." ACK | 채널 본문에 뜸 | 멘션이 속한/만든 스레드 안에 뜸 |
| Slack 채널 멘션 → 에이전트가 `message` 툴로 중간 메시지 | 채널 본문에 뜸 | 스레드 안 |
| Slack 채널 멘션 → `/stop`, `/help`, `/new`, 에러 응답 | 채널 본문에 뜸 | 스레드 안 |
| Slack 채널 멘션 → 최종 답변 | 스레드 안 (정상) | 동일 (변경 없음) |
| Slack DM | 스레드 개념 없음, 동일 | 동일 (변경 없음) |
| Telegram 전 경로 | 기존 fallback 으로 동작 | 동일 (변경 없음, 테스트로 검증) |

### 3.2 핵심 원칙

> **모든 outbound 메시지는 트리거가 된 inbound 의 metadata 를 상속한다. 추가 키는 덮어쓰기, 나머지는 보존.**

구체 구현 패턴:

```python
await self.bus.publish_outbound(
    OutboundMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content="생각 중...",
        metadata={**(msg.metadata or {}), "_progress": True},
    )
)
```

이 패턴을 `loop.py` 의 **모든** OutboundMessage 생성 지점에 일괄 적용한다.

### 3.3 변경 파일 목록 (레이어별)

**Business Logic 레이어**
- `companio/core/loop.py` — outbound 생성 6곳 수정
  - `_handle_stop()` (128): metadata 상속
  - `_process_message()` 의 에러 경로 (144): metadata 상속
  - `/help` 응답 (164): metadata 상속
  - ACK "생각 중..." (178): metadata 상속 + `_progress` 추가
  - `/new` 에러 응답 (321): metadata 상속
  - `/new` 성공 응답 (332): metadata 상속
  - `set_context` 호출 (173): 새 API 로 호출

**Tool 레이어**
- `companio/tools/message.py` — `MessageSender` 가 metadata dict 통째 보존
  - `__init__` / `set_context` 시그니처에 `metadata: dict | None = None` 추가 (기존 `message_id` 파라미터는 호환을 위해 그대로 두되, 내부적으로는 metadata 에 병합)
  - `send()` 가 `{**self._default_metadata, "message_id": message_id}` 형태로 구성

**Presentation(Channel) 레이어**
- `companio/channels/slack.py` — `_progress_messages` 캐시 키 정규화
  - `thread_ts` 가 `None` 이거나 빈 문자열이면 progress 캐시를 사용하지 않고(or 키를 DM 전용 `"dm:chat_id"` 로 명확화) 매번 새 메시지로 처리
  - 핵심 수정이 정상 작동하면 실제로는 이 케이스가 거의 발생하지 않지만, 방어 차원에서 명시

### 3.4 데이터 모델

`OutboundMessage` / `InboundMessage` 의 **스키마 변경 없음**. `metadata: dict[str, Any]` 라는 유연한 통로를 그대로 사용한다. 관습만 바꾼다.

### 3.5 구현 순서

의존 관계 고려 순서:

1. **`tools/message.py` 수정**: `MessageSender` 가 metadata dict 를 통째로 들고 다니도록 시그니처 확장
2. **`core/loop.py` 수정**:
   - `_process_message()` 내에서 `message_sender.set_context(...)` 호출을 새 API (`metadata=msg.metadata`) 로 변경
   - 6개 OutboundMessage 생성 지점에 metadata 상속 패턴 적용
3. **`channels/slack.py` 수정**: `_progress_messages` 키 정규화 (방어적)
4. **테스트 추가**:
   - 단위: 가짜 inbound → `AgentLoop._process_message` 를 mocked Claude CLI 로 태우고, 튀어나오는 outbound 들이 모두 inbound metadata 를 담고 있는지 검증
   - 단위: `MessageSender.send()` 호출 시 outbound metadata 에 `thread_ts` 가 보존되는지
   - 통합 레벨까지는 안 감 (Slack API mock 이 비싸므로 channel.send() 는 단위로 커버)
5. **수동 검증**: 재기동 후 Slack 에서 실제 멘션 테스트 (최소 3 케이스: 채널 루트 멘션, 기존 스레드 내 멘션, DM)

### 3.6 마이그레이션

없음. 런타임 캐시(`_progress_messages`) 는 봇 재기동 시 비워지므로 이전 잘못된 키가 쌓여 있어도 재기동 한 번으로 정리됨.

## 4. 대안 및 트레이드오프

### Option A — 최소 수정 (ACK 1줄만)

`loop.py:177-183` 만 고친다.

- **장점**: 변경 4~5줄, 리스크 최소, 5분 이내 머지 가능
- **단점**: `/stop`, `/help`, `/new`, 에러, 에이전트 툴 메시지가 여전히 본문에 새어 나감. 사용자가 "ACK 는 고쳐졌는데 왜 에러 메시지는 여전히 바깥에 뜨지?" 라고 재보고할 가능성 높음
- **선택하지 않는 이유**: 같은 root cause 의 다른 증상이 남아 있어 결국 두 번 들어가야 한다. 한 번에 처리하는 게 옳다.

### Option B — 모든 loop.py 지점 일괄 수정 **(권장)**

위에 기술한 설계. `loop.py` 의 6곳 + `tools/message.py` + Slack 캐시 방어 코드.

- **장점**: root cause 전면 해결, 파생 증상 모두 제거, 변경 범위 여전히 국소적(3~4 파일)
- **단점**: 각 호출 지점에 같은 패턴 `metadata={**(msg.metadata or {}), ...}` 이 반복돼 약간의 중복
- **선택 이유**: 트레이드오프가 가장 균형 잡혀 있음. 반복 패턴은 Option C 의 헬퍼로 후에 접을 수 있음

### Option C — 팩토리 도입 (Option B + `OutboundMessage.reply_to()`)

```python
@classmethod
def reply_to(cls, inbound: InboundMessage, content: str, **overrides) -> "OutboundMessage":
    metadata = {**(inbound.metadata or {}), **overrides.pop("metadata", {})}
    return cls(
        channel=inbound.channel,
        chat_id=inbound.chat_id,
        content=content,
        metadata=metadata,
        **overrides,
    )
```

호출자는 `OutboundMessage.reply_to(msg, "생각 중...", metadata={"_progress": True})` 로 단순화.

- **장점**: 호출 관습을 타입/API 레벨에서 안내. 리그레션 방지 효과 최상. 호출 지점마다 `{**(msg.metadata or {}), ...}` 반복 제거
- **단점**: `bus.py` 에 메서드 추가 + 호출처 전면 rewrite → 변경 파일 수 약간 증가, 리뷰 부담 증가. 또한 `MessageSender.send()` 는 inbound 객체를 들고 있지 않아 이 헬퍼만으로는 커버 안 됨 (여전히 metadata dict 직접 구성 필요)
- **선택 여부**: **페르소나 리뷰 후 결정**. 이번 PR 에 합쳐 넣을지, 후속 리팩터 PR 로 뺄지 리뷰어 의견에 따른다. 본 설계서의 기본 권고는 **Option B 로 먼저 고치고, Option C 는 후속 리팩터**.

### Option D — Channel 레벨에서 "마지막 inbound 를 기억" 하는 컨텍스트

`SlackChannel` 이 자기가 최근 처리한 inbound 를 `(chat_id → thread_ts)` 로 기억하고, `send()` 가 metadata 에 thread_ts 가 없으면 그걸 참조.

- **장점**: 코어 루프 코드 변경 없음
- **단점**: 암묵적 전역 상태, 동시 여러 스레드가 활성이면 race, Telegram 의 `_message_threads` fallback 과 같은 부류의 hack. 근본 원인을 덮을 뿐
- **선택하지 않는 이유**: 계약을 우회하는 방향이라 장기적으로 이해도를 해침. Telegram 쪽 fallback 도 기회가 되면 제거하자는 것이 분석 소견

## 5. 리스크

| 리스크 | 가능성 | 영향 | 대응 |
|---|---|---|---|
| 수정이 Telegram 경로를 깨뜨림 | 낮음 | 중 | 단위 테스트로 커버, 수동 Telegram 체크는 현재 환경에 없으면 생략 |
| `message.py` 시그니처 변경이 Claude CLI 툴 스키마에 영향 | 낮음 | 중 | `message_id` 파라미터 호환 유지, 새 파라미터는 내부 호출자만 추가 사용 |
| `_progress_messages` 캐시 정규화가 과거 키와 충돌 | 매우 낮음 | 낮음 | 런타임 캐시라 재기동으로 사라짐 |
| Slack DM 에 thread_ts 가 실려가며 의도 외 스레드가 생성 | 낮음 | 낮음 | DM 의 `thread_ts` 는 메시지 자신의 `ts` 라 Slack 서버 쪽에서도 "스레드 없음" 과 동등하게 처리. 별도 처리 불필요하지만 수동 확인 권장 |
