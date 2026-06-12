# 페르소나 리뷰: Slack progress 메시지 thread 누락

> 리뷰일: 2026-04-10
> 리뷰 깊이: deep
> 기반: 01-analysis.md, 02-design.md

## 페르소나 선정 근거

이번 수정은 Python 백엔드 코드 단독 변경이고, DB 스키마 변경·인프라·인증/보안 직접 영향이 없다. 대신 **사용자 체감 경험(스레드 격리)**, **호출 관습(metadata 계약)**, **검증 전략(6개 outbound 경로 회귀)** 세 축이 핵심이다. 다음 4명을 선정했다:

- **PM**: 스코프/수용 기준/사용자 소통 점검
- **시니어 백엔드 개발자**: API 설계·계약·동시성·관습 점검 (deep, 코드 라인 인용)
- **QA 엔지니어**: 테스트 전략·엣지 케이스·회귀 리스크 점검
- **UX 디자이너**: 스레드 격리가 사용자 경험에 주는 긍정/부정 점검

CEO / DBA / DevOps / 보안은 이 변경의 성격과 거리가 멀어 제외.

---

## 리뷰 결과

### PM 리뷰

**평가**: 🟢 좋음

**강점**
- 단일 root cause(metadata 상속 누락)를 정확히 짚고 직접/파생 증상을 명확히 구분해 사용자 체감 문제를 빠짐없이 커버.
- Option A/B/C/D 트레이드오프가 PM 판단에 필요한 수준으로 정리되어 있음. "Option B 먼저, C 는 후속" 단계적 접근이 스코프 드리프트를 잘 막음.
- Out-of-Scope 와 후속 과제가 명시되어 있어 이번 PR 에서 안 할 것이 분명함(Telegram 리팩터, 스키마 변경 등).

**우려 사항**
1. [심각도: 중간] **사용자 스토리가 "개발자 언어" 로만 기술됨**. "스레드 안에 뜬다" 외에, 최종 사용자(SOM 슬랙 멤버) 관점의 수용 기준(AC)이 없음 — 예: "채널 루트 멘션 시 새 스레드가 하나만 생성되고 ACK/중간/최종이 모두 그 스레드에 모인다", "기존 스레드 내 멘션 시 원 스레드에 붙는다" 등을 체크리스트로 박아야 QA 가 명확.
2. [심각도: 중간] **DM 리스크(5절)에 "수동 확인 권장" 으로만 남아 있음**. DM 은 사용 빈도가 낮지 않은 경로인데 "Slack 서버가 알아서 처리할 것" 이라는 추정에 의존. 최소 1회 수동 확인을 구현 순서 5단계에 명시적으로 넣어야 함.
3. [심각도: 낮음] **릴리즈/사용자 공지 계획 부재**. 이 버그는 SOM 슬랙 사용자가 이미 인지하고 있을 가능성이 높음. 머지 후 채널에 한 줄 공지 필요 여부에 대한 언급이 없음.
4. [심각도: 낮음] **Option C 의 "후속" 의사결정 트리거가 모호**. "리뷰어 의견에 따라" 로만 적혀 있는데, 후속 PR 을 실제로 띄울 트리거(예: 유사 버그 1회 더 발생 시)를 정해두지 않으면 영구히 미뤄짐.

**제안**
1. 02-design.md 2절(스코프) 아래 **"수용 기준"** 서브섹션 추가: 체크리스트 4~5개.
2. 3.5 구현 순서 5단계(수동 검증)에 **DM 1 케이스 포함, 총 4 케이스** 로 명시.
3. 후속 과제에 **"Option C 도입 트리거: 동일 계열 리그레션 1회 추가 발생 시 즉시"**.
4. 머지 후 SOM Slack 공지 문구 1줄을 PR 본문 하단에 템플릿으로 포함.

**놓친 부분**
- 사용자 체감 이력 기록 없음: 언제부터(`d44c1ae` 이후?) 사용자가 불편했는지가 없음 — 재발 시 회귀 판정 기준 애매.
- QA 환경 한계: Telegram 수동 미검증이라면 "Telegram 리그레션은 단위 테스트로만 보증" 이라는 리스크 수용을 명시.
- CHANGELOG 정책이 이 레포에 있는지 언급 없음.

---

### 백엔드 개발자 리뷰

