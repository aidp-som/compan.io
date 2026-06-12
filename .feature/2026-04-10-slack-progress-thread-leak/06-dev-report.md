# 구현 결과 보고서: Slack progress 메시지 thread 누락 수정

> 작성일: 2026-04-10
> 복잡도: medium
> 브랜치: holmes (aidp-som fork)
> 기반: 01-analysis.md → 02-design.md → 03-review.md → 04-plan.md → 05-test-strategy.md

## 1. 구현 요약

Slack 채널에서 `@SOM Agent` 를 멘션할 때 "생각 중..." ACK 와 `/stop`·`/help`·`/new`·에러 응답·에이전트의 `message` 툴 중간 메시지가 스레드 바깥 채널 본문에 뜨던 버그를 수정했다. 원인은 코어 루프의 OutboundMessage 생성 지점 7곳 중 1곳(최종 답변)만 inbound metadata 를 상속하고 나머지가 thread 정보를 증발시킨 것. `bus.py` 에 화이트리스트 기반 팩토리 `OutboundMessage.reply_to_inbound()` 를 도입해 계약을 타입 레벨에서 강제하고, 7곳 전부 교체했다. 부가로 발견한 `role_prompt` 누수 위험, `MessageSender._sent_in_turn` 의 thread 무관 판정, `SlackChannel._progress_messages` 캐시 race 도 같은 PR 에 묶어 해결했다.

## 2. 변경 파일 목록

| 파일 | 변경 유형 | 설명 |
|---|---|---|
| `companio/bus.py` | 수정 | `_FORWARDED_METADATA_KEYS` 화이트리스트 + `OutboundMessage.reply_to_inbound()` 팩토리 추가 (+42줄) |
| `companio/core/loop.py` | 수정 | 7개 OutboundMessage 생성 지점을 `reply_to_inbound` 로 교체. `loop.py:194-197` 의 `msg.metadata` mutation 제거, 로컬 `context_metadata` dict 로 대체. `set_context(msg)` 로 간소화 (-24/+30 줄) |
| `companio/tools/message.py` | 수정 (재작성) | `MessageSender` 가 `set_context(inbound: InboundMessage)` 로 inbound 자체를 수납. `send()` 내부가 `reply_to_inbound` 경유. `_sent_in_turn` 판정에 thread_ts/message_thread_id 비교 추가. LLM 노출 `send()` 시그니처는 호환 유지 (-13/+54 줄) |
| `companio/channels/slack.py` | 수정 | `_progress_locks: defaultdict[asyncio.Lock]` 추가. progress 캐시 check/post/update 를 per-thread-key lock 으로 atomic 처리. `thread_ts` falsy 면 캐시 bypass 해 DM 안전성 확보 (+34/-12 줄) |
| `tests/test_thread_metadata_propagation.py` | 신규 | 17개 테스트. 5개 영역(reply_to_inbound 화이트리스트, MessageSender thread 컨텍스트, SlackChannel progress cache, AgentLoop `_handle_stop`, 소스 레벨 regression guard, Telegram metadata 회귀 가드) |
| `.feature/2026-04-10-slack-progress-thread-leak/` | 신규 | 기획·리뷰·플랜·테스트 전략·결과 보고서 산출물 7개 |

## 3. 검증 결과

### 빌드 / 린트

- `ruff check companio/bus.py companio/core/loop.py companio/tools/message.py companio/channels/slack.py tests/test_thread_metadata_propagation.py`: ✅ 통과 (import 정렬 1건 자동 수정 후 재통과)

### 단위 테스트

`tests/test_thread_metadata_propagation.py` 17개 모두 통과:

| 영역 | 개수 | 상태 |
|---|---|---|
| A. `TestReplyToInbound` (화이트리스트 계약) | 6 | ✅ |
| B. `TestMessageSenderThreadContext` | 4 | ✅ |
| C. `TestSlackProgressCache` (posting/update/isolation/cleanup) | 4 | ✅ |
| D. `TestAgentLoopOutboundMetadata::test_handle_stop_inherits_thread_ts` | 1 | ✅ |
| E. `TestNoBareOutboundInLoop` (소스 regression guard) | 1 | ✅ |
| F. `TestTelegramInboundMetadata` (회귀 가드) | 1 | ✅ |

전체 테스트 스위트: **104 passed, 2 failed (pre-existing)**. 실패 2개는 `tests/test_claude_cli.py::TestBuildCmd::{test_basic, test_add_dir_home}` 로 Windows 에서 `claude` 명령이 `claude.CMD` 래퍼로 해석되는 pre-existing 이슈 — 이 PR 과 무관하며 main 에서도 동일하게 실패함을 `git stash` 후 재실행으로 확인.

### E2E 테스트

해당 없음 — 이 레포는 full E2E 스위트가 없다. AsyncMock 기반 단위 테스트로 channel adapter 까지 커버.

### UI E2E 테스트

해당 없음 (UI 없음, Slack 봇).

### 수동 확인 (봇 재기동 필요 — 대기 중)

04-plan.md 의 AC1~AC8 체크리스트 + 수동 테스트 시나리오 (a)~(g) 는 **봇 재기동 후 SOM Slack 에서 사용자가 직접 실행**. 현재 봇은 재기동 전 상태이며, 수동 확인 개시 시점은 사용자 판단.

## 4. 사용자 확인 가이드

### 사전 조건

