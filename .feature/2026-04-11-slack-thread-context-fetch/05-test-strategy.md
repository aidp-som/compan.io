# 테스트 전략: Slack 스레드 컨텍스트 자동 fetch

> 작성일: 2026-04-11
> 복잡도 판단: **medium**
> 판단 근거:
> - 변경 파일 5개 (`slack.py`, `loop.py`, `schema.py`, `AGENTS.md`/`TOOLS.md`, 신규 테스트 파일)
> - DB 스키마 변경 없음
> - 새 사용자 흐름은 채널 멘션 시 자동 컨텍스트 주입 (사용자 입장에서는 transparent)
> - 외부 의존성: 새 Slack API 호출 2종 (`conversations_replies`, `users_info`) — 기존 slack-bolt AsyncWebClient 재사용
> - 기존 동작에 대한 영향: 제한적 (slack.py 의 `_on_app_mention` + loop.py 의 `_invoke_claude_turn` 에 hook 추가)
> - 권한/인증: 부분적 (새 OAuth scope `channels:history` 등 필요, runtime auto-disable 로 graceful)
> - 동시성·캐시·prompt injection 등 미묘한 백엔드 디테일이 많아 단위 테스트 필수

## 테스트 범위

### 단위 테스트 (필수, 26개+)

`tests/test_thread_context_fetch.py` 신규. 04-plan.md Phase 8 의 26개 케이스를 그대로 채택. 영역별:

- **Fetch 게이트** (4): config disabled, runtime disabled, blocked channel, single-message thread, self-reply edge
- **포맷팅** (3): happy path, subtype 전체 스킵, filter_secrets inbound 적용
- **캐시** (4): hit, superset hit, invalidate on active thread, LRU eviction
- **동시성** (1): concurrent same thread → lock 직렬화
- **Display name** (3): LRU+TTL, sanitization, users_info error → disable
- **에러 매트릭스** (4): missing_scope flip, subsequent noop, ratelimited retry, not_in_channel
- **Shadow detection** (2 parametrize): 한국어 변형 10개, captured number with cap
- **Integration** (4): _on_app_mention metadata 저장, fresh mention skip, _active_threads 등록 지연, loop.py external-context wrapping
- **회귀 가드** (2): session DB 원본만 저장, TestNoBareOutboundInLoop 통과

### E2E 테스트

생략. 이 레포는 full E2E 스위트가 없고, AsyncMock(WebClient) 기반 단위 테스트가 채널 어댑터까지 충분히 커버.

### UI E2E 테스트

해당 없음 (UI 없음).

### 수동 확인 체크리스트

04-plan.md §5 의 AC1~AC12 + 7개 시나리오를 그대로 채택. 봇 재기동 후 SOM Slack 에서 사용자가 직접 실행. 특히 AC5(missing_scope), AC10(session DB), AC11(concurrency) 는 자동 테스트로도 커버되지만 운영 환경에서 한 번 더 확인 권장.

## 주의 사항

- `asyncio_mode = "auto"` 설정이라 `@pytest.mark.asyncio` 선택적
- AsyncMock(WebClient) 패턴은 어제 머지된 `test_slack_reactions.py` 와 본인이 어제 작성한 `test_thread_metadata_propagation.py` 를 참조
- `_user_display_names` 는 반드시 테스트에서 prewarm 해 결정론 보장 (QA 리뷰 #2)
- ClaudeCLI 는 호출하지 않음 — `_fetch_thread_context` 는 순수하게 Slack API + 포맷팅 로직