**평가**: 🟡 개선 필요

**강점**
- Root cause 를 "단일 계약 위반(inbound metadata 상속 누락)" 으로 정확히 특정했고, 수정 방향이 코어의 채널 불가지론(channel agnostic) 원칙과 일치.
- Option A/B/C/D 트레이드오프 비교가 현실적이고, Option D(채널 레벨 전역 상태)를 거부한 논거가 타당. 리그레션 방지 관점에서 테스트 대상(bus 레벨에서 metadata forward 검증)도 잘 잡힘.

**우려 사항**

1. **[심각도: 높음] `role_name`/`role_prompt` 오염 리스크 — Option B 의 암묵 계약 불안전성.** `loop.py:194-197` 에서 `msg.metadata["role_name"]`, `msg.metadata["role_prompt"]` 를 **inbound metadata 에 mutate** 함. Option B 의 `{**(msg.metadata or {}), ...}` 패턴을 최종 답변 경로(`loop.py:307-311`)와 에러 경로(`loop.py:144-148`)에 적용하면, role injection **후**에 실행되므로 `role_prompt`(수십~수백 토큰짜리 시스템 프롬프트 텍스트)가 OutboundMessage metadata 에 실려 dispatcher 를 거쳐 채널로 흘러감. Slack 채널은 현재 읽는 키만 보지만, 로깅·디버깅·향후 확장 시 민감한 role prompt 가 outbound 로그에 남을 수 있음. 설계서의 "추가 키가 섞여도 무해" 주장은 **검증되지 않은 가정**.

2. **[심각도: 중간] `MessageSender._sent_in_turn` 플래그와 metadata 상속의 상호작용 미고려.** `loop.py:304` 에서 `_sent_in_turn` 이 true 면 최종 답변 return 을 생략. `message.py:69` 는 `channel == default_channel and chat_id == default_chat_id` 만으로 판단하는데, **thread_ts 비교가 없음**. 동일 chat_id 에서 다른 스레드로 메시지를 보낸 경우에도 `_sent_in_turn=True` 가 되어 최종 답변이 유실.

3. **[심각도: 중간] `_progress_messages` 캐시 race.** `slack.py:104, 205-230` 캐시는 key 단위 lock 이 없음. 동일 세션 락은 코어 루프 레벨이고, bus 큐를 거친 후의 dispatcher 순서는 보장 안 됨. 동일 `(chat_id, thread_ts)` 로 두 progress 가 거의 동시에 `send()` 를 통과하면, 첫 `chat_postMessage` 완료 전에 두 번째가 캐시 체크를 통과 못 해 **둘 다 새 메시지로 post**.

4. **[심각도: 중간] Telegram inbound 가 `message_thread_id` 를 metadata 에 실제로 싣는지 확인 안 됨.** 만약 실지 않는다면 Telegram 은 기존대로 `_message_threads` fallback 에 의존해야 하고, Slack 만 상속 방식으로 가는 비대칭. 이번 PR 테스트 범위에 Telegram metadata 실태 확인 필수.

5. **[심각도: 낮음] `MessageSender.send()` 시그니처 노출.** 이 함수는 Claude CLI 의 **tool** 로 LLM 이 직접 호출. `metadata` 를 툴 파라미터로 노출하면 안 됨(LLM 이 잘못 채우면 내부 상태 오염). `set_context(metadata=...)` 는 내부 API, `send(..., message_id=...)` 는 외부(LLM) API 로 **완전히 분리** 필요.

**제안**

1. **Option C 를 이번 PR 에 합치되, role injection 이동 + 화이트리스트 방식으로.** `OutboundMessage.reply_to()` 팩토리를 `bus.py` 에 추가하고, metadata 상속을 **화이트리스트**로 좁힘:

```python
# bus.py
_FORWARDED_METADATA_KEYS = frozenset({
    "thread_ts", "message_thread_id", "message_ts", "is_channel",
})

@classmethod
def reply_to(cls, inbound, content, *, extra_metadata=None, **kwargs):
    md = {k: v for k, v in (inbound.metadata or {}).items()
          if k in _FORWARDED_METADATA_KEYS}
    if extra_metadata:
        md.update(extra_metadata)
    return cls(channel=inbound.channel, chat_id=inbound.chat_id,
               content=content, metadata=md, **kwargs)
```

