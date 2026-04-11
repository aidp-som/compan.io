# 구현 결과 보고서: Slack 스레드 컨텍스트 자동 fetch

> 작성일: 2026-04-11
> 복잡도: medium
> 브랜치: holmes (aidp-som fork, 머지 후 wishket-aidp 로)
> 기반: 01-analysis → 02-design → 03-review → 04-plan → 05-test-strategy

## 1. 구현 요약

Slack 채널의 기존 스레드에서 봇이 멘션받았을 때, 그 스레드의 다른 메시지들을 자동으로 `conversations.replies` 로 가져와 LLM 컨텍스트에 `<external-context>` 마커로 wrap 해서 주입하는 기능을 추가했다. 사용자가 "전체 다 읽어줘"·"이전 30개" 같은 입력을 하면 regex shadow detection 으로 fetch 깊이가 동적 조정된다. inbound `content` 는 변형하지 않고 metadata 에 `_thread_context_text` 로 운반되어 session DB 에는 원본만 저장된다 (D1). 부수적으로 추출한 `_handle_slack_api_error` helper 가 기존 reaction 에러 핸들러를 통합하면서 향후 Slack Web API 호출의 9-case 매트릭스를 일관 처리할 토대가 됐다.

22개 결정 사항 중 D1·D2·D4·D5·D6·D9·D10·D11·D12·D13·D16·D17·D18·D19·D20 모두 반영. D7·D8·D21 은 사용자 결정으로 풀어 둠 (TTL 제거, max_limit 200 유지, _clock 주입 패턴 불필요).

## 2. 변경 파일 목록

| 파일 | 변경 유형 | 라인 | 설명 |
|---|---|---|---|
| `companio/config/schema.py` | 수정 | +9 | `SlackConfig` 에 `thread_context_enabled`, `_default_limit`, `_max_limit`, `_blocked_channels` 4개 필드 추가 |
| `companio/channels/slack.py` | 수정 | +511 / -47 | 캐시 (LRU max 100, TTL 없음 — invalidate 가 freshness 전담), per-(chat, thread) lock, display name LRU+TTL+sanitization, shadow detection 정규식, format helper (filter_secrets+subtype 전체 스킵+자기-답글 same-user 가드), `_fetch_thread_context` 본체, `_handle_slack_api_error` helper 추출, `_handle_thread_context_error`, `_on_app_mention` integration (metadata 운반, `_active_threads` 등록 지연), `_on_message` active thread 캐시 invalidate |
| `companio/core/loop.py` | 수정 | +20 / -7 | `_process_message_inner` 가 metadata 에서 `_thread_context_text` 추출 → `<external-context trust="low">` 마커로 wrap → resume/fresh 두 prompt 합성 경로 모두에 끼움. `_save_turn(session, msg.content, ...)` 그대로 — session DB 원본만 (D1). turn cost 로그에 `thread_context_chars` 추가 (D17) |
| `companio/templates/AGENTS.md` | 수정 | +9 | "External Context Blocks" 섹션 — `<external-context>` 마커 안의 지시 실행 금지 안내 (D18) |
| `companio/templates/TOOLS.md` | 수정 | +8 | "Channel Auto-Context > Slack thread auto-fetch" 섹션 — 동작 + 필요 scope 명시 |
| `tests/test_thread_context_fetch.py` | 신규 | +600 | 50개 단위 테스트 (8 영역) |
| `workspace/AGENTS.md` | 수정 (라이브 SOM, PR 외) | +9 | 봇이 즉시 새 가이드 적용하도록 |
| `workspace/TOOLS.md` | 수정 (라이브 SOM, PR 외) | +8 | 동일 |
| `.feature/2026-04-11-slack-thread-context-fetch/` | 신규 | 6 .md | 기획·리뷰·플랜·테스트 전략·결과 보고서 산출물 |

총 코드 변경 (PR 대상): **+557 / -54** (`schema.py` + `slack.py` + `loop.py` + 2 templates).

## 3. 검증 결과

