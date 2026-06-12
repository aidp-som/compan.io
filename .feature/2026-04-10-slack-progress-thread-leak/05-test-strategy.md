# 테스트 전략: Slack progress thread leak 수정

> 작성일: 2026-04-10
> 복잡도 판단: **medium**
> 판단 근거:
> - 변경 파일 4개 (bus.py, core/loop.py, tools/message.py, channels/slack.py) + 테스트 파일 1개 신규
> - DB 스키마 변경 없음
> - 새로운 사용자 흐름 없음 (버그 수정)
> - 외부 의존성 추가 없음
> - 기존 동작에 대한 제한적·국소적 영향 (metadata 계약)
> - 권한/인증 무관
> - 단, async 경쟁 상태와 채널별 metadata 차이 등 미묘함이 있어 자동화된 단위 검증 필수

## 테스트 범위

### 단위 테스트 (필수)

`tests/test_thread_metadata_propagation.py` 신규 생성. 04-plan.md Phase 5 의 11개 케이스 채택:

- [ ] `OutboundMessage.reply_to()` 화이트리스트 동작 검증 (role_prompt 차단 포함)
- [ ] `OutboundMessage.reply_to()` 의 `extra_metadata` 오버라이드 동작
- [ ] AgentLoop 의 모든 outbound 경로가 thread_ts 를 상속 (파라미터라이즈: ACK, 최종 답변, `/stop`, `/help`, `/new` 성공/실패, 에러 핸들러)
- [ ] `MessageSender.send()` 가 thread_ts 상속 및 role_prompt 비유출
- [ ] `MessageSender._sent_in_turn` 이 thread_ts 까지 비교
- [ ] Telegram `_on_message` 가 `message_thread_id` 를 metadata 에 정상 삽입 (회귀 가드)
- [ ] `TelegramChannel.send()` 가 outbound metadata 의 `message_thread_id` 로 `send_message` 호출 (회귀 가드)
- [ ] Slack `_progress_messages` 캐시가 DM 과 스레드 멘션 교차 시에도 오염되지 않음

### E2E 테스트

생략. 이 레포는 기존에 full E2E 스위트가 없고(Slack/Telegram 실기기 통합 테스트 없음), 단위 테스트에서 AsyncMock 으로 채널 어댑터까지 검증한다.

### UI E2E 테스트

해당 없음 (UI 없음, Slack 봇).

### 수동 확인 체크리스트

04-plan.md 의 "Section 5 수용 기준(AC1~AC8)" 과 동일. 봇 재기동 후 SOM Slack 워크스페이스에서 사용자가 직접 실행.

## 주의 사항

- `asyncio_mode = "auto"` 설정이라 `@pytest.mark.asyncio` 는 선택적. 기존 테스트 파일의 스타일 참고.
- Slack WebClient mock 은 `AsyncMock(spec=...)` 대신 `AsyncMock()` 으로 시작해 호출 인자를 캡처하는 게 더 단순 (spec 을 맞추기에 slack_sdk 내부 구조가 복잡).
- 테스트 내에서 실제 Claude CLI 는 호출 안 함 — `ClaudeCLI.run` 을 `AsyncMock` 으로 패치.
