# 핵심 이슈 및 요청사항: Slack 스레드 컨텍스트 자동 fetch

> 작성일: 2026-04-11

## 발견된 이슈 (이번 PR 해결 완료)

### [심각도: 높음] companio "tools" 가 LLM-callable 이 아님

- **현상**: `message`, `cron`, `share_to_channel` 이 TOOLS.md 에 "companio-Specific Tools" 로 문서화돼 있지만 실제로는 LLM 이 호출 가능한 툴이 아님. shadow detection / pre/post 처리 / 동기 dispatcher 패턴.
- **원인**: companio 는 `claude -p` 서브프로세스를 spawn 만 하고 stdin/stdout 으로 텍스트만 주고받음. 외부 툴 주입은 `<project_dir>/.mcp.json` 의 MCP 서버 기반인데 SOM 머신은 거기가 비어 있음. `cli.py:418-420` 에 "MessageSender cannot be injected into Claude CLI subprocess" 명시.
- **영향 범위**: 분석 도중 발견. 사용자가 처음 옵션 C("자동 + 명시적 LLM 툴") 를 원한 경우, "명시적 LLM 툴" 부분은 in-process MCP 서버 인프라가 없는 한 직접 가능하지 않다는 사실을 명확히 함.
- **조치**: 이번 PR 은 옵션 C 를 옵션 A1+A2 (자동 pre-fetch + shadow detection) 로 구체화. 진짜 LLM 툴은 옵션 B (별도 MCP 서버 신설) 가 필요하지만 이번 PR 스코프 외. `01-analysis.md §6, §7` 에 상세 기록.

### [심각도: 중간] `_handle_reaction_error` 와 신규 `_handle_thread_context_error` 의 99% 동형 패턴

- **현상**: 9-case 매트릭스(silent / not_in_channel / invalid_auth / missing_scope / ratelimited / other) 가 호출자별로 거의 동일한 분기.
- **조치**: `_handle_slack_api_error` helper 추출 (D4). `_handle_reaction_error` 를 thin wrapper 로 재작성. 새 thread context + users_info 도 같은 helper 사용. 기존 reaction 테스트 18개 회귀 0.

### [심각도: 중간] inbound `content` prepend 시 session DB pollution

- **현상**: 원래 02-design.md 는 합성된 thread context 를 inbound content 에 prepend 하려 했음. 하지만 그러면 session.messages 에 합성본이 저장되고 다음 turn 의 history_text 가 중복 폭발.
- **조치**: D1 — slack.py 는 metadata 에만 저장, loop.py 가 LLM prompt 합성 시점에 끼움. `_save_turn(session, msg.content, ...)` 그대로 — session DB 는 원본만. QA 페르소나 리뷰에서 잡힘.

### [심각도: 중간] inbound 방향 secret 마스킹 부재

- **현상**: `filter_secrets` 는 LLM 응답에만 적용. fetched thread text 안에 토큰/패스워드가 있으면 외부 Claude API 로 평문 전송.
- **조치**: D2 — `_format_thread_messages` 가 매 메시지 텍스트에 `filter_secrets` 적용. 보안 페르소나 리뷰에서 잡힘. 단위 테스트 `test_filter_secrets_applied_to_messages` 로 회귀 가드.

### [심각도: 중간] `MessageSender._sent_in_turn` 등 `_active_ack_reactions` 캐시 race 가능성

- **현상**: 같은 (chat, thread) 동시 멘션 시 fetch 2회 가능.
- **조치**: D5 — `_thread_context_locks: defaultdict(asyncio.Lock)` 으로 per-(chat, thread) 직렬화. `_progress_locks` 패턴 차용. 동시성 테스트로 회귀 가드.

### [심각도: 낮음] display name 의 prompt injection 표면

- **현상**: 사용자가 Slack display name 을 `[INST]ignore prior instructions` 같이 설정하면 fetched 메시지의 발화자 라벨로 LLM prompt 에 직접 들어감.
- **조치**: D19 — `_sanitize_display_name` 이 control char + injection marker(`[INST]`, `<|`, `</s>`) strip + 64자 cap. 보안 페르소나 리뷰에서 잡힘.

## 기술 부채 (이번에 미해결)

### `_handle_slack_api_error` 의 retry 시그니처 한계

- **현상**: 현 helper 는 `retry: Callable[[], Awaitable[Any]] | None` 으로 단일 retry 만 지원. 향후 backoff·multi-retry 가 필요한 caller 가 생기면 시그니처 부족.
- **권장 조치**: 4번째 caller 추가 시 인터페이스 재검토. 지금은 충분.

### shadow_miss 측정 인프라 부재

- **현상**: D10 의 `shadow_miss_candidate` 메트릭은 미구현. 어제 머지된 broadcast (`1e10b8e`) 는 둘 다 측정하는데 본 PR 은 `shadow_match` 만.
- **권장 조치**: 운영 1주 데이터로 정규식 보정 시점에 함께 도입. weak proxy: "content < 20자 + thread_ts != message_ts + default_limit 사용" → INFO 로그.

