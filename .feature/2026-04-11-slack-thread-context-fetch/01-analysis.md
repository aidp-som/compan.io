# 현황 분석: Slack 스레드 컨텍스트 fetch 한계 해소

> 분석일: 2026-04-11
> 대상: Slack 채널에서 봇이 멘션을 받았을 때 그 멘션이 속한 스레드의 다른 메시지들을 읽지 못하는 한계. 사용자 요청으로 옵션 C(하이브리드 — 자동 컨텍스트 주입 + 명시적 LLM 툴) 검토 시작.
> ⚠️ 분석 도중 발견된 핵심 사실 때문에 옵션 C 의 "명시적 LLM 툴" 부분이 현 아키텍처에서는 직접 가능하지 않음. 자세한 내용은 §7 분석 소견.

## 1. 현재 상태 요약

봇이 받는 inbound 는 **멘션 메시지 1개의 텍스트**만 담고 있다. 그 메시지가 속한 스레드의 부모/형제 메시지들은 전혀 가져오지 않는다. 사용자가 "현재 스레드 내용 읽고 브리핑해" 같은 요청을 해도 봇은 그 메시지 한 줄만 보고 답해야 하므로 정직하게 "스레드에 직접 접근할 도구가 없습니다" 라고 대답한다(2026-04-11 11:06 SOM Slack 사례). 같은 회사의 다른 머신에서는 사용자가 별도로 `~/.claude.json` 에 Slack MCP 서버를 등록해 둔 덕분에 우연히 동작했으나, 이 머신은 그런 등록이 없다.

## 2. 관련 코드 맵

| 파일 | 줄/위치 | 역할 | 비고 |
|---|---|---|---|
| `companio/channels/slack.py` | `_on_app_mention` (~283-321) | 멘션 이벤트 핸들러 | `event["text"]` 만 inbound 로 만듦. 형제 메시지 전혀 fetch 안 함 |
| `companio/channels/slack.py` | `_on_message` (~327-396) | 일반 메시지 이벤트 핸들러 | DM 과 active thread 내부 메시지 처리. 마찬가지로 단일 메시지만 |
| `companio/channels/slack.py` | `_active_threads: dict[str, set[str]]` | 채널별 활성 thread_ts 추적 | thread 내부 후속 메시지 라우팅용 — 컨텍스트 fetch 와는 별도 |
| `companio/channels/slack.py` | 138 | `_active_ack_reactions: OrderedDict` | 새 commit `6eaa3ee` 에서 도입. 같은 OrderedDict + TTL 패턴 차용 가능 |
| `companio/channels/slack.py` | `_app.client` | slack-bolt `AsyncWebClient` | 이미 `bot_token` 으로 인증된 핸들. `reactions_add`, `conversations_open`, `chat_postMessage`, `chat_update`, `files_upload_v2` 사용 중. **`conversations_replies` / `conversations_history` 호출 전무** |
| `companio/core/loop.py` | `_dispatch` (~132) | 인바운드 처리 분기 + finally 라이프사이클(`6eaa3ee` 에서 추가) | 여기서 ack 리액션 lifecycle 실행 |
| `companio/core/loop.py` | `_process_message` / `_invoke_claude_turn` (~201, ~260) | Claude CLI 호출 경로 | 컨텍스트가 들어가야 할 곳 |
| `companio/core/loop.py` | 234, 39 | `_detect_broadcast_intent`, regex shadow detection | 새 commit `1e10b8e` 의 shadow detection 패턴 — slack_thread 도 동일 패턴 차용 가능 |
| `companio/core/context.py` | `_build_runtime_context` (92-124) | 런타임 metadata 를 LLM 프롬프트 헤드에 주입 | 가능한 주입 지점 1: 여기에 thread excerpt 를 끼워넣기 |
| `companio/core/loop.py` | 215-247 | `runtime_ctx + "\n\n## Recent Conversation\n..." + "\n\n## Current Message\n..."` 조립 | 가능한 주입 지점 2: 여기에 `## Slack Thread Context` 섹션 삽입 |
| `companio/core/claude_cli.py` | `_build_cmd` (171-213) | Claude CLI subprocess 명령 조립 | **`--mcp-config` 같은 툴 주입 옵션 없음**. 내장 툴 + `<project_dir>/.mcp.json` 만이 LLM 툴 출처 |
| `companio/config/paths.py` | `sync_user_mcp_servers` (65-107) | `~/.claude.json` 의 top-level `mcpServers` 를 `<project_dir>/.mcp.json` 으로 동기화 | **인프라는 있는데 현재 머신에서는 sync 할 항목 없음**. 사용자가 `~/.claude.json` 에 등록만 하면 즉시 활용 가능 |
| `companio/cli.py` | 418-420 | "MessageSender cannot be injected into Claude CLI subprocess" 명시 코멘트 | LLM 이 companio 의 Python 함수를 직접 호출 못 함을 코드가 자인 |
| `companio/tools/message.py` | `MessageSender.send` (43-95) | "메시지 송신 툴" — 실제로는 in-process 호출자(코어 루프, cron 콜백) 만 사용 | LLM 이 호출 못 함. 어제 PR #2 가 `set_context(inbound)` 로 정리 |
| `companio/tools/cron_tool.py` | `CronManager` 클래스 전체 | "cron 툴" — `companio/` 어디서도 인스턴스화 안 됨 (`grep` 결과 self-reference 1건뿐) | **dead code 에 가까움**. 과거 MCP 시도의 잔재 또는 placeholder |
| `companio/templates/TOOLS.md` | 전체 | "companio-Specific Tools: message, cron" 문서 | 실제 LLM 호출 가능 툴이 아니라 **문서 컨벤션**. AGENTS.md 와 함께 시스템 프롬프트로 로드되어 LLM 의 자연어 출력 형식을 안내하는 역할 |
| `companio/config/schema.py` | `SlackConfig` (~37) | bot_token, app_token, allow_from, respond_in_thread + 새 commit 들의 ack_reactions_enabled / progress_pulse_enabled / broadcast_enabled 등 | 새 toggle (`thread_context_enabled` / `thread_context_limit`) 추가 위치 |
| `~/.claude.json` | top-level `mcpServers` | 사용자 스코프 MCP 서버 등록 | 현재 비어 있음(`{}`). 등록만 하면 `sync_user_mcp_servers` 가 자동 sync |
| `<project_dir>/.mcp.json` | (현재 미존재) | 프로젝트 스코프 MCP 서버 명세 | 파일 자체가 없음. find 결과 0건 |