그리고 `loop.py:194-197` 의 role injection 을 **별도 dict (`runtime_metadata`)** 로 분리 — inbound mutation 중단.

2. **`_sent_in_turn` 판정에 thread 추가:**
```python
if (channel == self._default_channel and chat_id == self._default_chat_id
    and msg.metadata.get("thread_ts") == self._default_metadata.get("thread_ts")):
    self._sent_in_turn = True
```

3. **`_progress_messages` 에 per-key asyncio.Lock**. 캐시 체크 → post/update → 캐시 기록을 atomic 으로. `thread_ts` 가 `None` 이면 progress 편집 비활성화(매번 새 메시지).

4. **테스트**: (a) `MessageSender.send()` 출력에 thread_ts 포함, (b) ACK→tool msg→final 셋 다 같은 thread_ts, (c) `role_prompt` 가 outbound metadata 에 **유출되지 않음**(화이트리스트 검증), (d) Telegram `_on_message` 가 metadata 에 `message_thread_id` 를 실제로 싣는지 확인.

5. **`MessageSender.set_context()` 내부 API 를 inbound 통째 수납으로 단순화:**
```python
def set_context(self, inbound: InboundMessage) -> None:
    self._inbound = inbound  # send() 는 OutboundMessage.reply_to(inbound, ...) 사용
```
`loop.py:173` 의 `message_id` / `message_ts` 키 mismatch 버그도 자연 소멸.

**놓친 부분**

- **Inbound mutation anti-pattern**: `loop.py:194-197` 의 mutation 이 outbound 오염 경로가 됨. 분석서도 설계서도 이 위험을 언급 안 함.
- **`split_message` 의 chunked post 버그**(`slack.py:220-227`): 루프 변수 `result` 가 마지막 chunk 로 덮이므로 progress 캐시에 **마지막** chunk 의 `ts` 만 저장. 다음 `chat_update` 시 마지막 chunk 만 편집되고 앞 chunk 들은 고아. 설계 범위 밖이지만 같은 파일을 건드리는 김에 짚을 가치.
- **`send_progress=False` 시 drop** 이 `_progress_messages` 일관성 가정을 흔듦 — 현 기능상 문제 없으나 기록 가치.

---

### QA 엔지니어 리뷰

**평가**: 🟡 개선 필요 (설계 방향은 확고, 검증 전략 보강 필요)

**강점**
- 단일 root cause → 6개 지점 일괄 수정이라는 대응 범위가 증상 클러스터와 정확히 일치, 한 번에 검증할 단위가 명확.
- `metadata={**(msg.metadata or {}), ...}` 패턴이 **테이블 주도 파라미터라이즈 테스트**로 검증하기 쉬운 형태.
- 기존 `tests/test_agent_core.py` 에 `MessageBus` + `InboundMessage` 조립 패턴이 이미 있고 `pytest-asyncio asyncio_mode=auto` 설정 완료 — 접붙이기 자연스러움.

**우려 사항**
1. **[심각도: 높음] bus 레벨 단위 테스트가 6개 지점 일부만 커버할 위험**. `_handle_stop`, `/help`, `/new`, 에러 경로는 각각 다른 진입점. **각 경로별 파라미터라이즈 케이스**를 명시적으로 요구해야 함. 안 그러면 "ACK 만 고쳤는데 테스트는 통과" 재현.
2. **[심각도: 높음] Telegram 회귀 검증 공백**. "수동 Telegram 체크 생략" 은 위험. `TelegramChannel.send()` 를 `AsyncMock` 으로 감싼 단위 테스트는 실기기 없이도 가능하니 필수.
3. **[심각도: 중간] `_progress_messages` 캐시 정규화의 검증 누락**. DM(`thread_ts=None`) 과 스레드 루트 멘션이 같은 `chat_id` 로 번갈아 들어올 때 **캐시 오염 회귀 테스트** 필요.
4. **[심각도: 중간] `MessageSender.set_context` 시그니처 호환성** — 외부 호출자 전수 확인(Grep) 필요.
5. **[심각도: 낮음] 병행성 케이스 부재** — 동일 chat_id 에 두 스레드 동시 활성 시 교차 오염 언급 없음.

**제안 (테스트 케이스 목록)**

새 파일 `tests/test_thread_metadata_propagation.py`:

