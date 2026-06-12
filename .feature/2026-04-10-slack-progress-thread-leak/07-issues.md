# 핵심 이슈 및 요청사항: Slack progress thread leak 수정

> 작성일: 2026-04-10

## 발견된 이슈 (이번 PR 해결 완료)

### [심각도: 높음] `companio/core/loop.py` OutboundMessage 생성 7곳 중 6곳에서 inbound metadata 상속 누락

- **현상**: Slack 채널 멘션 시 ACK "생각 중...", `/stop`·`/help`·`/new`·에러·`message` 툴 중간 메시지가 스레드 밖 채널 본문에 게시.
- **원인**: `loop.py:307-311` 최종 답변 경로만 `metadata=msg.metadata or {}` 로 inbound 를 상속하고, 나머지 6곳은 `metadata={}` 또는 `metadata={"_progress": True}` 로 새 dict 를 만들어 `thread_ts` 증발.
- **영향 범위**: Slack 채널 멘션의 전 응답 경로. Slack DM 은 thread 개념이 없어 영향 없음. Telegram 은 `channels/telegram.py:362-365` 의 `_message_threads` fallback 덕분에 우연히 동작했으나 설계 원칙에서는 벗어난 상태였음.
- **조치**: `bus.py` 에 `OutboundMessage.reply_to_inbound()` 팩토리 + 화이트리스트(`_FORWARDED_METADATA_KEYS`) 도입. `loop.py` 7곳 전부 교체. `tests/test_thread_metadata_propagation.py::TestNoBareOutboundInLoop` 가 소스 레벨 regression guard.

### [심각도: 높음] `role_prompt` inbound metadata mutation 으로 인한 시스템 프롬프트 누수 위험

- **현상**: `loop.py:194-197` 에서 `msg.metadata["role_prompt"] = role.role_prompt` 로 inbound 를 mutate. 만약 mutation 이후의 OutboundMessage 경로가 inbound metadata 를 무차별 상속하면 `role_prompt`(수십~수백 토큰의 시스템 프롬프트 텍스트)가 outbound 메시지·로그·채널로 유출될 수 있음.
- **원인**: inbound 는 bus 를 건너는 데이터 객체인데 이를 직접 변형하는 anti-pattern.
- **영향 범위**: 현재 Slack 채널 `send()` 는 읽는 키만 본다는 우연 때문에 실제 유출이 관측되진 않았음. 하지만 로깅·디버깅·향후 확장 시 언제든 터질 지뢰.
- **조치**: `loop.py` 에 로컬 `context_metadata: dict = dict(msg.metadata or {})` 를 만들어 role injection 을 이 복사본에만 적용. inbound 는 immutable 취급. `reply_to_inbound` 팩토리의 화이트리스트는 이중 방어선.

### [심각도: 중간] `MessageSender._sent_in_turn` 판정이 thread 를 비교 안 함

- **현상**: `tools/message.py` 의 `_sent_in_turn` 플래그가 `channel == default_channel and chat_id == default_chat_id` 만으로 판정. 같은 chat_id 의 다른 스레드로 툴이 중간 메시지를 보내도 True 가 되어, 코어 루프가 "이미 전송됨" 으로 오판하고 **최종 답변을 생략**할 수 있음.
- **영향 범위**: 에이전트가 같은 채널 내 여러 스레드로 중간 메시지를 분산 전송하는 드문 케이스.
- **조치**: 판정에 `thread_ts` / `message_thread_id` 비교 추가. 매칭하는 스레드로 보냈을 때만 `_sent_in_turn=True`.

### [심각도: 중간] `SlackChannel._progress_messages` 캐시 race

