# 페르소나 리뷰: Slack 멘션 ACK 리액션 + 채널 공지 옵션

> 리뷰일: 2026-04-11
> 리뷰 깊이: deep
> 기반: 01-analysis.md, 02-design.md

## 페르소나 선정 근거

| 페르소나 | 선정 이유 |
|---|---|
| **PM** | 두 피처 동시 진행의 우선순위/스코프 결정, 사용자 가치 검증 |
| **UX 디자이너** | 리액션 기반 피드백의 인지성, broadcast 정보 노출의 사용자 멘탈 모델 |
| **백엔드 개발자** | 동시성/lifecycle/에러 경로, metadata 경계 유지, Slack API 견고성 |
| **QA 엔지니어** | LLM 비결정성 검증 전략, 회귀 가드, 엣지 케이스 커버리지 |
| **DevOps** | scope 추가 절차, 롤아웃/롤백, feature flag, 관찰성 |

선정 이유: 이 피처는 **기술 구현보다 "실제 배포 후 검증"이 어려운 성격**을 가진다. 특히 피처 2는 LLM 판단에 의존하는 확률적 UX라서 PM·QA·UX·DevOps가 모두 관측/롤백 관점에서 집중 검토가 필요했다.

---

## 리뷰 결과 요약

| 페르소나 | 평가 | 핵심 지적 |
|---|---|---|
| PM | 🟡 개선 필요 | 성공 지표 부재, MVP 스코프 팽창, 두 피처를 분리해야 함 |
| UX 디자이너 | 🟡 개선 필요 | 👀→✅ 전환이 실제론 사후 감사 로그 역할, broadcast는 LLM 자동 판단 + 컨펌 없으면 위험 |
| 백엔드 개발자 | 🟡 개선 필요 | `_active_ack_reactions` race, send_done 누락 경로, BaseChannel 추상화는 metadata 기반이 나음 |
| QA 엔지니어 | 🟡 개선 필요 | LLM 트리거 검증 전략 2층 분리 필요, `_reaction_lifecycle` 키 도입 재고, 엣지케이스 다수 |
| DevOps | 🟡 개선 필요 | feature flag 필수, 메모리 누수 경로, scope rollout runbook 필요 |

전원 🟡. 설계 방향은 공통적으로 OK이나 **디테일·측정·롤아웃**이 모두 부족.

---

## 페르소나별 핵심 피드백 (요약)

> 원문 전문은 병렬 에이전트 실행 결과에 포함됨. 여기서는 실행 가능한 결정 사항 중심.

### PM 핵심

1. **두 피처 분리**: 설계는 함께, PR/배포는 분리. 피처 1 먼저 (피처 2는 확률적 UX).
2. **MVP 스코프 축소**: 피처 1은 `eyes` 추가 + 최종 응답 후 제거 2단계면 충분. ✅/❌ 전환, 5초 텍스트 fallback, config 커스터마이징은 **전부 후속 PR로**.
3. **성공 지표 필수**: 피처 2는 2주간 `share_to_channel=True` 호출 0건이면 실패 선언. 지금 설계엔 관측 수단 자체가 없다.
4. **Shadow 감지 로거**: inbound에 트리거 후보 문구가 있는데 LLM이 도구 미호출 → WARN 로그. 미탐 측정의 유일한 수단.
5. **사용자 공지 계획**: 피처 2는 사용자가 트리거 문구를 모르면 죽은 기능. 릴리스 시 각 인스턴스 대표 채널에 공지 메시지 1회 자동 송신.

### UX 디자이너 핵심

1. **👀 → ✅ 전환은 "사후 감사 로그"**: 사용자는 응답 후 스레드 하단을 보지, 원본 메시지의 리액션을 다시 올려다보지 않는다. 진행 상태 피드백으로 기능 X. 이 역할 재포지셔닝 필요.
2. **리액션 lifecycle 단순화**: 👀 → 제거 (완료 자체는 스레드 응답이 신호) 또는 👀 → ✅ 중 택일. 3단계는 과잉.
3. **`reply_broadcast`는 예상보다 강한 노출 행위**: 채널 피드 최상단 + 알림 발생. "LLM 자동 판단 + 즉시 broadcast"는 비가역 액션에 비결정성 붙이는 안티패턴.
4. **컨펌 단계 필수**: Slack Block Kit으로 "[공유] [취소]" 버튼. 사용자가 명시적으로 "채널에도 공유해"라고 발화한 경우만 즉시 broadcast, 애매한 경우는 컨펌.
5. **5초 fallback은 Slack 맥락에서 길다**: 시간 기반이 아니라 **스테이지 기반** (Claude CLI 시작 / Tool 호출 시점)으로 피드백 전환.
6. **접근성**: 리액션은 스크린 리더가 실시간으로 안 읽음. 시각장애 사용자에겐 텍스트 fallback이 선택 아닌 필수.
7. **모바일 UX**: 모바일 Slack에서 리액션 배지는 매우 작음. 외근 많은 SOM Agent 사용자에겐 text ACK가 여전히 1차 피드백이어야 함.
8. **이모지 선택**: `x`는 peer-to-peer 반대 투표와 중첩. `warning`/`exclamation`이 덜 중첩적.
9. **스레드 밖 observer 고려**: broadcast된 답변만 채널에서 본 사람은 맥락을 모름. 답변에 "**@요청자**의 질문에 대한 답변:" 프리픽스 필요.