1. `test_ack_inherits_thread_ts` — ACK 가 `_progress=True && thread_ts=="1.23"`.
2. `test_final_reply_preserves_thread_ts` — 회귀 보호.
3. `test_stop_command_inherits_metadata`.
4. `test_help_command_inherits_metadata`.
5. `test_new_command_success_and_error`.
6. `test_error_handler_inherits_metadata`.
7. `test_message_sender_tool_inherits_metadata`.
8. `test_telegram_metadata_key_isolated` — AsyncMock bot 으로 `message_thread_id` 해석 검증.
9. `test_progress_cache_key_dm_vs_thread` — AsyncMock WebClient.
10. `test_dm_flow_no_thread_leak`.

**수동 테스트 플랜** (재기동 후)
- (a) 채널 루트 멘션 → 3단 같은 새 스레드.
- (b) 기존 스레드 내 멘션 → 3단 원 스레드.
- (c) DM → 평문, 스레드 UI 없음.
- (d) (a)(b)(c) 각각에서 `/stop`, `/help`, `/new`.
- (e) 에이전트 실패 유도 → 에러가 스레드 안.
- (f) 동일 채널 두 멘션 연달아 → 각자 독립 스레드, 교차 오염 없음.
- (g) 재기동 직후 잔여 캐시 없는지.

**놓친 부분**
- **`send_progress=false` 설정 교호작용**: 드롭돼도 최종 답변은 스레드 안에 있는지 고정.
- **Slack WebClient mock 전략**: `AsyncMock(spec=slack_sdk.web.async_client.AsyncWebClient)` 로 `chat_postMessage`/`chat_update` 인자 캡처 단언.
- **리그레션 가드**: `loop.py` 에 `def _reply_metadata(msg, **extra)` 내부 헬퍼 하나 — 투자 대비 회귀 방지 효과 큼. 이번 PR 포함 권장(Option C 의 부분 채택).

---

### UX 디자이너 리뷰

**평가**: 🟡 개선 필요

**강점**
- 채널 본문/스레드 파편화 해소 → "대화의 장소성" 회복. 나중에 스레드만 열어도 전체 흐름을 선형 복기 가능.
- 최종 답변 경로는 그대로 두고 주변만 맞춤 — 사용자 익숙한 "정답 위치" 불변.
- `_progress` 캐시 정규화 덕분에 스레드 안에서 "생각 중..." 이 한 자리에서 갱신 → 노이즈 미누적.

**우려 사항**
1. **[높음] "생각 중..." 이 여전히 텍스트** — 스레드 세로 폭이 좁아 노이즈가 상대적으로 더 커 보임. Slack 관습상 봇 인지는 👀 리액션이 더 가볍고 접근성 친화적.
2. **[높음] 에러 메시지가 한 줄 "Sorry, I encountered an error."** — 스레드에 가둘수록 빈약함 도드라짐. 재시도/관리자 호출/세션 리셋 여부를 알 수 없음.
3. **[중간] `/new` 가 스레드에 갇힘** — 의미상 "이 스레드를 떠나 새 대화" 인데 응답이 같은 스레드 안이면 멘탈 모델과 충돌. 리셋 안내는 ephemeral 또는 채널 루트가 자연스러움.
4. **[중간] 다수 사용자 동시 사용 시 사일로화** — 각자 자기 스레드에 갇히면 옆 사람 중복 질문을 못 봄. 기존 본문 노출은 우연한 정보 공유 효과가 있었음.

**제안**
1. ACK 는 텍스트 대신 **원본 멘션에 👀 리액션**, 완료 시 ✅ 로 교체. 텍스트 "생각 중..." 은 3초 이상 지연될 때만 스레드에 폴백.
2. 에러 메시지에 **재시도 힌트 + 세션 상태 + 원인 한 줄**.
3. `/new` 응답은 **ephemeral**, `/help` 도 ephemeral 권장.
4. (후속) 긴 답변에 "채널에도 공유" 버튼.

**놓친 부분**
- 접근성: 스레드는 모바일·스크린 리더에서 펼치기 전 숨겨짐, 리액션 병행이 유리.
- 알림 볼륨: 스레드 내 봇 메시지마다 알림 가능 — `chat_update` 활용을 UX 근거로 재확인.
- ACK 타이밍 SLA: 500ms 내 리액션, 2s 내 텍스트 폴백.