### 새로 머지된 commit 들의 패턴 차용 잠재력

| commit | 차용 가능 패턴 | 적용 위치 |
|---|---|---|
| `6eaa3ee` (reaction ACK) | `OrderedDict + TTL bounding` (`_active_ack_reactions`), 9-case Slack API error matrix, runtime auto-disable on `missing_scope`, `_send_ack_reaction` lock 밖 task | thread fetch 캐시 / scope 누락 시 자동 비활성화 |
| `21aebd8` (progress pulse) | asyncio Task lifecycle (try/finally cancel), feature flag gate | 비동기 fetch 의 lifecycle 관리 |
| `1e10b8e` (channel broadcast) | regex shadow detection (`_detect_broadcast_intent`), shadow miss WARN 로깅 with metric name (`slack.broadcast.shadow_miss`) | LLM 이 실제로 "더 가져와" 같은 의도 표현 시 감지하기 위한 fallback (자동 fetch 가 못 잡은 케이스 측정용) |

## 3. 아키텍처 현황

### 인바운드 컨텍스트 흐름

```
Slack event (app_mention)
    │ event = {channel, user, text, ts, thread_ts?, ...}
    ▼
SlackChannel._on_app_mention
    │ thread_ts = event.get("thread_ts") or event["ts"]
    │ content   = event["text"] (멘션 토큰만 strip)
    │ metadata  = {user_id, thread_ts, message_ts, is_channel}
    ▼
InboundMessage(content=event["text"], metadata={thread_ts, ...})
    │ bus.publish_inbound
    ▼
AgentLoop._dispatch → _process_message → _invoke_claude_turn
    │
    ├─ runtime_ctx = ContextBuilder._build_runtime_context(...)
    │     채널/시간/sender/role 메타데이터를 텍스트로 직렬화 (~30줄)
    ├─ history_text = format_history(session.messages[-N:])
    │     이전 turn 들의 [user]/[assistant] 라인
    ├─ full_message = runtime_ctx + history + msg.content
    └─ claude.run(full_message)
            │
            ▼
        Claude CLI subprocess (`claude -p`)
            │ 시스템 프롬프트: <project_dir>/CLAUDE.md (AGENTS.md + SOUL.md + USER.md + TOOLS.md + MEMORY.md)
            │ 툴: built-in (Read/Write/Edit/Bash/...) + <project_dir>/.mcp.json (현재 없음)
            ▼
        Text response
```

