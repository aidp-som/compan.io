# 피처 플랜 (condensed): broadcast regex recall + LLM-tool docs 정직성

> 작성일: 2026-04-11
> 형태: condensed (분석/설계/리뷰는 별 ceremony 없이 사용자와의 실시간 대화에서 진행. 이 한 파일이 4-단계 사이클의 압축 결과)
> 트리거: 어제 머지된 `bb6c4d0 fix(slack): broadcast trigger via regex on user input` 의 regex 가 사용자의 자연스러운 표현 *"채널 본문 공지하고"* 와 *"공지해 줘"* 를 못 잡음. 또 LLM 이 옛 `TOOLS.md` 의 *"message tool / cron tool"* 설명을 보고 *"이 Claude Code 환경에서는 message 도구가 없어... Gateway 재시작 필요"* 라는 환각 응답 생성.

## 1. 발견된 두 결함

### 결함 A — `_IMPERATIVE_ENDING` regex 가 자연스러운 한국어 변형을 못 잡음

`companio/core/loop.py:43-47` 의 정규식:

```python
_IMPERATIVE_ENDING = re.compile(
    r"(해|해줘|해주세요|드려|드릴게|알려|알려줘|올려|올려줘|"
    r"공유해|공지해|브리핑해|전달해|보내|보내줘|보내주세요)"
    r"\s*[?!.~ㅋㅎ]*\s*$"
)
```

- **공백 분리 미허용**: *"해 줘"* (한국어 띄어쓰기 관행 그대로) 가 alternation 의 literal `해줘` 에 안 잡힘
- **`~하고` connector form 미포함**: *"공지하고"*, *"공유하고"* 같이 chained imperative 의 끝맺음 형태가 alternation 에 없음

### 결함 B — `_BROADCAST_CHANNEL_TOKENS` 가 *"채널"* 리터럴 단어만 인정

`companio/core/loop.py:32`:

```python
_BROADCAST_CHANNEL_TOKENS = ("채널", "channel")
```

- *"본문에 공지해줘"* 같이 사용자가 *"본문"* 으로 채널 root 를 가리켜도 has_channel=False 로 차단
- 사용자 표현 다양성 vs false positive 방지의 트레이드오프인데 *"본문"* 은 false positive 거의 없음 (Slack 맥락에서 *"본문"* 은 thread 가 아닌 채널 root 를 가리키는 거의 유일한 표현)

### 결함 C — `TOOLS.md` 와 `AGENTS.md` 가 LLM 이 호출 못 하는 도구를 광고

