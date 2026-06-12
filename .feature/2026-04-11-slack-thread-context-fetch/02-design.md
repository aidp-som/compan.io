# 피처 설계: Slack 스레드 컨텍스트 자동 fetch (A1 + A2)

> 설계일: 2026-04-11
> 기반: 01-analysis.md
> 사용자 결정: 옵션 A1(자동 pre-fetch) + A2(사용자 의도 패턴 매칭 후 limit 동적 조정). 옵션 B(in-process MCP 서버) 는 over-engineering 으로 보류.

## 1. 목표

- **핵심 사례 해결**: Slack 채널의 기존 스레드에서 봇이 멘션받았을 때, 그 스레드의 다른 메시지들을 봇이 인지하고 답할 수 있게 한다. 사용자가 "현재 스레드 내용 읽고 브리핑해" 라고 했을 때 정상 대답.
- **사용자 의도 인식**: "전체 다 읽어줘", "처음부터", "이전 N개" 같은 입력 시 fetch 깊이를 동적으로 조정 (A2).
- **인스턴스 격리**: 봇 인스턴스 간 자동 격리 (이미 companio 의 config-path 모델로 보장됨 — 새 코드도 그 모델 안에서 동작).
- **운영 안정**: 토큰 비용 통제(default limit + 상한), scope 누락 시 runtime auto-disable(`6eaa3ee` 패턴 차용), 캐시로 동일 스레드 단기간 중복 fetch 방지.
- **회귀 0**: 어제 PR #2 의 metadata 흐름과 source-level regression guard(`TestNoBareOutboundInLoop`) 를 깨지 않음.

## 2. 스코프

### In-Scope

- `companio/channels/slack.py` 에 `_fetch_thread_context()` 헬퍼 + OrderedDict 기반 TTL 캐시 추가
- `_on_app_mention` 에서 **`thread_ts != event["ts"]` 인 경우에만** 자동 fetch → inbound `content` 에 prepend
- A2: `_detect_thread_depth_intent(content)` regex 헬퍼 → 매칭 시 default limit 을 override(상한까지)
- `_handle_slack_api_error(exc, op_name)` 헬퍼를 `_handle_reaction_error` 의 9-case 매트릭스에서 추출/재사용 (또는 동일 패턴 중복 인라인). `missing_scope` 시 `_thread_context_runtime_disabled` 플립
- `companio/config/schema.py` 의 `SlackConfig` 에 새 flag 3개 추가 (`thread_context_enabled`, `thread_context_default_limit`, `thread_context_max_limit`)
- `tests/test_thread_context_fetch.py` 신규 — AsyncMock WebClient 기반 단위 테스트
- 사용자 display name 조회 (`users_info`) + 영구 캐시 — fetched 메시지의 가독성용
- 메트릭 로그: `slack.thread_context.fetch.ok|empty|cached|skipped|error|shadow_match` (`6eaa3ee` 의 로그 컨벤션 차용)

### Out-of-Scope

- **DM 컨텍스트**: DM 은 "다른 사람의 발화" 개념이 없고 봇의 session history 가 이미 충분. fetch 안 함.
- **active thread 후속 메시지**: `_on_message` 의 active thread 분기에서는 fetch 안 함. 이유: 봇이 이미 그 스레드의 과거 turn 을 session 에 보유. 한 번 engage 한 스레드는 LLM 메모리로 충분. (엣지 케이스: bot turn 사이에 외부 사용자가 추가 메시지를 끼움 → 다음 봇 turn 에서 그 메시지 누락. A2 의 "다시 읽어줘" 패턴으로 사용자가 명시 트리거 가능.)
- **채널 본문(스레드 밖) 메시지 fetch**: 채널 루트의 다른 메시지들은 fetch 하지 않음. 이번 작업은 *thread* 컨텍스트 한정.
- **In-process MCP 서버 / `slack_thread.read()` LLM 툴**: 옵션 B. 사용자 결정으로 보류. 향후 슬랙 통합이 깊어지면 별도 PR 로 검토.
- **PII 마스킹**: 별개 이슈. 본 작업은 스레드 텍스트를 그대로 LLM 에 전달. PII 정책은 후속 검토.
- **Telegram 영향**: 0. Telegram 은 thread 개념(`message_thread_id`)이 다르고 같은 클래스의 누락이 없음.
- **CronManager dead code 정리**: 분석 중 발견한 별건. 후속 PR.
- **사용자 display name 의 외부 변경 추적**: 캐시는 영구. Slack 사용자가 display name 을 바꾸면 봇 재기동 전까지 옛 이름 사용. 허용 트레이드.
- **`_on_message` active thread 의 외부 메시지 누락 대응 (cursor 추적)**: 향후 검토.