**스레드의 형제 메시지가 들어갈 자리는 없음**. `runtime_ctx` 도 `history_text` 도 인바운드 채널의 다른 메시지를 끌어오지 않는다 — `history_text` 는 *이 봇과의 이전 대화* 만 다룬다(SQLite session).

### LLM 이 호출할 수 있는 도구의 진실

| 출처 | LLM 이 호출 가능? | 비고 |
|---|---|---|
| Claude CLI 내장 (Read/Write/Edit/Glob/Grep/Bash/WebFetch/WebSearch) | ✅ | `Bash` 는 현재 `--disallowedTools` 로 차단 |
| `<project_dir>/.mcp.json` 의 MCP 서버 | ✅ | 현재 파일 없음 → 0개 |
| companio 의 `MessageSender.send`, `CronManager.add_job` | ❌ | 별도 프로세스(claude -p)는 companio 의 Python 객체에 접근 불가 |
| TOOLS.md 에 "message", "cron" 으로 적힌 것 | ❌ | 문서 컨벤션. companio 가 LLM 출력/입력의 텍스트 패턴을 보고 사후 행동 |

### 정책 / 권한 흐름

- ACL: `BaseChannel.is_allowed(sender_id)` — `_handle_message` 진입 시 검증
- Role-based tool restriction: `loop.py:_resolve_role` → `--allowedTools`/`--disallowedTools` (Claude CLI 내장 툴에만 적용)
- Secret filtering: `helpers.filter_secrets(...)` — Claude CLI 응답 텍스트에서 토큰류 마스킹
- Slack Web Client 호출 경로: 모두 `SlackChannel.send`/`_on_*_mention` 안에서, 즉 채널 어댑터의 책임 영역 안

### 어제 PR #2 의 영향 (긍정)

- inbound metadata 가 `_FORWARDED_METADATA_KEYS` 화이트리스트로 깨끗하게 outbound 까지 흐른다 — `thread_ts` 가 안정적으로 metadata 를 타고 흐름
- `MessageSender.set_context(inbound)` 로 inbound 자체에 바인딩되는 패턴 정착 — 새 컴포넌트가 inbound 의 thread_ts 에 의존하기 쉬워짐
- `OutboundMessage.reply_to_inbound` 팩토리로 thread context 가 자연 상속 — pre-fetch 한 메시지를 outbound 에 직접 첨부해도 안전
- `loop.py` 의 source-level regression guard (`TestNoBareOutboundInLoop`) — 새 코드가 bare `OutboundMessage(...)` 추가하면 즉시 실패. 이번 PR 도 이 가드를 깨면 안 됨

## 4. 도메인 컨텍스트

- **Slack `conversations.replies` API**: 주어진 `(channel, ts)` 의 스레드 메시지를 페이지네이션으로 반환. `limit` 기본 10, 최대 1000. 필요 scope: `channels:history`(public), `groups:history`(private), `im:history`(DM), `mpim:history`(group DM). 최소한 지금 SOM 봇이 사용하는 채널 종류에 맞는 scope 가 다 있어야 함.
- **현재 SOM 봇 토큰의 scope 미확인**: `slack_app_manifest.yaml` 류 파일이 repo 에 없음. 운영자가 Slack admin 에서 직접 부여. `6eaa3ee` 의 reaction 기능이 실제로 동작하려면 `reactions:write` 가 필요한데 이 머신은 아직 그 기능 미가동(봇 재기동 안 됐고 flag 도 default `False`). **scope 확인이 본 작업의 prerequisite**.
- **Slack Bot 의 thread 인식**: app_mention 이벤트에서 `thread_ts` 가 있으면 그 멘션은 기존 스레드 안. 없으면 채널 루트 멘션이고 `_on_app_mention` 에서 `thread_ts = event["ts"]` 로 자기 자신을 thread root 로 삼는 관행이 어제 PR #2 로 확립됨. 즉 컨텍스트 fetch 시 "스레드 루트" 와 "방금 멘션된 메시지" 가 같을 수 있음 — 이 경우 fetch 결과는 멘션 메시지 1개. 즉 자동 fetch 의 의미가 없음. **fetch 는 thread_ts != message_ts 인 경우에만 의미**.
- **PII / privacy**: 스레드 안에 다른 사람들의 프라이빗한 발화가 있을 수 있음. 봇이 그걸 fetch 해서 LLM 컨텍스트로 보내면, LLM 응답이 그 내용을 의도치 않게 인용/요약해 또 다른 사람에게 노출될 수 있음(예: 같은 채널 멘션이지만 발화자가 다름). 어제 PR #2 가 outbound 를 thread 안에 가두는 패턴을 만들었기 때문에 격리는 일정 수준 보장되지만, **PII 인식 (예: 비밀번호 패턴, 이메일/전화번호 마스킹) 은 별개 책임** — 현재 `helpers.filter_secrets` 만 있고 PII 마스킹은 없음.
- **토큰 비용**: 평균 SOM 채널 스레드 길이가 5~30 메시지 정도라고 가정하면, 자동 fetch 는 멘션당 +500~3000 input 토큰. 큰 스레드(50+ 메시지) 는 이상치. 캐시는 기존 turn-level 단위라 도움 안 됨. 매번 비용이 발생.