- **현상**: `slack.py:185-230` 의 "캐시 체크 → post/update → 캐시 기록" 시퀀스에 락이 없음. 같은 `(chat_id, thread_ts)` 조합에서 두 개의 progress 메시지가 거의 동시에 `send()` 를 통과하면 둘 다 캐시 miss 를 판정해 **중복 post** 가 발생.
- **원인**: 코어 루프는 per-session 직렬화를 하지만 bus 큐를 거쳐 dispatcher 가 비동기 처리하므로 채널 send 순서는 보장 안 됨.
- **조치**: `defaultdict(asyncio.Lock)` 로 per-thread-key lock 도입. 캐시 조작 구간을 `async with` 블록으로 atomic.

### [심각도: 낮음] `MessageSender.set_context` 의 `message_id` 키 mismatch

- **현상**: `loop.py:173` 가 `msg.metadata.get("message_id")` 를 전달하지만 Slack 은 `message_ts` 키를 사용. 항상 `None` 이 저장되어 무의미. 기능 영향 없음(이 값이 현재 사용처 없음).
- **조치**: `set_context(inbound: InboundMessage)` 로 시그니처를 전체 inbound 수납으로 변경. 키 mismatch 자연 소멸.

### [심각도: 낮음] `_progress_messages` 캐시 키가 `thread_ts=None` 일 때 `"chat_id:"` 로 붕괴

- **현상**: DM(thread 없음)과 채널 루트 멘션이 같은 chat_id 로 번갈아 들어오면 이 붕괴된 키가 재사용되어 과거 progress ts 를 엉뚱한 컨텍스트로 `chat_update` 할 위험.
- **영향 범위**: 이론적. 이번 PR 이전의 잠재적 엣지 케이스이며 관측된 사례는 없음.
- **조치**: `thread_ts` 가 falsy 면 progress 캐시 경로 자체를 bypass 하고 매번 `chat_postMessage` 로 새 메시지를 post. DM 과 bare 채널 post 는 progress 편집 기능이 없는 셈.

## 기술 부채 (이번 PR 에서 미해결)

### `channels/slack.py:220-230` chunked post 의 마지막 chunk 오버라이드

- **현상**: 긴 응답이 `split_message` 로 여러 chunk 로 쪼개지면 for 루프 안에서 `result` 변수가 **마지막 chunk 의 응답으로 덮여**, progress 캐시에 마지막 chunk 의 `ts` 만 저장됨. 다음 `chat_update` 는 마지막 chunk 만 갱신하고 앞 chunk 들은 편집 불가 고아가 됨.
- **권장 조치**: 첫 chunk 의 `ts` 를 기록하거나, chunk 별로 캐시 entry 를 리스트로 관리. 같은 파일이지만 다른 계약 이슈라 별도 PR.

### `channels/base.py:104` ACL 거부 응답의 metadata 누락

- **현상**: 접근 거부 메시지("접근 권한이 없습니다...") 가 `OutboundMessage(channel=..., chat_id=..., content=...)` 로 bare 생성되어 thread 밖으로 나감. 동일 계열 버그.
- **권장 조치**: `_handle_message` 내 ACL 거부 시에도 `_FORWARDED_METADATA_KEYS` 로 metadata 를 구성하거나 `OutboundMessage.reply_to_inbound` 전에 InboundMessage 를 임시 구성. 스코프 드리프트 방지 차원에서 이 PR 에 안 넣음.

### Telegram `_message_threads` fallback dead code 화

- **현상**: `channels/telegram.py:362-365` 에서 `message_thread_id` 가 없으면 `reply_to_message_id` 로 역조회하는 캐시 fallback 이 있음. 이번 PR 로 Telegram inbound 이 이미 화이트리스트 상속 대상이 되어 fallback 이 실제로 히트할 일이 거의 없어짐.
- **권장 조치**: Telegram 실기기 테스트 후 fallback 제거. 이 머신에는 Telegram 환경이 없어 미실시.

### `tests/test_claude_cli.py::TestBuildCmd::{test_basic, test_add_dir_home}` Windows 환경 breakage