### 후속 과제 (이번에 안 하지만 나중에)

- `users_info` 캐시 invalidation (몇 시간 TTL or 봇 재기동 시 초기화)
- `_handle_slack_api_error` 의 helper 일반화 — `_handle_reaction_error` 와 통합. 이번 PR 에선 패턴 중복도 허용하되 다음 PR 에서 통합 가능
- DM 에서 사용자가 "이전 대화" 를 명시 요청한 경우 session DB 에서 조회하는 옵션
- A2 의 shadow_match 메트릭이 누적되면 분석해서 자주 못 잡은 의도를 정규식에 추가
- PII 마스킹 / 컴플라이언스 별개 PR

## 3. 설계안

### 3.1 데이터 흐름 (변경 후)

```
Slack app_mention event
    │ event = {channel, user, text, ts, thread_ts?, ...}
    ▼
SlackChannel._on_app_mention
    │ thread_ts = event.get("thread_ts") or event["ts"]
    │ message_ts = event["ts"]
    │
    ├─ if thread_ts != message_ts:  # existing thread (A1 trigger)
    │      requested_limit = self._detect_thread_depth_intent(content) or default_limit
    │      requested_limit = min(requested_limit, max_limit)
    │      context_text = await self._fetch_thread_context(chat_id, thread_ts, requested_limit)
    │      if context_text:
    │          content = f"{context_text}\n\n---\n\n{content}"
    │
    ▼
InboundMessage(content=<context + original text>, metadata={thread_ts, ...})
    │
    └─→ bus → AgentLoop → Claude CLI (변경 없음, 어제 PR #2 의 흐름 그대로)
```

### 3.2 새 컴포넌트

#### `_fetch_thread_context(chat_id, thread_ts, limit) -> str | None`

```python
async def _fetch_thread_context(
    self, chat_id: str, thread_ts: str, limit: int,
) -> str | None:
    """Fetch thread replies and return formatted context block.

    Returns None on disabled / scope-missing / empty / error so the caller
    can degrade gracefully (continue without context).
    """
    if not self.config.thread_context_enabled:
        return None
    if self._thread_context_runtime_disabled:
        return None
    if not self._app:
        return None

    # Cache check (lazy TTL eviction)
    cache_key = (chat_id, thread_ts, limit)
    cached = self._thread_context_cache_get(cache_key)
    if cached is not None:
        self._log.debug("slack.thread_context.fetch status=cached chat_id={} thread_ts={}", chat_id, thread_ts)
        return cached

    try:
        result = await self._app.client.conversations_replies(
            channel=chat_id, ts=thread_ts, limit=limit,
        )
    except Exception as e:
        return await self._handle_thread_context_error(e, chat_id, thread_ts)

    messages = result.get("messages", []) if result else []
    if not messages or len(messages) <= 1:
        # Single-message thread — nothing useful (the parent IS the mention)
        self._log.debug("slack.thread_context.fetch status=empty chat_id={} thread_ts={}", chat_id, thread_ts)
        self._thread_context_cache_put(cache_key, "")
        return None

    text = await self._format_thread_messages(messages)
    self._thread_context_cache_put(cache_key, text)
    self._log.info(
        "slack.thread_context.fetch status=ok chat_id={} thread_ts={} count={} chars={}",
        chat_id, thread_ts, len(messages), len(text),
    )
    return text
```

#### `_detect_thread_depth_intent(content) -> int | None`

```python
# Top-level constant (file scope), case-insensitive
_THREAD_DEPTH_PATTERNS = [
    (re.compile(r"전체|처음부터|모든\s*(메시지|대화|내용)|all\s+messages|from\s+the\s+beginning", re.IGNORECASE), "max"),
    (re.compile(r"이전\s*(\d+)|earlier\s+(\d+)|last\s+(\d+)", re.IGNORECASE), "captured"),
]

def _detect_thread_depth_intent(self, content: str) -> int | None:
    """Return a custom limit if user intent suggests broader thread fetch.

    None = use default. "max" = use thread_context_max_limit. captured int = use that
    (capped at max_limit). Logged as slack.thread_context.shadow_match for measurement.
    """
    for pattern, kind in _THREAD_DEPTH_PATTERNS:
        m = pattern.search(content)
        if not m:
            continue
        if kind == "max":
            self._log.info("slack.thread_context.shadow_match kind=max")
            return self.config.thread_context_max_limit
        if kind == "captured":
            for g in m.groups():
                if g and g.isdigit():
                    n = int(g)
                    self._log.info("slack.thread_context.shadow_match kind=captured n={}", n)
                    return min(n, self.config.thread_context_max_limit)
    return None
```