## 5. 관련 이슈 및 히스토리

- **GitHub 이슈**: upstream `wishket-aidp/compan.io` 는 issues 가 비활성. 2026-04-11 SOM Slack 의 사용자 보고가 유일한 trigger.
- **관련 commit**:
  - `d44c1ae feat: Slack 채널 지원 추가 (Socket Mode)` — Slack 채널의 출생. 이 시점부터 thread fetch 누락이 잠재
  - `904e2bc fix: Slack 멘션 응답이 스레드 밖 채널 본문에 게시되는 버그 수정 (#2)` — PR #2, 어제 머지. metadata 흐름 정리. 본 작업의 기반
  - `6eaa3ee, 21aebd8, 1e10b8e, 00f30e8` — 어제 머지된 reaction ACK / pulse / broadcast / 그 기획 산출물. 본 작업과 직교하지만 패턴 차용 풍부
  - `891127e feat: add companio-init skill` — companio 인스턴스 부트스트랩 스킬. MCP 자동 설정은 아직 안 함
- **다른 머신의 우연한 동작**: 사용자 진술에 의하면 같은 회사의 다른 companio 인스턴스에서 스레드 read 가 됐던 것은 그 머신의 `~/.claude.json` 에 별도 Slack MCP 서버가 등록되어 있었기 때문으로 추정. 즉 **머신 의존 우회로**였고 봇 자체의 능력은 아니었음.

## 6. 제약 사항 및 리스크

- **🔴 [핵심 제약] LLM 이 companio 의 Python 함수를 직접 호출할 수 없다**. 옵션 C 의 "명시적 툴이 LLM 호출에 응답한다" 는 현 아키텍처에서 가능하지 않다. 가능한 경로는 (a) MCP 서버 등록(외부 또는 in-process 신규 구축), (b) 자동 pre-fetch (LLM 의 의지와 무관), (c) shadow detection 으로 인입 메시지 패턴 매칭 후 pre-fetch 파라미터 조정.
- **Scope 미확인**: 현재 SOM 봇 토큰에 `channels:history`/`groups:history`/`im:history`/`mpim:history` 가 부여돼 있는지 미확인. 부재 시 새 구현은 모두 `missing_scope` 에러로 failsafe. `6eaa3ee` 가 만든 runtime auto-disable 패턴(`_ack_reactions_runtime_disabled`) 을 그대로 차용해야 안전.
- **토큰 비용 일정 증가**: 매 멘션마다 자동 fetch 를 수행하면 평균 +1000 input 토큰 / 멘션. 비용 사용자 부담 가능성. feature flag + 조절 가능한 limit 필요.
- **PII 노출**: 스레드 안의 다른 발화를 LLM 이 보면 인용/요약 형태로 outbound 에 포함될 수 있음. 봇은 같은 스레드로 응답하므로 **원래부터 그 스레드에 있던 사람만 본다** 는 점이 일부 완화하지만, 개인정보 보호 측면 검토 필요.
- **스레드 길이 방어**: limit 없이 fetch 하면 1000 메시지짜리 거대 스레드가 LLM context window 를 폭파할 수 있음. limit 강제 + 잘림 정책 명시 필요.
- **PR #2 의 source regression guard (`TestNoBareOutboundInLoop`)**: 새 코드가 `OutboundMessage(...)` 직접 호출 시 깨짐. fetch 결과를 outbound 로 보내는 일은 이 PR 에 없을 가능성 높지만 주의.
- **`_active_threads` 와의 상호작용**: 스레드 fetch 후 LLM 이 응답을 그 스레드에 내면 `_active_threads` 가 자동 추적. 그 후 다른 사용자의 일반 메시지(비-멘션)도 같은 스레드 안에 들어오면 봇이 응답해야 함(현재 구현). 자동 fetch 가 매번 일어나면 비용 합산. 이 활성 스레드 인입에는 fetch 를 안 하거나 캐시를 둘 필요 있음.
- **Telegram 영향 0**: 본 작업은 Slack 한정. Telegram 는 이미 `replied_message_id` 등 별도 컨텍스트가 있어 같은 클래스의 문제 아님.
- **MessageSender 의 LLM 비호출 사실은 이번 작업의 reference architecture**: 새 컴포넌트도 같은 가정 위에서 설계해야 함 — LLM 이 직접 호출하는 게 아니라 inbound 흐름 또는 outbound 흐름의 적절한 hook 에서 동작.