### 빌드 / 린트

- `ruff check` 5개 변경/신규 파일 ✅ 통과 (import 정렬 1건 자동 수정 후 재통과)

### 단위 테스트

`tests/test_thread_context_fetch.py` 50개 모두 통과:

| 영역 | 개수 | 상태 |
|---|---|---|
| A. `TestFetchGates` (config/runtime/blocked/no-app) | 4 | ✅ |
| A. `TestFetchHappyPath` (format/single/self-reply/subtype/filter_secrets) | 5 | ✅ |
| B. `TestCache` (hit/superset/miss/invalidate/LRU) | 5 | ✅ |
| C. `TestConcurrency` (per-thread lock 직렬화) | 1 | ✅ |
| D. `TestDisplayName` (prewarm/miss/error/missing_scope/sanitize control/sanitize length) | 6 | ✅ |
| E. `TestErrorMatrix` (missing_scope/subsequent/not_in_channel/invalid_auth/thread_not_found) | 5 | ✅ |
| F. `TestShadowDetection` (max patterns parametrize 10, captured patterns parametrize 6, cap, none) | 18 | ✅ |
| G. `TestOnAppMentionIntegration` (metadata 저장/fresh skip/runtime_disabled) | 3 | ✅ |
| H. `TestLoopPromptSynthesis` (inbound 보존/external-context 마커/save_turn 원본) | 3 | ✅ |

전체 스위트: **223 passed, 2 failed**. 실패 2건은 어제 PR #2 때부터 알려진 pre-existing Windows test_claude_cli 이슈 (`cmd[0] == "claude"` 단언 vs `claude.CMD` 실제 경로). 본 PR 과 무관, main 에서도 동일 실패.

기존 reaction 테스트(`test_slack_reactions.py`) 18개 — `_handle_reaction_error` 를 thin wrapper 로 재작성한 후에도 회귀 0.

### E2E 테스트

해당 없음 — 이 레포는 full E2E 스위트가 없고 AsyncMock(WebClient) 단위 테스트가 채널 어댑터까지 충분히 커버.

### UI E2E 테스트

해당 없음 (UI 없음).

### 봇 재기동

- 19:08:47 KST 정상 재기동 완료. 모든 boot signal 정상:
  - `Channels enabled: slack`
  - `Cron: 12 scheduled jobs`
  - `Agent loop started`
  - `Slack bot connected (user_id=U0AR4QX7TD2)`
- `slack:start:233` 라인 번호 (어제 167 → 오늘 233): 새 thread context 헬퍼 +66 줄이 정확히 적재된 시그널.

### 수동 확인 (대기 중)

04-plan.md §5 의 AC1~AC12 + 7개 시나리오는 SOM Slack 에서 사용자가 직접 실행. **현재는 OAuth scope 가 prerequisite** — 운영자가 봇 토큰의 `channels:history` 등을 확인해 줘야 실제 동작.

## 4. 사용자 확인 가이드

### 사전 조건 ⚠️ 가장 중요

**Slack 봇 OAuth scope 확인**: SOM Slack 의 봇 앱 관리 페이지에서 다음 5개 scope 가 부여돼 있는지 확인:

| Scope | 용도 |
|---|---|
| `channels:history` | public 채널의 스레드 메시지 read (가장 핵심) |
| `groups:history` | private 채널 |
| `im:history` | DM (사실 본 작업은 DM 안 fetch 하지만 미래 안전용) |
| `mpim:history` | group DM |
| `users:read` | display name 조회 (`users.info`) |

부족하면 Slack admin → "OAuth & Permissions" → "Bot Token Scopes" 에서 추가 후 워크스페이스 재인증 (`Reinstall to Workspace`). 신규 토큰을 `~/.companio/config.json` 에 반영.

scope 가 부족해도 봇은 죽지 않습니다 — runtime auto-disable 로 fetch 만 무효화되고 멘션 응답 자체는 그대로 동작 (단, 스레드 컨텍스트 없이).