- 관리자 권한 터미널 (UAC elevated) 에서 Claude Code 세션 필요 — `taskkill` 권한
- SOM Slack 워크스페이스 접근, 테스트 채널 최소 1개
- 봇이 tmux 세션 `som-agent` 에서 돌고 있어야 함
- 이 PR 의 변경 사항은 editable install 덕분에 봇 재기동 한 번으로 반영됨

### 재기동 절차 (CLAUDE.md 참조)

```bash
# 1) graceful shutdown 시도
tmux send-keys -t som-agent C-c
sleep 3

# 2) 안 죽으면 강제 종료 (Git Bash 에서는 //F //PID 이스케이프 주의)
tasklist | grep -iE "companio|python"
taskkill //F //PID <pid>

# 3) 재기동
tmux send-keys -t som-agent 'companio gateway --config C:/Users/som_server_2/.companio/config.json' Enter
sleep 8
tmux capture-pane -t som-agent -p -S -80
```

정상 기동 시그널:
```
Using config: C:\Users\som_server_2\.companio\config.json
✓ Channels enabled: slack
✓ Cron: 12 scheduled jobs
Slack bot connected (user_id=U0AR4QX7TD2)
```

### 확인 절차 (AC 체크리스트)

1. **AC1 채널 루트 멘션**: 테스트 채널의 루트(스레드 아님)에서 `@SOM Agent 간단히 자기소개해줘` → 새 스레드 하나가 생성되고 그 안에 "생각 중..." → 최종 답변이 순서대로 쌓이는지. 채널 본문에는 내 멘션 외에 봇 메시지가 일절 안 뜨는지.
2. **AC2 스레드 내부 멘션**: 기존 스레드 안에서 `@SOM Agent 뭐 하고 있어?` → 같은 스레드 내부에 응답이 이어 붙는지.
3. **AC3 DM**: 봇과의 DM 에서 `안녕` → 평문 응답, 스레드 UI 가 생기지 않는지.
4. **AC4 슬래시 명령**: 멘션으로 시작한 스레드 안에서 `@SOM Agent /stop` (진행 중일 때) → "Stopped N task(s)." 가 스레드 안에 뜨는지. `/help`, `/new` 도 동일 원리로 스레드 안에서.
5. **AC5 에러 경로**: 봇이 에러를 내도록 유도 (예: 비정상적으로 긴 입력 등) → "Sorry, I encountered an error." 가 스레드 안에 뜨는지.
6. **AC6 `message` 툴 중간 메시지**: 에이전트에게 "중간 경과를 스레드에 보고하면서 작업해" 같이 지시 → 중간 메시지들이 전부 스레드 안에 들어오는지.
7. **AC7 동시 멘션**: 같은 채널에서 두 번 연달아 멘션 → 각자 독립 스레드 생성, 서로 말풍선이 섞이지 않는지.

### 예상 결과

모든 봇 응답(ACK, 진행 중 메시지, 슬래시 명령, 에러, 최종 답변)이 **트리거가 된 멘션이 속한 스레드 안**에 갇혀 있어야 한다. 채널 본문에는 원본 멘션 메시지만 남고 봇은 스레드 내부에서만 말한다. DM 에서는 기존과 동일하게 평문.

## 5. 후속 과업

이번 PR 에서 의도적으로 제외한 항목들 (03-review.md 크로스 커팅 분석 + 04-plan.md 섹션 2 참조):

- [ ] **UX 개선 별도 티켓**: (i) 👀 리액션 ACK + 3초 폴백 텍스트, (ii) 에러 메시지 재시도 안내 풍부화, (iii) `/new`·`/help` ephemeral 처리, (iv) 다수 사용자 중복 질문 가시화 — UX 디자이너 페르소나가 강하게 요청한 항목. 이번 PR 은 thread 격리 자체에만 집중.
- [ ] **`split_message` chunk 고아 버그**: `channels/slack.py:220-230` 의 for 루프에서 `result` 가 마지막 chunk 로 덮여 progress 캐시에 **마지막 chunk 의 ts 만** 저장되는 별도 버그. 이후 `chat_update` 시 앞 chunk 들이 고아가 됨. 같은 파일이지만 다른 계약이라 별도 PR 로 분리.
- [ ] **Telegram `_message_threads` fallback 제거**: 이번 PR 이후 Telegram inbound 은 이미 `message_thread_id` 를 metadata 에 싣고 화이트리스트로 상속되므로 `telegram.py:363-365` 의 `_message_threads.get((chat_id, reply_to_message_id))` fallback 은 dead code 에 가까움. 제거는 Telegram 실기기 재확인 필요.
- [ ] **pre-existing Windows 테스트 깨짐 수정**: `tests/test_claude_cli.py::TestBuildCmd::{test_basic, test_add_dir_home}` 가 `cmd[0] == "claude"` 를 단언하지만 Windows 에선 `claude.CMD` 로 해석됨. `Path(cmd[0]).stem == "claude"` 같은 방식으로 느슨하게. 이 PR 과 무관하지만 PR 본문에 기록.
- [ ] **`channels/base.py:104` ACL 거부 응답 metadata 누락**: 접근 거부 메시지("접근 권한이 없습니다...") 가 스레드 밖으로 나가는 동일 계열 버그. 이 PR 스코프 밖 (drift 방지) 이지만 향후 수정 필요.
- [ ] **CHANGELOG / 머지 공지 정책**: 이 레포에 CHANGELOG 파일이 없고 upstream 이슈가 비활성임. PR 본문이 유일한 기록이므로 본문에 AC 체크리스트와 후속 과제를 모두 포함하기.
