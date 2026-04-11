# 피처 플랜: Slack progress 메시지 thread 누락 수정

> 작성일: 2026-04-10
> 기반: 01-analysis.md, 02-design.md, 03-review.md
> 사전 검증: Telegram inbound 이 `telegram.py:536` 에서 `message_thread_id` 를 metadata 에 정상 삽입함을 확인. 화이트리스트 기반 상속이 양 채널 모두에 symmetric 하게 동작 가능. 기존 테스트 인프라(`tests/test_agent_core.py`, `tests/test_channels.py`, `pytest-asyncio asyncio_mode=auto`) 는 접붙이기 적합.

## 1. 최종 결정 사항

| # | 결정 | 근거 |
|---|---|---|
| D1 | **Option C 채택** — `OutboundMessage.reply_to()` 팩토리를 `bus.py` 에 도입하고, metadata 상속은 **화이트리스트** 방식 | 백엔드 리뷰어가 지적한 `role_prompt` 오염 위험이 블랙리스트 상속의 실질적 리스크임. QA 리뷰어도 "헬퍼로 리그레션 가드" 를 독립적으로 요구. 두 방향 컨센서스. 변경 범위는 몇십 줄 증가에 그침 |
| D2 | **Inbound mutation 제거** — `loop.py:194-197` 의 `msg.metadata["role_name"] = ...` 을 로컬 `runtime_metadata` dict 로 옮김 | D1 의 화이트리스트만으로는 로깅/디버깅 경로의 role_prompt 누수가 남음. 근본적으로 inbound 를 mutate 하지 않는 게 맞음 |
| D3 | **`MessageSender` 내부 API 를 `inbound: InboundMessage` 통째 수납으로 변경** | LLM 노출 `send()` 시그니처는 호환 유지, 내부 `set_context` 만 바꿔 `message_id`/`message_ts` 키 mismatch 버그까지 동시 소멸 |
| D4 | **`_sent_in_turn` 판정에 `thread_ts` 비교 추가** | 백엔드 지적 대로 현재 chat_id 만 비교해 다른 스레드로 send 한 경우 최종 답변 유실 가능. 같은 파일 건드리는 김에 수정 |
| D5 | **`_progress_messages` 캐시에 per-key asyncio.Lock + `thread_ts is None` 시 캐시 비활성** | 백엔드 지적대로 dispatcher 이후 async race 가능. DM 에서 progress 편집 비활성화는 캐시 충돌 원천 차단 |
| D6 | **UX 개선(리액션 ACK, 에러 문구 풍부화, `/new` ephemeral) 은 이번 PR 제외** | 크로스 커팅 분석의 컨센서스. 버그 수정 스코프를 침범하지 않기 위해 별도 UX 개선 티켓으로 분리. PR 본문에 "후속" 으로 명시 |
| D7 | **`split_message` 마지막 chunk 오버라이드 버그는 별도 PR** | 같은 파일이지만 다른 계약. 스코프 드리프트 방지 |
| D8 | **수용 기준(AC) 을 PR 본문에 명시적 체크리스트로 포함** | PM·QA 공통 요구 |
| D9 | **Telegram 단위 테스트는 AsyncMock bot 으로 필수 커버, 실기기 수동 테스트는 생략 명시** | 실기기 없음. 리스크 수용을 PR 본문에 기록 |

## 2. 최종 스코프

### 포함

- `bus.py`: `OutboundMessage.reply_to()` 클래스메서드 + `_FORWARDED_METADATA_KEYS` 화이트리스트
- `core/loop.py`:
  - `_handle_stop` (128), 에러 핸들러 (144), `/help` (164), ACK (178), `/new` 에러 (321), `/new` 성공 (332), 최종 답변 (307) — 총 **7 지점** `OutboundMessage.reply_to()` 로 교체
  - `loop.py:194-197` role injection 을 `runtime_metadata` 로컬 변수로 이동, inbound mutation 중단
  - `loop.py:173` `message_sender.set_context(msg)` 로 간소화
- `tools/message.py`:
  - `MessageSender.__init__` / `set_context(inbound: InboundMessage)` 로 내부 API 변경, `_default_inbound` 보관
  - `send()` 내부가 `OutboundMessage.reply_to(self._default_inbound, content, extra_metadata={"message_id": message_id})` 사용
  - LLM 노출 `send()` 시그니처 (`channel`, `chat_id`, `message_id`, `media`) 호환 유지
  - `_sent_in_turn` 판정에 `thread_ts` 비교 추가
- `channels/slack.py`:
  - `_progress_messages` 에 `defaultdict(asyncio.Lock)` 동반 → post/update atomic
  - `thread_ts is None or thread_ts == ""` 시 progress 캐시 사용 안 함 (DM 안전)