---

## 크로스 커팅 분석

### 공통 우려 사항 (여러 페르소나에서 공통)

1. **테스트 범위 축소 위험** (백엔드·QA): 설계서의 "bus 레벨 단위 테스트" 가 6개 outbound 경로 전부를 파라미터라이즈로 덮지 않으면 ACK 만 고쳐지고 파생 경로가 회귀할 수 있다. 각 진입점(`_handle_stop`, `/help`, `/new` 성공/실패, 에러 핸들러, `MessageSender` 툴)에 케이스 1개씩 강제.
2. **Telegram 회귀 공백** (PM·백엔드·QA): "수동 생략 가능" 이 아니라 단위 테스트(AsyncMock bot)로는 반드시 커버. inbound 가 `message_thread_id` 를 metadata 에 싣는지도 이번 기회에 검증.
3. **수용 기준(AC) 부재** (PM·QA): "스레드 안에 뜬다" 가 아니라 사용자 관점 체크리스트가 필요. PM 과 QA 가 동일하게 요구.
4. **Option C 의 즉시 채택 압력** (백엔드·QA): 원 설계는 "후속" 으로 미뤘지만, 두 리뷰어 모두 "이번 PR 에 넣어라" 고 권함. 근거 다름 — 백엔드는 화이트리스트로 role_prompt 유출 방지, QA 는 헬퍼로 리그레션 가드. 둘 다 같은 귀결.

### 의견 충돌

1. **ACK 방식: 텍스트 vs 리액션** (UX vs 나머지). UX 는 👀 리액션 + 3초 폴백 텍스트를 강력 권장. 백엔드/PM/QA 는 "텍스트 유지" 를 전제로 리뷰. 결정 필요.
   - **해소안**: 이 PR 은 **thread leak 수정에만 집중**하고, 리액션 방식은 별도 UX PR 로 분리. 이번에 두 가지를 합치면 스코프 폭발. UX 의 다른 지적(에러 메시지, `/new` ephemeral)도 동일 판단.

2. **`/help`, `/new` 스레드 격리 vs ephemeral** (UX vs 기본 설계). UX 는 멘탈 모델 충돌을 이유로 채널 루트/ephemeral 을 선호. 현 설계는 "멘션 스레드에 일관되게" 를 선호.
   - **해소안**: 이 PR 은 버그 수정(스레드 일관성) 범위로 한정. UX 개선(ephemeral, 에러 메시지 풍부화, 리액션)은 후속 UX 개선 티켓으로 분리. 결정의 기본값은 **현 설계 유지**.

3. **Option C 범위 — 팩토리 + 화이트리스트 일체**(백엔드) vs **내부 헬퍼 1개 추가**(QA):
   - **해소안**: 둘은 충돌이 아니라 범위 차이. 백엔드 제안(`OutboundMessage.reply_to()` + 화이트리스트)은 더 넓지만 타입 레벨에서 강제 가능. QA 제안(`_reply_metadata` 내부 헬퍼)은 더 국소. **백엔드 제안 채택** — 추가 비용 소수십 줄, 효과는 role_prompt 유출 차단이라는 명확한 안전성 이득.

### 컨센서스

- **단일 root cause 판정 정확**: 4명 전원 "metadata 상속 누락" 이 맞다고 인정.
- **Option B 이상 필요**: Option A(최소 수정)는 전원 거부. 파생 경로 누수 때문.
- **이번 PR 스코프**: 버그 수정 + 근본 해결(화이트리스트 기반 상속) + 테스트. UX 개선(리액션·에러 문구·ephemeral) 은 후속 분리.
- **추가 발견 이슈 동반 처리 여부**: `_sent_in_turn` thread 비교, `_progress_messages` race, `split_message` chunk 버그 — 세 건 모두 백엔드 지적. 이 중 **`_sent_in_turn` 과 `_progress_messages` race 는 이번 PR 에 포함**(같은 파일, 같은 계약 이슈). `split_message` chunk 버그는 별도 PR(다른 계약, 리뷰 범위 다름).
- **수용 기준(AC)과 테스트 체크리스트 보강**: 설계서에 AC 섹션 추가, QA 가 제시한 테스트 10개 + 수동 7개를 그대로 채택.