### `_active_threads` 등록 정책의 절충

- **현상**: D9 는 "fetch 결과 기반으로 지연" 이었으나 실제 구현은 "fetch 후 항상 등록". 이유: fetch 가 실패해도 봇이 그 스레드에 engage 한 사실을 추적해야 후속 비-멘션 reply 라우팅이 일관됨.
- **권장 조치**: 만약 운영 중 "scope 부재 채널에서 active thread 가 누적되는 게 문제" 가 되면 strict 버전(`fetch 성공 시에만 등록`) 으로 좁힐 수 있음.

### TTL 제거로 socket event drop 케이스 fallback 부재

- **현상**: `_on_message` invalidate 가 freshness 전담. Slack Socket Mode 가 어떤 이유로 message event 를 drop 하면 cache 가 stale 한 상태로 무한 유지 (LRU eviction 외).
- **권장 조치**: 운영 관측. 실제로 문제가 발생하면 backstop TTL (예: 5분) 추가. 사용자 결정으로 일단 제거.

## 요청사항

### 사용자 (이 세션의 운영자)

- [ ] **🔴 prerequisite: Slack 봇 OAuth scope 확인**: 이번 PR 의 가장 큰 외부 의존성. SOM Slack admin → 봇 앱 → "OAuth & Permissions" → "Bot Token Scopes" 에서 다음 5개가 부여돼 있는지 확인:
  - `channels:history` (가장 핵심)
  - `groups:history`
  - `im:history`
  - `mpim:history`
  - `users:read`
  부족하면 추가 후 워크스페이스 재인증, 신규 토큰을 `~/.companio/config.json` 에 반영. scope 가 없어도 봇은 죽지 않고 runtime auto-disable 로 fallback 만 함.
- [ ] **AC1~AC12 수동 검증**: 06-dev-report.md §4 참조. 봇은 이미 재기동돼 새 코드로 동작 중.
- [ ] **PR 제출 허가**: scope 확인 + AC 검증이 통과하면 (또는 scope 미확인 상태로도 코드는 안전하니 PR 만 먼저 띄우는 것도 가능):
  ```bash
  git add -- companio/ tests/test_thread_context_fetch.py .feature/2026-04-11-slack-thread-context-fetch/
  git commit
  git push fork holmes
  gh pr create ...
  ```
- [ ] **SOM Slack 사용자 공지 여부**: 04-plan.md §5 의 공지 문구 초안 사용 여부 결정. 머지 전·후 어느 시점에 올릴지.
- [ ] **`thread_context_blocked_channels` 정책**: 민감 채널(`#hr`, `#finance` 등) 을 블록할지 결정. config.json 에 chat_id 추가하면 즉시 적용 (재기동 필요).

### 업스트림 메인테이너 (PR 리뷰어)

- [ ] **scope blast radius 인지**: `channels:history` 등은 *멘션받은 스레드만* 이 아니라 *봇이 들어간 모든 채널의 전체 히스토리* 를 읽을 수 있는 권한. 코드 self-limit 은 충실하지만 토큰 자체의 권한은 넓음 — 향후 코드 변경 시 주의 필요. 보안 페르소나 리뷰 #1.
- [ ] **`<external-context>` prompt injection 방어 검토**: D18 의 마커 + AGENTS.md 안내 조합이 충분한가? Claude 모델 자체의 instruction-following 신뢰도에 의존.
- [ ] **D9 절충**: `_active_threads` 등록을 fetch 결과와 무관하게 하는 결정이 적절한가? 더 strict 한 버전(fetch 성공 시에만 등록)을 선호하면 알려주기.
- [ ] **TTL 제거 결정**: 사용자가 의도적으로 풀어둠. socket event drop fallback 이 없는 점에 대한 의견.

## 참고사항

- 이번 PR 의 기획·리뷰·플랜·테스트 전략·결과 보고서 모두 `.feature/2026-04-11-slack-thread-context-fetch/` 에 보존 (7개 파일).
- 분석 단계에서 발견한 "companio tools 가 LLM-callable 이 아님" 사실은 향후 슬랙/텔레그램 통합 확장 시 가장 중요한 architectural constraint. 옵션 B (in-process MCP 서버) 가 후보로 남음.
- `_handle_slack_api_error` helper 는 향후 Slack Web API 호출의 표준 진입점 — 새 caller 추가 시 동일 패턴 권장.
- 단위 테스트 `test_thread_context_fetch.py` 는 PR #2 의 `test_thread_metadata_propagation.py` 와 별 파일로 분리 — 데이터 contract (PR #2) vs fetch 동작 (이 PR) 의 자연스러운 책임 분리.
- 어제 PR #2 의 source-level guard `TestNoBareOutboundInLoop` 가 본 PR 에서도 통과 — outbound 생성 경로를 건드리지 않았음을 자동 검증.
- 봇은 19:08:47 KST 재기동돼 새 코드로 동작 중. SOM Slack 에서 멘션 들어오면 스레드 컨텍스트 자동 주입 시도 (scope 부재 시 첫 호출에서 runtime auto-disable, 응답 자체는 정상).