- `tests/test_thread_metadata_propagation.py` (신규): 아래 "검증 계획" 참조

### 제외 (사유)

- UX 개선(리액션, 에러 문구, ephemeral) — 별도 UX 티켓. 컨센서스 결정.
- `split_message` 마지막 chunk 버그 — 별도 PR.
- Telegram `_message_threads` fallback 제거 — Telegram inbound 이 이미 `message_thread_id` 를 실어보내므로 fallback 은 dead code 에 가까우나, 제거는 후속 리팩터(Telegram 실기기 확인 필요).
- `OutboundMessage.metadata` 스키마 필드화 — 파급 범위 큼.
- `cron` 경로의 OutboundMessage — 이번 변경과 직교. cron 은 자체적으로 chat_id/channel 을 구성.
- CHANGELOG 갱신 — 레포 정책 확인 후 결정 (PR 본문에 포함 여부 묻기).

### 후속 과제

- **UX 개선 티켓** (별도 PR): (i) 👀 리액션 ACK + 3초 폴백 텍스트, (ii) 에러 메시지 재시도 안내, (iii) `/new`·`/help` ephemeral, (iv) 다수 사용자 중복 질문 가시화.
- **`split_message` chunk 고아 버그** (별도 PR).
- **Telegram `_message_threads` fallback 정리** (후속 리팩터, Telegram 실기기 확인 필요).
- **Option C 의 확산**: `cron` 경로 등 다른 outbound 생성 지점에도 `OutboundMessage.reply_to()` 혹은 명시적 metadata 구성 적용.
- **`process_direct()` (CLI/cron 경로) 에서 metadata 주입이 필요해지는 시점** 에 설계 재검토.

## 3. 구현 계획

### Phase 1: 계약 정립 (bus.py)

- [ ] `bus.py`
  - `_FORWARDED_METADATA_KEYS = frozenset({"thread_ts", "message_thread_id", "message_ts", "is_channel"})` 상수 추가
  - `OutboundMessage.reply_to(inbound, content, *, extra_metadata=None, **kwargs) -> OutboundMessage` 클래스메서드 추가

### Phase 2: 코어 루프 수정 (core/loop.py)

- [ ] `_handle_stop` (현재 128-130) → `OutboundMessage.reply_to(msg, content=...)`
- [ ] `_dispatch` 예외 핸들러 (현재 144-148) → `reply_to(msg, "Sorry, I encountered an error.")`
- [ ] `_process_message` 의 `/help` 응답 (현재 164-167) → `reply_to(msg, ...)`
- [ ] `_process_message` 의 ACK "생각 중..." (현재 177-183) → `reply_to(msg, "생각 중...", extra_metadata={"_progress": True})`
- [ ] `_process_message` 의 최종 답변 (현재 307-311) → `reply_to(msg, result_text)` (기능상 동일하지만 패턴 일관화)
- [ ] `_handle_new` 의 에러 응답 (현재 321-324) → `reply_to(msg, ...)`
- [ ] `_handle_new` 의 성공 응답 (현재 332) → `reply_to(msg, ...)`
- [ ] role injection 리팩터 (현재 194-197): `msg.metadata["role_name"] = ...` 를 삭제하고 `runtime_metadata = {"role_name": role_name, "role_prompt": role.role_prompt}` 로컬 dict 구성. `ContextBuilder._build_runtime_context(..., msg.metadata)` 호출 지점 두 곳(217, 234)에 `runtime_metadata` 를 합쳐서 전달하거나 별도 파라미터로 분리
- [ ] `message_sender.set_context(msg)` 로 호출 간소화 (현재 173)

### Phase 3: 메시지 툴 수정 (tools/message.py)

- [ ] `MessageSender.__init__` 에 `_default_inbound: InboundMessage | None = None` 추가
- [ ] `set_context(inbound: InboundMessage)` 시그니처로 변경. 기존 파라미터는 제거 (내부 API 라 호환 불필요). 사용처는 loop.py 1곳뿐
- [ ] `send()` 내부가 `self._default_inbound` 기반으로 `OutboundMessage.reply_to(...)` 사용
- [ ] LLM 노출 `send()` 파라미터 (`channel`, `chat_id`, `message_id`, `media`, `content`) 는 그대로 유지
- [ ] `_sent_in_turn` 판정: `thread_ts` 가 `_default_inbound.metadata["thread_ts"]` 와 일치하는지까지 비교

### Phase 4: Slack 캐시 보강 (channels/slack.py)

- [ ] `_progress_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)` 추가
- [ ] `send()` 내 progress 처리 구간(205-230) 을 `async with self._progress_locks[progress_key]:` 블록으로 감쌈
- [ ] `thread_ts` 가 falsy 면 progress 캐시 경로를 건너뛰고 매번 `chat_postMessage` 로 처리 (DM 안전)