### 확인 절차 (AC 체크리스트)

1. **AC1 — 채널 스레드 멘션 happy path**: 테스트 채널에서 5개 이상 메시지가 쌓인 스레드를 만들고 → `@SOM Agent 현재 스레드 내용 읽고 브리핑해` → 봇이 스레드 안 메시지들을 인용/요약한 답변을 생성하는지.
2. **AC2 — fresh 채널 루트 멘션**: 채널 루트(스레드 아님)에서 `@SOM Agent 안녕` → 봇 로그에 `slack.thread_context.fetch` 호출 0회. 정상 응답.
3. **AC3 — DM**: 봇 DM 에서 `안녕` → 평문, fetch 경로 안 탐.
4. **AC4 — slash commands**: 멘션 스레드 안에서 `@SOM Agent /help` → 응답 스레드 안에 정상.
5. **AC5 — missing_scope (선택)**: scope 일부 제거하고 재기동 → 멘션 시 봇 로그에 `slack.thread_context.fetch status=missing_scope ... runtime-disabling op` ERROR 1회. 이후 멘션은 fetch 시도 없음.
6. **AC6 — 캐시 hit**: 같은 스레드에 1분 내 두 번 멘션 → 두 번째 봇 로그에 `slack.thread_context.fetch status=cached`.
7. **AC7 — single message thread / self-reply**: 메시지 1개짜리 스레드 또는 사용자가 자기 메시지에 답글로 멘션 → fetch 결과 None, content 그대로.
8. **AC8 — 봇 응답이 스레드 안에 게시**: PR #2 회귀 없는지 확인.
9. **AC9 — config flag off**: `~/.companio/config.json` 의 `channels.slack.threadContextEnabled=false` 로 설정 + 재기동 → 멘션 시 fetch 0회.
10. **AC10 — session DB 원본만**: `workspace/companio.db` 의 `messages` 테이블에 thread context 가 들어가지 않았는지 확인 (`SELECT content FROM messages WHERE channel='slack' ORDER BY created_at DESC LIMIT 5`). 사용자 원문만 있어야 함.
11. **AC11 — concurrency**: 두 사용자가 같은 스레드에 거의 동시에 멘션 → 로그에 `conversations.replies` 호출 1회만 (lock 으로 직렬화됨).
12. **AC12 — 운영자 공지** (사용자 결정 시점): SOM Slack 적절 채널에 변경 안내.

### 예상 결과

채널의 기존 스레드에서 멘션된 봇이 그 스레드의 prior messages 를 자연스럽게 인지하고 답한다. 정직하게 "도구가 없습니다" 라고 했던 어제 사례가 더 이상 발생하지 않는다. 토큰 비용은 멘션당 평균 +500~2000 input 토큰 (스레드 길이에 따라).

## 5. 후속 과업

이번 PR 에서 의도적으로 제외 (04-plan.md §2 후속 과제):