`companio/templates/TOOLS.md` 의 *"companio-Specific Tools"* 섹션은 *"message"* 와 *"cron"* 을 *"companio 가 주입하는 두 개의 도구"* 로 문서화. 하지만 분석 단계(PR #3) 에서 발견했듯 **이들은 Claude CLI subprocess 에서 호출 불가**:
- `companio/cli.py:418-420` 의 코멘트: *"NOTE: MessageSender cannot be injected into Claude CLI subprocess. _sent_in_turn is always False."*
- `companio/tools/cron_tool.py` 의 `CronManager` 는 인스턴스화 안 됨 (dead code)
- `share_to_channel` 도 LLM tool 이 아니라 regex shadow detection (`bb6c4d0`)

LLM 이 이 도구들을 호출하려고 시도 → 못 함 → 환각으로 *"이 Claude Code 환경에는..."* 같은 confabulation 생성. 사용자에게 부적절한 안내문이 노출되고 운영자가 디버깅 비용을 부담.

## 2. 결정 사항

| # | 결정 | 근거 |
|---|---|---|
| **D1** | `_IMPERATIVE_ENDING` 재구성 — 보조용언 *"줘/주세요"* 앞에 optional space 허용. `(해|드려|...)(?:\s*(?:줘|주세요))?` 형태 | 결함 A 의 한국어 띄어쓰기 관행 |
| **D2** | `_IMPERATIVE_ENDING` 에 별도 alternation 추가 — `(?:공지|공유|브리핑|전달|방송|안내|업데이트)하고` (특정 하다-verb stem 의 connector form). 임의 `~하고` 는 미포함 (false positive 방지) | 결함 A 의 connector form |
| **D3** | `_BROADCAST_CHANNEL_TOKENS = ("채널", "channel", "본문")` — *"본문"* 추가 | 결함 B |
| **D4** | `templates/TOOLS.md` 의 *"companio-Specific Tools"* 섹션을 정직하게 재작성. *"message", "cron", "share_to_channel" 은 Claude CLI 의 LLM-callable 도구가 아니다. 모든 채널 동작은 사용자 자연어 입력을 사후 분석해서 트리거된다"* 명시 | 결함 C |
| **D5** | `templates/AGENTS.md` 의 안내도 정정 — broadcast/cron/message 가 LLM-driven 이 아니라 사용자 trigger 임을 명시. 대신 *"~을 채널에도 공지해줘" 같이 자연어로 사용자가 명시 요청하면 봇이 자동 공유"* 안내 | 결함 C |
| **D6** | `workspace/TOOLS.md` 와 `workspace/AGENTS.md` (라이브 SOM 사본) 도 동기 — 봇 다음 재기동 시 즉시 적용 | 라이브 환경 |
| **D7** | `tests/test_broadcast_intent.py` 의 POSITIVE 케이스에 새 패턴 4~6개 추가, NEGATIVE 케이스에 false positive 가드 3~4개 추가 | 회귀 가드 |
| **D8** | source-level guard 신설 — `templates/TOOLS.md` 가 *"companio-Specific Tools"* 같은 이름으로 LLM-callable 도구를 광고하는 옛 텍스트가 다시 들어오면 테스트가 깨지도록 | LLM 환각 재발 방지 |

## 3. 변경 파일

| 파일 | 변경 | 라인 추정 |
|---|---|---|
| `companio/core/loop.py` | `_BROADCAST_CHANNEL_TOKENS` +1, `_IMPERATIVE_ENDING` 재구성 | +12 / -4 |
| `companio/templates/TOOLS.md` | *"companio-Specific Tools"* 섹션 재작성 | +20 / -15 |
| `companio/templates/AGENTS.md` | broadcast/cron/message 안내 정정 | +6 / -2 |
| `workspace/TOOLS.md` | 동기 (PR 외, 라이브 SOM) | 동일 |
| `workspace/AGENTS.md` | 동기 (PR 외) | 동일 |
| `tests/test_broadcast_intent.py` | POSITIVE +6, NEGATIVE +4, source guard +1 | +20 |

총 PR 라인: +58 / -21 추정.

## 4. 검증

### 단위 테스트 케이스 (확장)

POSITIVE 추가:
- `"기능 추가했으니까 스레드 요약하고 채널 본문 공지하고"` (원 사용자 보고)
- `"채널에 공지해 줘"` (보조용언 공백)
- `"본문에 공지해줘"` (본문 token)
- `"본문에 올려 주세요"` (본문 + 공백 + 주세요)
- `"채널에 공유하고"` (공유하고 connector)
- `"본문에 안내해 주세요"` (안내 동사 + 본문 + 공백)

NEGATIVE 추가:
- `"운동을 좋아하고"` (특정 stem 아닌 임의 하고)
- `"그래서 채널에 공지하고 끝낼게"` (말끝이 끝낼게 → 매칭 안 됨)
- `"그가 채널에 공지하고 떠났다"` (REFERENTIAL_HINT 의 *"떠났"* catches)
- `"산책하고 와"` (특정 stem 아님)

### 수동 확인

봇 재기동 후 사용자가 SOM Slack 에서 직접:
- `@SOM Agent 채널 본문에 공지해줘 + 답변` → broadcast intent 발화 → 응답이 채널에도 공유됨
- `@SOM Agent 정상 응답해줘 (channel 단어 없이)` → broadcast 안 됨 (false positive 0 검증)
- 첫 시도가 안 되면 thread session 이 옛 환각으로 오염돼 있을 수 있어 `@SOM Agent /new` 후 재시도

## 5. 스코프 외 (별도 PR / 후속)

- **옵션 B (in-process MCP server)** — 진짜 LLM-callable 도구 인프라. 본 PR 은 *"옵션 A (regex + docs)"* 라는 사용자 결정. 후속 ticket 후보로 보존.
- **broadcast 의 multi-clause 의도 인식** — *"X 하고 Y 하고 Z 해줘"* 같이 마지막 절이 broadcast 가 아닐 때 처리. 정규식만으로 어렵고 토큰화가 필요. recall 한계 명시 후 nice-to-have.
- **`CronManager` dead code 정리** — 분석 중 발견. 별 건.
- **`split_message` chunk 마지막 ts 오버라이드 버그** — PR #2 의 후속.

## 6. 리스크

| 리스크 | 가능성 | 영향 | 대응 |
|---|---|---|---|
| 새 `~하고` alternation 이 narrative false positive 야기 | 낮음 | 낮음 | NEGATIVE 케이스 4개 추가, `_REFERENTIAL_HINT` 가 과거형 catches |
| *"본문"* 채널 토큰이 다른 의미로 쓰이는 케이스 | 매우 낮음 | 낮음 | Slack 맥락에서 *"본문"* 의 거의 유일한 의미가 채널 root |
| docs 정정이 LLM 의 다른 정상 응답을 깸 | 매우 낮음 | 낮음 | TOOLS.md/AGENTS.md 의 broadcast/message 관련 문장만 교체, 다른 영역 보존 |
| source-level guard 가 너무 strict 해서 정상 변경 차단 | 낮음 | 낮음 | 명시적인 *"companio-Specific Tools"* + *"share_to_channel"* 같은 deprecated 어휘만 차단 |
| 어제 PR #3 의 source guard `TestNoBareOutboundInLoop` 와 충돌 | 매우 낮음 | 낮음 | 본 PR 은 outbound 생성 안 함 |