### Phase 5: 테스트 추가 (tests/test_thread_metadata_propagation.py)

QA 제안 10개를 그대로 채택하되, 일부는 단일 파라미터라이즈로 병합 가능:

- [ ] `test_reply_to_whitelist` — `OutboundMessage.reply_to()` 가 화이트리스트 외 키(특히 `role_prompt`)를 **제거**하는지
- [ ] `test_reply_to_extra_metadata_override` — `extra_metadata` 키가 inbound 값을 덮어쓰는지
- [ ] `test_ack_inherits_thread_ts` — Slack 채널 inbound → AgentLoop._process_message → 첫 outbound 가 `_progress=True && thread_ts==expected`
- [ ] `test_final_reply_preserves_thread_ts` (회귀)
- [ ] `test_all_outbound_paths_inherit_metadata` — 파라미터라이즈로 `/stop`, `/help`, `/new success`, `/new error`, 에러 핸들러 커버
- [ ] `test_message_sender_tool_inherits_metadata`
- [ ] `test_message_sender_does_not_leak_role_prompt`
- [ ] `test_telegram_inbound_has_message_thread_id` — 기존 `_on_message` 경로가 `metadata["message_thread_id"]` 를 채우는지(AsyncMock Telegram bot)
- [ ] `test_telegram_send_uses_forwarded_metadata` — `TelegramChannel.send(outbound_with_message_thread_id)` 가 bot.send_message 에 `message_thread_id` 를 올바르게 전달
- [ ] `test_slack_progress_cache_key_isolated` — DM 과 채널 루트 멘션이 같은 `chat_id` 로 번갈아 들어와도 캐시 오염 없음 (AsyncMock AsyncWebClient)
- [ ] `test_sent_in_turn_thread_comparison` — `MessageSender` 가 다른 thread 로 보낸 경우 `_sent_in_turn` 이 False 유지

### Phase 6: 검증 및 배포

- [ ] `pytest tests/ -x -v` 로컬 전체 통과
- [ ] `tmux send-keys -t som-agent C-c` → 프로세스 확인 → 재기동 (CLAUDE.md 절차)
- [ ] 수동 테스트 체크리스트 (아래 검증 계획) 실행
- [ ] fork 에 push (`git push fork holmes` — `-u` 없이!)
- [ ] upstream PR 생성 (`gh pr create --repo wishket-aidp/compan.io --base holmes --head aidp-som:holmes`)
- [ ] PR 본문에 수용 기준 체크리스트, 알려진 제외 항목, Telegram 실기기 미검증 리스크 명시
- [ ] 머지 후 `git fetch origin && git reset --hard origin/holmes`
- [ ] 머지 후 `tmux` 세션 재기동

## 4. 리스크 및 대응

| 리스크 | 가능성 | 영향 | 대응 |
|---|---|---|---|
| 화이트리스트가 필요한 키를 누락해 일부 채널 기능 깨짐 | 낮음 | 중 | 현재 확인된 채널별 키는 4개(`thread_ts`, `message_thread_id`, `message_ts`, `is_channel`). 신규 채널 추가 시 여기를 갱신하는 코멘트 추가 |
| Telegram 실기기 미검증 회귀 | 낮음 | 중 | AsyncMock 단위 테스트로 `TelegramChannel.send()` 와 `_on_message` 양쪽 커버. PR 본문에 명시적 리스크 수용 |
| `role_prompt` 로컬 리팩터가 `ContextBuilder._build_runtime_context()` 의 기존 동작을 깸 | 낮음 | 중 | 리팩터 전후로 `_build_runtime_context` 단위 테스트 추가(현재 없다면) 혹은 기존 assertion 보완 |
| `_progress_messages` lock 이 기존 흐름에 지연 유발 | 매우 낮음 | 낮음 | per-key lock 이라 서로 다른 thread 간 경합 없음. 동일 thread 내에서는 어차피 순차 처리 필요 |
| `MessageSender.set_context` 내부 시그니처 변경이 예상치 못한 호출자를 깸 | 매우 낮음 | 낮음 | `loop.py:173` 외에 호출자 없음을 PR 작성 전 Grep 으로 재확인 |
| UX 개선 제외가 "또 스레드 이탈" 유저 리포트를 부를 가능성 | 낮음 | 낮음 | PR 본문과 SOM Slack 공지에 "이번 PR 은 thread 이탈 버그 수정, 에러 문구/리액션은 후속" 명시 |

## 5. 검증 계획

### 수용 기준 (AC) — PR 본문에 복사