## 7. 분석 소견

1. **옵션 C 재정의 필요** — 사용자와 합의한 옵션 C("자동 컨텍스트 주입 + 명시적 LLM 툴") 의 후반부가 현 아키텍처에서는 직접 가능하지 않다는 사실을 분석 도중에 발견했다. 진짜 LLM-callable 툴을 만들려면 별도의 MCP 서버 인프라(외부 등록 또는 in-process 신규 구축) 가 필요하다. **이 발견을 02-design.md 에서 사용자에게 명확히 제시하고 4개의 viable 경로를 비교**해야 한다. 4개 경로:
   - **A1**: 자동 pre-fetch 만 (가장 단순)
   - **A2**: 자동 pre-fetch + shadow detection 기반 re-fetch (의사 hybrid — "툴 느낌" 모방)
   - **B**: companio 가 in-process MCP 서버를 띄우고 `slack_thread.read` 진짜 툴 노출 (큰 스코프, 사용자 의도에 가장 충실)
   - **D**: 외부 Slack MCP 서버를 `~/.claude.json` 에 등록하고 sync (가장 빠르지만 사용자가 거부했던 인증/scope 분리 문제 그대로)

2. **사용자의 의도 보존**: 사용자는 "MCP 우회 금지" 라고 했지만 그 본질적 우려는 **인증/컨텍스트/권한 일관성**이었다. 옵션 B(in-process MCP) 라면 같은 프로세스 내라 인증·컨텍스트 공유가 가능하다 — 사용자 의도와 정합. 옵션 D(외부 MCP) 만 사용자 의도와 충돌. A1/A2 는 "LLM 툴" 이 아니라는 점에서 사용자 의도와 부분적으로만 일치하지만 인증/컨텍스트 면에서는 가장 깨끗.

3. **비용 인지가 중요한 결정 변수**: 옵션 A1 은 "매번 자동" 이라 비용이 일정. 옵션 B 는 "필요할 때만" 이라 비용 변동. 매월 슬랙 멘션 N건 × 평균 1000 토큰을 추정하면 운영자가 결정할 만한 숫자. 02-design.md 에 budget 시뮬레이션을 포함해야 한다.

4. **`6eaa3ee` 의 Slack API 에러 매트릭스를 그대로 재사용**: `_handle_reaction_error` 의 9-case 패턴(`already_reacted`, `missing_scope`, `not_in_channel`, `ratelimited` 등) 은 `conversations.replies` 에도 동일하게 필요. helper 를 채널 어댑터에 일반화할 가치 있음(`_handle_slack_api_error(exc, op_name)`).

5. **shadow detection 의 한계와 가치**: `1e10b8e` 의 `_detect_broadcast_intent` 가 보여준 것 — regex 로 사용자 의도 캡처 가능하지만 recall 이 낮다 (그래서 `slack.broadcast.shadow_miss` 메트릭으로 미스 케이스 측정하는 인프라까지 만들었다). thread fetch 도 같은 패턴으로 갈 수 있지만, 옵션 A1(자동) 이 있다면 shadow 가 보완재로 잘 어울림.

6. **scope 확인을 design 단계의 prerequisite 로**: 토큰 scope 가 부족하면 아무리 좋은 설계도 동작하지 않는다. 02-design.md 작성 전 또는 03-review 직전에 운영자(사용자)에게 "현재 SOM 봇의 OAuth scope 목록을 Slack admin 에서 확인 부탁" 요청을 하는 게 좋다. 부족하면 scope 추가가 본 작업의 가장 첫 단계가 된다.

7. **CronManager dead code 정리도 후속 후보**: `cron_tool.py` 의 `CronManager` 는 인스턴스화되지 않는 dead code 다. 본 작업과 직접 관련 없지만 분석 도중 발견. PR #2 의 07-issues.md 처럼 별도 이슈로 기록.