#### TTL 캐시 (`OrderedDict` 기반, `6eaa3ee` 의 `_active_ack_reactions` 패턴)

```python
# In __init__:
self._thread_context_cache: OrderedDict[tuple[str, str, int], tuple[float, str]] = OrderedDict()
self._thread_context_cache_max = 100
self._thread_context_cache_ttl = 30.0  # seconds
self._thread_context_runtime_disabled = False

def _thread_context_cache_get(self, key) -> str | None:
    entry = self._thread_context_cache.get(key)
    if entry is None:
        return None
    ts, text = entry
    if time.monotonic() - ts > self._thread_context_cache_ttl:
        # Lazy eviction
        self._thread_context_cache.pop(key, None)
        return None
    # LRU touch
    self._thread_context_cache.move_to_end(key)
    return text

def _thread_context_cache_put(self, key, text) -> None:
    self._thread_context_cache[key] = (time.monotonic(), text)
    self._thread_context_cache.move_to_end(key)
    # Bound the cache
    while len(self._thread_context_cache) > self._thread_context_cache_max:
        self._thread_context_cache.popitem(last=False)
```

#### `_format_thread_messages(messages) -> str`

```python
async def _format_thread_messages(self, messages: list[dict]) -> str:
    """Render Slack thread messages as a compact LLM-friendly text block."""
    lines = ["## Thread Context", ""]
    # Skip the last message (it's the current mention being processed)
    # Actually we keep it — see decision §3.4 below
    for m in messages:
        if m.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
            continue
        ts = m.get("ts", "")
        user_id = m.get("user")
        text = (m.get("text") or "").strip()
        if not text:
            continue
        display = await self._get_display_name(user_id) if user_id else "unknown"
        # Optional: human-readable time
        when = self._format_ts(ts)
        lines.append(f"[{display} {when}] {text}")
    return "\n".join(lines)

async def _get_display_name(self, user_id: str) -> str:
    if user_id in self._user_display_names:
        return self._user_display_names[user_id]
    try:
        info = await self._app.client.users_info(user=user_id)
        user = info.get("user", {})
        name = user.get("profile", {}).get("display_name") or user.get("real_name") or user_id
    except Exception:
        name = user_id  # graceful degradation
    self._user_display_names[user_id] = name
    return name
```

#### `_handle_thread_context_error(exc, chat_id, thread_ts) -> None` (9-case 매트릭스, `6eaa3ee` 패턴 차용)

| Slack error code | 동작 | 로깅 |
|---|---|---|
| `not_in_channel` | None 반환, WARN 1회 (봇이 그 채널에 없음) | `slack.thread_context.fetch status=not_in_channel chat_id=...` |
| `channel_not_found` | None, WARN | `status=channel_not_found` |
| `thread_not_found` | None, DEBUG (멘션 메시지가 아직 thread reply 가 아닌 케이스) | `status=thread_not_found` |
| `invalid_auth` | runtime_disable + ERROR | `status=invalid_auth runtime_disabled=true` |
| `missing_scope` | runtime_disable + ERROR (필요 scope 안내) | `status=missing_scope runtime_disabled=true required_scopes=channels:history,groups:history,im:history,mpim:history` |
| `ratelimited` | 1회 retry with `Retry-After` 헤더, 그 후 None | `status=ratelimited retry_after=N` |
| `account_inactive` | runtime_disable + ERROR | `status=account_inactive runtime_disabled=true` |
| 기타 SlackApiError | None, WARN | `status=error err=...` |
| 비-SlackApiError 예외 | None, WARN | `status=error err=...` |

### 3.3 Config 변경

```python
# companio/config/schema.py — SlackConfig 에 추가
class SlackConfig(Base):
    # ... 기존 필드 ...
    thread_context_enabled: bool = True   # default ON — 이 PR 의 핵심 가치
    thread_context_default_limit: int = 20
    thread_context_max_limit: int = 200   # Slack API 상한은 1000, 우리는 토큰 보호
```