- [ ] **AC1**: 채널 루트에서 `@SOM Agent` 멘션 시, 새 스레드가 1개 생성되고 그 안에 ACK("생각 중..."), 중간 progress, 최종 답변이 모두 들어간다. 채널 본문에는 멘션 원본 외에 봇 메시지가 일절 뜨지 않는다.
- [ ] **AC2**: 이미 존재하는 스레드 안에서 `@SOM Agent` 멘션 시, 응답 3단이 원 스레드에 이어 붙는다.
- [ ] **AC3**: Slack DM 에서 메시지 전송 시, 기존과 동일하게 평문으로 받는다(스레드 UI 없음). 오류 없음.
- [ ] **AC4**: 채널 스레드에서 `/stop`, `/help`, `/new` 모두 원 스레드 안에 응답.
- [ ] **AC5**: 에이전트 처리 중 예외 발생 시 에러 메시지가 원 스레드 안에 뜬다.
- [ ] **AC6**: 에이전트가 `message` 툴로 중간 메시지를 보내면 원 스레드 안에 뜬다.
- [ ] **AC7**: 동일 채널에서 두 개 이상 멘션을 연달아 트리거해도 각자 독립 스레드 안에서 동작하고 교차 오염 없음.
- [ ] **AC8**: Telegram 경로(자동화 테스트 수준) 회귀 없음 — `message_thread_id` 정상 전달.

### 수동 테스트 체크리스트 (재기동 후)

- [ ] 채널 A 루트에서 멘션 → AC1
- [ ] 채널 A 기존 스레드에서 멘션 → AC2
- [ ] DM 에서 질문 → AC3
- [ ] 채널 B 루트 멘션 후 스레드 안에서 `/stop`, `/help`, `/new` → AC4
- [ ] 에이전트 실패 유도 (존재하지 않는 툴 호출 요청 등) → AC5
- [ ] 에이전트 응답 중에 `message` 툴이 호출되도록 요청 → AC6
- [ ] 두 사람이 같은 채널에서 동시에 멘션 (또는 본인이 빠르게 두 번) → AC7

## 6. 리뷰 반영 이력

| 피드백 | 출처 | 반영 여부 | 사유 |
|---|---|---|---|
| 수용 기준(AC) 체크리스트 | PM | ✅ | Section 5 에 포함, PR 본문에도 복사 |
| DM 수동 검증 명시 | PM | ✅ | Section 5 체크리스트에 포함 |
| 머지 후 Slack 공지 문구 | PM | 🟡 부분 | "사용자에게 공지 여부 확인" 은 사용자에게 질문으로 전달 예정 |
| Option C 의 후속 트리거 명시 | PM | ✅ | D1 채택으로 자동 무효화 (이번 PR 에 합침) |
| Option C 즉시 채택 (팩토리 + 화이트리스트) | 백엔드 | ✅ | D1 |
| Inbound mutation 제거 | 백엔드 | ✅ | D2 |
| `_sent_in_turn` thread 비교 | 백엔드 | ✅ | D4 |
| `_progress_messages` per-key lock + DM 비활성 | 백엔드 | ✅ | D5 |
| Telegram inbound `message_thread_id` 실태 확인 | 백엔드·QA | ✅ | 플랜 작성 전 확인, `telegram.py:536` 에서 정상 삽입 |
| `MessageSender.send()` LLM 노출 시그니처 분리 | 백엔드 | ✅ | D3, Phase 3 |
| `split_message` chunk 고아 버그 | 백엔드 | ❌ | 별도 PR. 스코프 드리프트 방지 (D7) |
| `send_progress=False` 와 캐시 일관성 기록 | 백엔드 | 🟡 부분 | 테스트 케이스에 1개 추가 가능하지만 기능 영향 없어 우선순위 낮음 |
| 각 outbound 경로별 파라미터라이즈 테스트 | QA | ✅ | Phase 5 `test_all_outbound_paths_inherit_metadata` |
| Telegram AsyncMock 단위 테스트 필수 | QA | ✅ | Phase 5 두 케이스 |
| `MessageSender.set_context` 외부 호출자 전수 확인 | QA | ✅ | Phase 6 직전 Grep |
| 캐시 정규화 회귀 테스트 | QA | ✅ | Phase 5 `test_slack_progress_cache_key_isolated` |
| ACK 을 리액션(👀)으로 대체 | UX | ❌ (후속) | 이번 PR 스코프 밖, 후속 UX 티켓. 컨센서스 D6 |
| 에러 메시지 풍부화 | UX | ❌ (후속) | 후속 UX 티켓. D6 |
| `/new`, `/help` ephemeral | UX | ❌ (후속) | 후속 UX 티켓. 멘탈 모델 논의는 UX 전용으로 분리 |
| 중복 질문 가시화 | UX | ❌ (후속) | 후속 UX 티켓 |
| 접근성 (스레드 숨김) | UX | 📝 기록 | 후속 UX 티켓의 근거로 보존 |