- **현상**: `cmd[0] == "claude"` 를 단언하지만 Windows 에선 `claude.CMD` 래퍼 경로로 해석됨. 이 PR 과 **무관**, main 에서도 동일하게 실패함을 `git stash` 후 재확인.
- **권장 조치**: `Path(cmd[0]).stem == "claude"` 식으로 느슨화하거나 Windows 조건 분기.

### 봇 권한 레이어 부재 — 봇이 자기 소스 편집 가능 (CLAUDE.md 알려진 quirk #4)

- **현상**: default role 에 Edit/Write 차단이 안 걸려 있어 봇이 자기 소스 수정 가능. 이 PR 과 무관한 오래된 quirk.
- **권장 조치**: 별도 보안 강화 티켓.

## 요청사항

### 사용자 (이 세션의 운영자)

- [ ] **봇 재기동 타이밍 결정**: 이 PR 의 변경은 editable install 덕분에 재기동 한 번으로 반영됨. 현재 봇은 SOM Slack 에서 다른 사용자가 방금 이용한 상태이므로 아무 때나 재기동하면 안 됨. 06-dev-report.md 의 "재기동 절차" 를 참고해 운영자가 적절한 시점에 실행.
- [ ] **AC1~AC7 수동 검증**: 재기동 후 Slack 에서 직접 시나리오 실행. 하나라도 어긋나면 이슈 리포트.
- [ ] **PR 제출 허가**: 검증 통과 시 `git push fork holmes` + `gh pr create --repo wishket-aidp/compan.io --base holmes --head aidp-som:holmes` 실행 여부 확인 요망. CLAUDE.md 의 git 워크플로우 규칙(`-u` 금지 등) 준수.
- [ ] **SOM Slack 사용자 공지 여부**: 이 버그는 SOM 멤버가 이미 인지했을 가능성이 높음. "스레드 이탈 버그 수정됨" 공지를 어느 채널에 올릴지 또는 공지 없이 갈지 결정 필요 (PM 페르소나가 지적한 사항).

### 업스트림 메인테이너 (PR 리뷰어)

- [ ] **화이트리스트(`_FORWARDED_METADATA_KEYS`) 합의**: 새 키(`thread_ts`, `message_thread_id`, `message_ts`, `message_id`, `is_channel`, `is_group`)가 적절한지 확인. 향후 새 채널(예: Discord) 추가 시 이 상수를 갱신해야 함 — 코드 주석에 명시.
- [ ] **`OutboundMessage.reply_to_inbound` 네이밍 확인**: `OutboundMessage.reply_to` 는 이미 필드로 예약되어 있어 `reply_to_inbound` 로 명명. 다른 이름(`from_inbound`, `for_inbound`) 을 선호하면 알려주기.
- [ ] **`MessageSender.set_context` 시그니처 변경 승인**: 외부 호출자는 `loop.py:173` 단 한 곳. LLM 노출 `send()` 시그니처는 호환 유지.

## 참고사항

- 이번 PR 의 기획·리뷰·플랜·테스트 전략·결과 보고서 모두 `.feature/2026-04-10-slack-progress-thread-leak/` 에 보존됨 (7개 파일).
- 페르소나 리뷰(03-review.md) 에서 UX 디자이너가 강하게 권한 개선안(리액션 ACK, 에러 문구 풍부화, `/new` ephemeral)은 이번 PR 스코프 밖으로 분리. 별도 UX 티켓 필요.
- 테스트 `TestNoBareOutboundInLoop` 는 `companio/core/loop.py` 에 `OutboundMessage(...)` 직접 호출이 추가되는 순간 실패한다 — 향후 이 파일에 새 outbound 경로를 추가하는 개발자가 계약을 잊지 않도록 설계된 regression guard.
- 실패 2개(`test_claude_cli.py::TestBuildCmd::test_basic`, `test_add_dir_home`)는 pre-existing Windows 이슈이며 이 PR 과 무관. main 에서도 동일 실패. PR 본문에 주석 필요.
