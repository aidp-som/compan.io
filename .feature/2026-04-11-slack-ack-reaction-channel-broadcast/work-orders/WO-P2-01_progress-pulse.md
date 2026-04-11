# WO-P2-01: Progress Pulse 구현

> 대상 에이전트: **compan-io-agent** (compan.io)
> 우선순위: P2
> 상태: ⬜ 대기
> 생성일: 2026-04-11

## 1. 목적

처리 시간이 길어질 때 Slack progress 메시지를 5초마다 4번까지 update하여 사용자가 봇이 살아있음을 인지하게 한다. 04-plan.md PR 2 (Feature 3)의 모든 결정 사항을 따른다.

## 2. 컨텍스트

**필독:**
- `/home/holmes/compan.io/.feature/2026-04-11-slack-ack-reaction-channel-broadcast/04-plan.md` — §1.14a, §3 PR 2

**참조 코드:**
- `companio/channels/slack.py` — 기존 `_progress_messages` 캐시 + `chat.update` 경로 (slack.py 약 228-265 line). 이 인프라를 그대로 활용
- `companio/core/loop.py` — `_dispatch`, `_process_message`
- `companio/bus.py` — `OutboundMessage.reply_to_inbound`

**브랜치:**
- 시작: `git checkout holmes && git pull && git checkout -b feature/slack-progress-pulse`
- 작업 종료 시 브랜치 머물기, 커밋 X

## 3. 상세 요구사항

### 3-1. SlackConfig 스키마 확장 (`companio/config/schema.py`)

```python
progress_pulse_enabled: bool = False
```

### 3-2. 상수 정의 (`companio/core/loop.py` 상단 또는 별도 모듈)

```python
PULSE_INTERVAL_SECONDS = 5
PULSE_MAX_COUNT = 4
PULSE_TEXT_FORMAT = "생각 중... (약 {n}초)"
```

### 3-3. Pulse loop helper (`AgentLoop` 메서드)

```python
async def _progress_pulse_loop(self, msg: InboundMessage) -> None:
    """Send up to 4 progress updates at 5-second intervals.
    
    Each pulse re-publishes a _progress=True OutboundMessage with
    increasing elapsed-time text. SlackChannel's _progress_messages
    cache routes these to chat.update (PR#2 infrastructure reuse).
    """
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

### 3-4. `_dispatch` 통합

WO-P1-01의 try/finally 구조 위에 pulse task lifecycle 추가:

```python
async def _dispatch(self, msg: InboundMessage) -> None:
    success: bool | None = None
    pulse_task: asyncio.Task | None = None
    
    # Slack 채널이고 flag ON일 때만 pulse 시작
    if self._should_start_pulse(msg):
        pulse_task = asyncio.create_task(self._progress_pulse_loop(msg))
    
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
            logger.exception(...)
            await self.bus.publish_outbound(...)
        finally:
            # Cancel pulse FIRST (before final response settles)
            if pulse_task:
                pulse_task.cancel()
                try:
                    await pulse_task
                except asyncio.CancelledError:
                    pass
            # Then publish done lifecycle (WO-P1-01)
            ...

def _should_start_pulse(self, msg: InboundMessage) -> bool:
    """Pulse only for Slack channels with config flag enabled."""
    if msg.channel != "slack":
        return False
    # Need access to slack channel config — store it in AgentLoop
    # via __init__ or via channel manager
    return self._slack_progress_pulse_enabled