### 백엔드 개발자 핵심

1. **BaseChannel.send_ack/send_done은 loop→channel 직접 참조를 낳음 → PR#2의 bus 경계 위반.** 대신 metadata 기반 OutboundMessage(`_reaction_lifecycle="ack|done_success|done_error"`)로 통일. 단, ACK 리액션만 `_on_app_mention`에서 직접 `_add_reaction` 호출(lock 밖 즉시성 확보).
2. **`_active_ack_reactions` race condition**: dict value를 `asyncio.Task`로 두고 `send_done`에서 `await task` 후 remove. 그래야 순서 보장 (add 전에 remove 호출되는 경우 방지).
3. **`_dispatch` try/finally로 done 누락 방지**: `CancelledError`, `/stop`, `/help`/`/new` early return, `is_error+result_text`, `_sent_in_turn=True` 등 여러 경로에서 현재 `send_done` 호출 보장 안 됨.
4. **Rate limit 대응 필수**: `reactions.add`는 Tier 2 (20+/min). 요청당 3 API 호출로 증폭. 429/`already_reacted`/`message_not_found`/`invalid_auth` 에러 코드별 명시적 동작 필요.
5. **Broadcast는 마지막 chunk에만**: 긴 응답 split 시 모든 chunk가 broadcast되면 채널 본문에 같은 메시지 여러 번 노출. 마지막 chunk에만 `reply_broadcast=True`.
6. **`share_to_channel` 오용 가드**: role-based (`can_broadcast` 플래그 또는 `broadcast_allowed_users` 설정). 현재 LLM이 자유롭게 호출 가능.
7. **Thread_ts 없으면 broadcast 무시**: `is_channel` 체크만으로는 부족. `thread_ts` falsy도 방어.
8. **`_reaction_lifecycle` 키 선택이라면 빼자**: QA 지적과 동일. 키가 없으면 누수 가능성 0.
9. **Text fallback task cancel**: 응답 완료 시 `fallback_task.cancel()` 필수.

### QA 엔지니어 핵심

1. **`_reaction_lifecycle` 키 도입 자체를 재고**: "선택"으로 되어 있는데, 뺄 수 있으면 빼자. 키가 없으면 회귀 가드 불필요.
2. **LLM 트리거 검증 2층 분리**:
   - **Unit (CI 게이트)**: `MessageSender.send(share_to_channel=True)` 호출 시 metadata 세팅 검증 (결정적)
   - **Eval (CI 외)**: 20 positive + 20 negative prompt로 LLM trigger recall/precision 측정. 주 1회 스크립트, CI 게이트 X (flaky 방지).