**Default ON 결정 근거**: 이 PR 의 핵심 가치는 "스레드 안 메시지를 봇이 안다" 이다. default OFF 면 운영자가 알고 켜야만 효과 발생 → 보고된 사용자 불편이 그대로 남음. default ON + flag 로 끌 수 있음 + runtime_disabled 로 scope 부재 시 자동 fallback. 어제 머지된 `ack_reactions_enabled` 등은 default OFF 였는데, 그건 부수 기능이라 그랬고 본 작업은 trigger 가 된 사용자 보고에 대한 직접 응답.

### 3.4 컨텍스트를 어디에 끼울까 — content prepend vs metadata 분리

| 옵션 | 코드 변경 범위 | LLM 가독성 | 결정 |
|---|---|---|---|
| **content prepend** (선택) | slack.py 만 | 높음 (자연어 흐름) | ✅ 채택 |
| metadata 별 키 → loop.py 의 `_build_runtime_context` 에서 헤더 섹션으로 직렬화 | slack.py + loop.py + context.py | 중간 (별 섹션) | ❌ 보류 |

**근거**: prepend 는 slack.py 한 파일만 건드림. 어제 PR #2 의 metadata 흐름과 source regression guard 를 흔들지 않음. LLM 입장에서도 "Thread Context" 가 사용자 메시지 바로 앞에 있으니 자연스러움. metadata 분리는 깔끔하지만 loop.py 에 컨텍스트 합성 로직 추가가 필요하고 회귀 위험 vs 가치 비율이 낮음.

#### 정확한 prepend 형태

```
## Thread Context (스레드의 다른 메시지들 — 사용자가 멘션 직전까지의 대화 내역)

[김윤재 13:24] 카카오워크 vs 슬랙 비교 좀 정리해 줘
[이홍주 13:25] 어떤 측면에서?
[김윤재 13:26] 읽음 확인, 조직도, 메시지 구분
... (총 12개 메시지, 최근 12개 표시)

---

@SOM Agent 현재 스레드 내용 읽고 브리핑해
```

마지막 `---` 구분선과 멘션 원문이 LLM 에게 "이게 사용자 입력의 본체" 라는 신호. 모델은 thread context 를 참고해 답하면 됨.

### 3.5 `_on_app_mention` 변경 diff (의사 코드)

```python
async def _on_app_mention(self, event: dict) -> None:
    if event.get("bot_id"):
        return
    sender_id = event["user"]
    if not self.is_allowed(sender_id):
        return

    chat_id = event["channel"]
    raw_content = event.get("text", "")
    content = re.sub(rf"<@{self._bot_user_id}>", "", raw_content).strip()

    thread_ts = event.get("thread_ts") or event["ts"]
    message_ts = event.get("ts")
    self._active_threads.setdefault(chat_id, set()).add(thread_ts)

    media = await self._download_files(event)

    # ▼ NEW: A1 + A2 — fetch thread context if mention is into an existing thread
    if thread_ts != message_ts:  # existing thread (not a fresh channel root mention)
        requested_limit = self._detect_thread_depth_intent(content) \
                          or self.config.thread_context_default_limit
        requested_limit = min(requested_limit, self.config.thread_context_max_limit)
        context_text = await self._fetch_thread_context(chat_id, thread_ts, requested_limit)
        if context_text:
            content = f"{context_text}\n\n---\n\n{content}"
    # ▲ END NEW

    session_key = f"slack:{chat_id}:{thread_ts}"
    metadata = {
        "user_id": event["user"],
        "thread_ts": thread_ts,
        "message_ts": message_ts,
        "is_channel": True,
    }

    if self._should_send_ack(message_ts):
        asyncio.create_task(self._send_ack_reaction(chat_id, message_ts))

    await self._handle_message(
        sender_id=sender_id,
        chat_id=chat_id,
        content=content,
        metadata=metadata,
        session_key=session_key,
        media=media,
    )
```

`_on_message` 의 active thread 분기와 DM 분기는 **변경 없음**.

### 3.6 변경 파일 목록