```

**중요**: `AgentLoop`가 SlackConfig flag를 알아야 함. 두 가지 옵션:
- (a) `AgentLoop.__init__`에 `slack_config: SlackConfig | None` 추가
- (b) `Config` 객체 전체를 받고 거기서 추출

**WO-P1-01과 통합**: P1이 먼저 머지되지 않은 상태에서 작업하므로, P2 브랜치는 holmes에서 분기. P1과 동일한 `_dispatch` 변경을 다시 만들지 말고, P1의 try/finally 구조 위에 pulse task만 추가하는 형태로 작성. 단, **P1과 충돌하지 않게** P2는 holmes 베이스 그대로에서 try/finally를 처음 도입하면서 pulse task도 같이 넣을 수 있음. control-agent가 PR 머지 시 충돌 해결.

대안: **P2를 `_dispatch` 변경 없이 구현 가능한가?** — `_process_message` 안에서 task lifecycle 관리 가능. 추천: `_process_message` 진입부에서 `pulse_task = asyncio.create_task(...)`, 빠져나갈 때 `pulse_task.cancel()` + `await`. 이러면 P1의 `_dispatch` 변경과 충돌 없음.

**최종 결정**: `_process_message`에서 pulse task lifecycle 관리. `_dispatch`는 건드리지 않음.

```python
async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
    # ... 기존 ACK 발송 ...
    
    pulse_task: asyncio.Task | None = None
    if self._should_start_pulse(msg):
        pulse_task = asyncio.create_task(self._progress_pulse_loop(msg))
    
    try:
        # ... 기존 Claude CLI 호출 ...
        return outbound
    finally:
        if pulse_task:
            pulse_task.cancel()
            try:
                await pulse_task
            except asyncio.CancelledError:
                pass
```

### 3-5. 신규 테스트 (`tests/test_progress_pulse.py`)

asyncio 시간 mock 패턴 사용. `asyncio.sleep`을 mock하거나 `freezegun` 또는 명시적 sleep으로:

- `test_pulse_sends_4_messages_at_5s_intervals` — sleep mock으로 4번 publish 검증
- `test_pulse_text_format_includes_elapsed_seconds` — "생각 중... (약 5초)" → "약 20초" 검증
- `test_pulse_max_count_4` — 5번째 sleep 없음
- `test_pulse_cancelled_on_fast_response` — Claude가 1초 만에 끝나면 publish 0회
- `test_pulse_cancelled_on_exception` — 예외 발생 시 pulse task 정리됨
- `test_pulse_cancelled_on_cancellation` — `_process_message` 취소 시 정리
- `test_pulse_disabled_flag_skips_loop` — `progress_pulse_enabled=False` → task 생성 X
- `test_pulse_skipped_for_non_slack_channels` — Telegram 등은 pulse 없음
- `test_pulse_publishes_with_progress_metadata` — `_progress=True` 포함 확인

## 4. 영향 파일 (예상)

| 파일 | 변경 유형 | 설명 |
|---|---|---|
| `companio/config/schema.py` | 수정 | `progress_pulse_enabled` 필드 |
| `companio/core/loop.py` | 수정 | `_progress_pulse_loop`, `_should_start_pulse`, `_process_message` 통합 |
| `tests/test_progress_pulse.py` | 신규 | 9 단위 테스트 |

## 5. 검증 기준

- [ ] `pytest tests/` 전체 통과
- [ ] `ruff check companio/ tests/` 통과
- [ ] 04-plan.md §1.14a, §3 PR 2 모든 항목 코드로 구현
- [ ] 00_qa-criteria.md Phase 2 (Q2.1~Q2.8) 모두 충족
- [ ] `_progress_messages` 캐시는 PR#2 그대로 재사용 (변경 금지)

## 6. 주의사항

- **`_dispatch` 변경 금지**: `_process_message` 안에서 처리 (P1과 충돌 회피)
- pulse task cancel은 finally에서 반드시 `await` (CancelledError 잡고)
- `_progress=True` metadata 발행하면 `SlackChannel.send()`가 자동으로 `chat.update` 경로로 라우팅함 — 새 코드 추가 불필요
- 5초 슬립 → 그래서 첫 pulse는 t=5초. 빠른 응답(<5s)은 pulse 0회
- `_FORWARDED_METADATA_KEYS` 변경 금지
- 커밋 금지

## 7. 결과 (작업 후 기록)

### 변경 파일
### 검증 결과
### 발견된 이슈
### 후속 작업