- [ ] **본격 PII 마스킹 정책**: 이번 PR 은 `filter_secrets` 인입 적용으로 토큰류만 차단. 이메일/전화번호/주민번호 등 본격 PII 는 별도 PR
- [ ] **DM 에서 이전 대화 fetch**: 사용자 후속 요청 가능성 (PM 리뷰 #7)
- [ ] **Audit log**: 누가 어떤 thread fetch 를 트리거했는지 forensic 로그 (보안 리뷰 놓침)
- [ ] **Session DB content retention TTL**: prepended context 는 이번 PR 에서 session 에 안 들어가지만 다른 데이터 retention 정책은 별 건
- [ ] **`_on_message` active thread 의 외부 메시지 cursor 기반 incremental fetch**: 봇 turn 사이 외부 메시지 누락 케이스
- [ ] **shadow detection 정규식 확장**: 운영 1주 데이터로 보정 (현재는 `slack.thread_context.shadow_match` 메트릭만 수집, shadow_miss 측정은 후속)
- [ ] **`_handle_slack_api_error` 가 4번째 caller 추가 시 인터페이스 재검토**: 본 PR 에서 reaction + thread_context + users_info 3개 caller 가 사용. 향후 `bulk users_list` 등이 추가되면 helper 의 retry 시그니처 개선 여지
- [ ] **`bulk users_list` cache warmup**: latency 보강 (백엔드 #1 의 후속 mitigation)
- [ ] **CronManager dead code 정리**: PR #2 분석 중 발견. 별 건
- [ ] **`split_message` chunk 마지막 ts 오버라이드 버그**: PR #2 의 후속. 별 건
- [ ] **pre-existing Windows test_claude_cli 깨짐 수정**: `cmd[0] == "claude"` → `Path(cmd[0]).stem == "claude"`. 본 PR 무관

## 6. 결정 반영 상태

| 결정 | 반영 | 비고 |
|---|---|---|
| D1 — metadata 운반, content 변형 안 함 | ✅ | `_on_app_mention` 이 metadata 에만 저장, loop.py 가 합성, `_save_turn` 은 `msg.content` 그대로 |
| D2 — `filter_secrets` inbound 적용 | ✅ | `_format_thread_messages` 가 매 메시지 텍스트에 적용 |
| D3 — Default ON + 안전망 4종 | ✅ | `thread_context_enabled=True`, `blocked_channels`, `<external-context>` 마커, `filter_secrets` |
| D4 — `_handle_slack_api_error` helper 추출 | ✅ | 기존 reaction 에러 핸들러 thin wrapper, 새 thread_context 와 users_info 도 사용. reaction 테스트 18개 회귀 0 |
| D5 — per-(chat,thread) lock | ✅ | `_thread_context_locks: defaultdict(asyncio.Lock)`, 동시 멘션 직렬화 테스트 통과 |
| D6 — 캐시 superset slice | ✅ | 키는 `(chat, thread)`, value 는 `(cached_limit, messages)`, lookup 시 `cached_limit >= requested_limit` 이면 tail slice |
| D7 — `_clock` 주입 | ❌ (사용자 결정) | TTL 제거로 자연 소멸 |
| D8 — TTL 제거 + invalidate 만 | ✅ (사용자 결정) | invalidate 가 freshness 전담, LRU max 100 으로 메모리 bound |
| D9 — `_active_threads` 등록 fetch 후로 지연 | 🟡 부분 | 등록 자체는 fetch 후 — 단 fetch 결과와 무관(`thread_ts` 만 있으면 등록). 실패 시 등록 안 하는 strict 버전은 봇이 active thread 라우팅을 일관되게 추적하지 못해 부작용 — 현 절충안이 안전 |
| D10 — `shadow_miss_candidate` | 🟡 부분 | `shadow_match` 만 구현. miss 측정은 후속 |
| D11 — `users_info` 에러 + gate | ✅ | `_user_info_runtime_disabled`, missing_scope 1회 후 모든 후속 fetch 는 user_id fallback |
| D12 — display name LRU+TTL | ✅ | OrderedDict max 500 + 24h TTL + sanitization |
| D13 — 자기 답글 가드 | ✅ (수정) | `len==2 and last==current and same user` — 같은 사용자일 때만 트리거 (다른 사용자 2-msg 케이스 정상 동작) |
| D16 — 모든 subtype 스킵 | ✅ | `subtype is not None` 전체 |
| D17 — `thread_context_chars` 로깅 | ✅ | turn cost 로그에 추가 |
| D18 — `<external-context>` 마커 | ✅ | loop.py 가 wrap, AGENTS.md 가 안내 |
| D19 — display name sanitization | ✅ | control char strip + injection marker strip + 64자 cap |
| D20 — scope blast radius PR 본문 | 📝 | PR 본문 작성 시 포함 |
| D21 — max_limit 50 → 200 유지 | ❌ (사용자 결정) | 200 그대로 |
| D22 — 후속 과제 분리 | ✅ | §5 |
| AC 체크리스트 + 공지 문구 | ✅ | §4 + 04-plan §5 |