| 파일 | 변경 유형 | 라인 추정 |
|---|---|---|
| `companio/channels/slack.py` | 수정 | +180~250 (`_fetch_thread_context`, `_detect_thread_depth_intent`, 캐시 헬퍼 2개, `_format_thread_messages`, `_get_display_name`, `_handle_thread_context_error`, `_on_app_mention` integration). 새 import: `time`, `OrderedDict`(이미 있음) |
| `companio/config/schema.py` | 수정 | +3 필드 |
| `companio/templates/TOOLS.md` | 수정 | "Slack thread context auto-injection" 한 줄 + 동작 안내 |
| `companio/templates/AGENTS.md` | 수정(선택) | LLM 이 thread context 가 prepended 됐을 때 어떻게 처리해야 하는지 한 줄 가이드. 사실상 자연어로 충분해서 미루어도 됨 |
| `tests/test_thread_context_fetch.py` | 신규 | ~250줄, 12~15 케이스 |

### 3.7 구현 순서

1. `SlackConfig` 에 새 필드 3개 추가 (가장 작은 변경)
2. `slack.py` 에 캐시·display name·error handler·fetch helper·shadow detection 헬퍼 작성 (top-half, `_on_app_mention` 호출되기 전 위치)
3. `_on_app_mention` 에 6줄 integration 추가
4. 테스트 작성: 캐시 hit/miss/expire, default limit, shadow match (max/captured/no-match), error matrix(missing_scope → runtime_disable, ratelimited → retry, etc.), prepend 형태 검증, single-message thread → empty 케이스
5. `templates/TOOLS.md` 한 줄 업데이트
6. ruff + pytest
7. 봇 재기동 + SOM Slack 에서 수동 검증

## 4. 대안 및 트레이드오프 (이미 사용자와 합의된 결정 정리)

| 대안 | 거부 이유 |
|---|---|
| 옵션 B (in-process MCP 서버) | over-engineering. 이 PR 의 단일 트리거(스레드 read) 에는 과한 인프라. 향후 슬랙 통합이 진지하게 커지면 별도 PR. |
| 옵션 D (외부 Slack MCP) | 인증·scope 분리. 사용자 의도와 충돌. |
| metadata 분리 (loop.py 변경) | 회귀 위험 vs 가치 비율 낮음. content prepend 로 충분. |
| `_on_message` active thread 에서도 fetch | 봇 session history 와 중복. 토큰 낭비. |
| DM fetch | DM 은 thread sibling 개념 없음. |
| 사용자 display name 영구 캐시 vs TTL | 영구 채택. display name 변경 빈도 낮음. 봇 재기동으로 reset. |
| Default OFF flag | trigger 가 된 사용자 보고가 영구히 미해결 상태로 남을 위험. Default ON. |

## 5. 리스크

| 리스크 | 가능성 | 영향 | 대응 |
|---|---|---|---|
| 봇 토큰에 `channels:history` 등 scope 부재 | 미확인 | 높음 (전 기능 무효화) | runtime_disable + ERROR 로그. 사용자에게 scope 추가 가이드. PR 본문에 prerequisite 명시 |
| 평균 스레드가 매우 길어 토큰 폭발 | 낮음 | 중 | default_limit 20, max_limit 200, 사용자 의도 패턴으로만 max 까지 |
| `users_info` 호출 누적으로 rate limit | 매우 낮음 | 낮음 | 영구 캐시. 첫 며칠만 누적, 이후 거의 0 호출 |
| 캐시 키 충돌 (다른 limit 으로 같은 스레드 fetch) | 중간 | 낮음 | 키에 limit 포함. limit 변경 시 별 entry 가 됨. 메모리 영향 미미 |
| `not_in_channel` 잦은 발생 (봇이 안 들어간 채널의 스레드 fetch 시도) | 중간 | 낮음 | WARN 1회 후 None. 동작 영향 없음 (멘션 자체가 봇이 들어간 채널에서만 발생) |
| Slack 사용자 발화의 PII 가 LLM 에 전달됨 | 중간 | 중 | 본 작업 스코프 밖. 후속 PII 마스킹 PR. PR 본문에 명시 |
| 어제 PR #2 의 source regression guard 깨짐 (`OutboundMessage` bare 호출) | 매우 낮음 | 낮음 | 본 PR 은 outbound 생성 안 함, inbound 생성 경로만 건드림 |
| `_handle_reaction_error` 와의 패턴 중복 | 낮음 | 낮음 | 일단 중복 허용. helper 일반화는 후속 |
| shadow detection 정규식의 오탐/누락 | 중간 | 낮음 | shadow_match 메트릭으로 측정. 정규식 보정은 운영 후 1주 데이터 모아서 |

## 6. 마이그레이션

없음. config 새 필드는 default 값 있어서 기존 인스턴스의 `config.json` 변경 불필요. 봇 재기동 시 자동 적용.