3. **Graceful handling의 정의 고착**: 9가지 Slack 에러 코드(404/429/`already_reacted`/`not_in_channel`/`invalid_auth`/`missing_scope`/network/...)별로 silent vs ERROR 구분을 테스트로 잠가야 함.
4. **동시 멘션 격리 테스트**: 두 멘션이 같은 `chat_id`, 다른 `message_ts`일 때 lifecycle 독립성 보장.
5. **Regression guard 추가**: `_FORWARDED_METADATA_KEYS`가 이번 PR에서 변하지 않는지 명시적 잠금. inbound에 reply_broadcast/`_reaction_lifecycle` 넣어도 outbound에 새지 않는지.
6. **Grep guard for `_on_app_mention`**: `message_ts` 추출이 `event["ts"]`인지 (PR#2의 Telegram `message_thread_id` grep guard 패턴).
7. **텍스트 fallback cancel 테스트**: 응답 0.1초 → progress 0건 assertion.
8. **E2E AC 20+ 항목 제시** (원본 리뷰 참고).

### DevOps 핵심

1. **Feature flag 3단 구조 필수**:
   ```
   slack.ack_reactions.enabled: bool = False   # 마스터
   slack.broadcast.enabled: bool = False        # 피처 2 마스터
   slack.broadcast.blocked_channels: list[str] = []
   ```
   default OFF로 머지 → 인스턴스별 단계적 ON.
2. **Scope 자동 감지 + 자가 비활성화**: 첫 `reactions.add`의 `missing_scope`/`invalid_auth` 에러 잡으면 해당 인스턴스 런타임 비활성화 + 경고 1회. 설정 실수를 폭발이 아닌 관찰 가능한 경고로.
3. **`_active_ack_reactions` TTL/bounded**: `Claude CLI hang/timeout` 경로에서 `send_done` 누락 시 orphan 누적. TTL cleanup 또는 `OrderedDict` + maxsize.
4. **구조화 로그 이벤트**:
   - `slack.reaction.add` (status=ok|missing_scope|not_found|other)
   - `slack.reaction.remove`
   - `slack.broadcast.sent` (channel, thread_ts, content_len)
   - `slack.ack_reactions.pending_size` (주기 게이지)
5. **운영자 runbook** (`docs/ops/slack-reactions-rollout.md`): Slack App scope 추가 절차, reinstall 순서, 스모크 테스트, 롤백 절차.
6. **Canary 롤아웃**: bbackar(내부) → holmes-family/incheon-family(저위험) → SOM Agent/wishket-aidp(외부) 순.
7. **`reactions:read`도 함께 scope 추가**: 미래 재작업 회피. 분석본 §7.5의 본인 권고와 일치시킴.
8. **피처 1·2 분리 릴리스**: 피처 2는 scope 변경 불필요 → 즉시 배포 가능. 피처 1만 reinstall 동반.
9. **Broadcast 감사 로그**: 누가/언제/어떤 내용/어느 채널. 오탐 시 소급 조사 수단.

---

## 크로스 커팅 분석

### 공통 우려 (3명 이상 지적)

| 주제 | 지적한 페르소나 |
|---|---|
| 두 피처를 분리해야 함 | PM, DevOps, Backend |
| `_active_ack_reactions` 메모리 누수/race | Backend, QA, DevOps |
| `send_done` 누락 경로 (에러, cancel, slash command) | Backend, QA |
| Feature flag / kill switch 필요 | DevOps, PM |
| Rate limit 대응 | Backend, DevOps |
| 성공 지표/관측성 부재 | PM, DevOps, QA |
| `_reaction_lifecycle` 키 재고 (빼거나 제한) | Backend, QA |
| Broadcast 확인/가드 (LLM 자동 판단 위험) | UX, Backend, PM |
| MVP 스코프 축소 (5초 fallback 등) | PM, UX |
| Scope rollout 운영 부담 | DevOps, PM |

### 의견 충돌

| 쟁점 | A 입장 | B 입장 | 해소 방안 |
|---|---|---|---|
| BaseChannel.send_ack vs metadata 기반 | 설계(인터페이스) | Backend, QA(metadata) | **metadata 채택** — bus 경계 유지, 순서 보장, 테스트 용이 |
| ACK 리액션 발송 위치 | 설계 §3.2(loop.py에서) vs 설계 §7(lock 밖 즉시) | 자체 모순. PM 지적 | **`_on_app_mention`에서 직접 `_add_reaction` 호출** — lock 밖 즉시성 확보 |
| ✅/❌ 3단계 전환 | 설계 | UX(2단계), PM(MVP 2단계) | **MVP는 2단계** (`eyes` 추가 → 응답 후 제거). ✅/❌는 후속 |
| 텍스트 fallback | 설계(5초 후 발송) | PM(제거), UX(5초 길다/스테이지 기반) | **MVP는 제거**. 필요성 증명 후 추가 |
| Broadcast 트리거 방식 | 설계(LLM 판단) | UX(컨펌 버튼) | **MVP는 LLM 판단 + Shadow 감지**. 2주 후 결과 보고 컨펌 단계 도입 재검토 |

### 컨센서스

1. **피처 1과 피처 2는 분리해서 별개 PR로 진행**. 같은 설계 문서는 유지 (인프라 공유 때문).
2. **피처 1 우선**, 피처 2는 후속 또는 병행이되 롤백 독립.
3. **MVP 엄격히 축소**: 피처 1은 2단계 리액션, 피처 2는 LLM tool option + shadow 감지만.
4. **metadata 기반 lifecycle 설계**는 폐기. ACK 리액션은 `_on_app_mention`에서 직접, done은 metadata outbound로 통일 (Backend 제안 (1) 채택).
5. **Feature flag 필수**, default OFF.
6. **`_active_ack_reactions`에 TTL/bounded 가드 필수**, `_dispatch` try/finally로 `send_done` 보장.
7. **Graceful handling 9가지 에러 코드별 동작 명시화 + 테스트 잠금**.
8. **Shadow 감지 로거 필수** (피처 2 관측성의 핵심).
9. **운영 runbook 작성**이 PR 머지 조건.
10. **사용자 공지 계획** 릴리스 체크리스트에 포함.
