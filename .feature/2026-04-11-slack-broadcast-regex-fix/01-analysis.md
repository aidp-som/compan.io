# 현황 분석: Broadcast 트리거 재설계 (LLM tool → regex)

> 분석일: 2026-04-11
> 대상: 직전 머지된 broadcast 기능이 아키텍처상 동작 불가능하므로 트리거 메커니즘을 재설계
> 선행 컨텍스트: `.feature/2026-04-11-slack-ack-reaction-channel-broadcast/04-plan.md` (원본 기획)

## 1. 현재 상태 요약

오늘 머지·푸시·배포된 PR 3 (Channel Broadcast)는 **운영 검증에서 동작 불가능**으로 판명. 사용자가 슬랙 AIDP 봇에 "채널 본문에도 브리핑해"를 보냈지만 broadcast가 발생하지 않았고, 나아가 shadow detector조차 트리거되지 않았다.

근본 원인 두 가지:
1. **`MessageSender`는 dead code 였다.** Claude는 `claude -p` subprocess로 실행되며 Python `MessageSender` 클래스에 접근할 수 없다. cli.py:418의 기존 주석이 이를 명시하고 있었으나 04-plan 페르소나 리뷰에서 백엔드 리뷰어가 캐치 실패.
2. **Shadow detector 패턴이 너무 좁았다.** `채널에도`처럼 인접 문자열만 매칭하므로 `채널 본문에도`(중간 단어 삽입) 미매칭.

피처 1 (Reaction ACK), 피처 3 (Progress Pulse)는 정상 동작 — 이번 fix는 피처 2만 대상.

## 2. 관련 코드 맵

| 파일 | 줄 | 역할 | 현재 상태 |
|---|---|---|---|
| `companio/tools/message.py` | 153 | `MessageSender` 클래스, `send(share_to_channel=...)` 시그니처 | **dead code** — Claude가 호출 불가 |
| `companio/cli.py` | 418 | `MessageSender cannot be injected into Claude CLI subprocess` 주석 | 사실 명시, 본 PR 검토 시 누락 |
| `companio/core/loop.py` | 22 | `MessageSender` import | 사용 중 (set_context, start_turn만 호출됨) |
| `companio/core/loop.py` | 32-43 | `_BROADCAST_TRIGGER_PATTERN` + `_detect_broadcast_intent()` | 패턴이 너무 좁음 |
| `companio/core/loop.py` | 227-273 | `_process_message`: shadow detector wraps `_invoke_claude_turn` | 트리거 매칭되면 audit log만 찍음, 실제 broadcast는 아무도 안 시킴 |
| `companio/core/loop.py` | 440-444 | 최종 응답: `OutboundMessage.reply_to_inbound(msg, result_text)` | broadcast metadata 없이 발행됨 |
| `companio/channels/slack.py` | 207~ | `SlackChannel.send()`: `chat_postMessage`에 `reply_broadcast` 전달 | 인프라는 정상 — metadata에 `reply_broadcast=True`만 들어오면 동작함 |
| `companio/config/schema.py` | 47-52 | `broadcast_enabled`, `broadcast_blocked_channels` config 필드 | 정상 |

### Trigger pattern 매칭 검증

| 사용자 입력 | 매칭 여부 | 사유 |
|---|---|---|
| `"채널에도 공유해"` | ✅ | `채널에도` 직매칭 |
| `"채널 본문에도 브리핑해"` | ❌ | `채널에도` 사이에 `본문` 삽입 → 미매칭 |
| `"이 답변 채널에도 올려줘"` | ✅ | `채널에도` 매칭 |
| `"채널에 공지해 줘"` | ✅ | `채널에 공지` 매칭 |
| `"팀에 알려줘"` | ✅ | `팀에 알` 매칭 |
| `"이 채널에 PR 공지가 떴는데"` | ✅ (오탐) | `채널에` + `공지` — 의도 무관 매칭 |

패턴 폭이 부족한 동시에 일부 오탐 위험도 있음.

### 데이터 흐름 (현재)

```
사용자 메시지
  → SlackChannel._on_app_mention
  → InboundMessage publish
  → loop._process_message
      ├─ broadcast_intent_detected = _detect_broadcast_intent(msg.content)  ← 너무 좁음
      ├─ message_sender.set_context(msg)  ← MessageSender bind
      ├─ _invoke_claude_turn (pulse task wrap)
      │   └─ _process_message_inner
      │       └─ Claude CLI subprocess (stdout text 반환)
      │       └─ return OutboundMessage.reply_to_inbound(msg, result_text)
      │             ↑↑↑ broadcast metadata 없음
      └─ finally: shadow detector audit
          if intent_detected and not message_sender._broadcast_called: WARN
            ↑↑↑ _broadcast_called는 항상 False (Claude가 send() 호출 못 함)
```

`MessageSender._broadcast_called = True`로 설정될 일이 없으므로, intent_detected가 True인 경우 항상 shadow_miss가 찍혀야 했다. 그런데 운영 로그엔 그 WARN조차 없었다 → 이번 케이스는 패턴 미매칭 때문.

## 3. 아키텍처 현황

- companio는 Claude를 **subprocess (`claude -p`)** 로 호출. stdin/stdout 텍스트 인터페이스. MCP/tool bridge 없음.
- `--mcp-config` 옵션을 안 씀 (또는 message tool은 MCP 등록 안 됨). Claude가 호출할 수 있는 "tool"은 Claude Code 내장 + 외부 MCP 서버뿐.
- companio 코드 내의 Python 클래스 `MessageSender`는 Claude의 도구 목록에 노출되는 경로 없음.
- 대안적으로 Claude는 별도 Slack MCP를 통해 슬랙 API에 접근 가능 (운영 테스트에서 Claude가 언급함). 단 그 MCP는 사용자 개인 계정으로 동작하며 봇 계정과 분리됨.

## 4. 도메인 컨텍스트

PR#2 (thread isolation) 이후 metadata 흐름 원칙이 확립됨:
- inbound metadata는 화이트리스트(`_FORWARDED_METADATA_KEYS`)를 통해 outbound로 전달
- `reply_broadcast`는 outbound 전용 키 — inbound에서 forward되면 안 됨 (회귀 가드 존재)
- outbound metadata에 `reply_broadcast=True`가 있으면 SlackChannel.send()가 마지막 chunk에 적용

이 원칙 위에서 broadcast를 트리거하는 방법은 두 가지:
1. **LLM 도구 호출** (원래 설계, 불가능)
2. **외부 코드(loop.py 또는 channel adapter)에서 outbound metadata에 직접 주입**

## 5. 관련 이슈 및 히스토리

- **방금 머지**: `1e10b8e feat(slack): channel broadcast option ...` — 이 변경의 LLM 트리거 부분이 본 fix의 대상
- **04-plan §1.6**: "Option A (regex) vs Option B (LLM tool) — B 채택" → 본 fix는 **A로 전환**
- **04-plan §1.6 후속 항목**: "정규식 fallback — 미탐 누적 후 결정" → 미탐을 누적할 새도 없이 LLM 경로 자체가 죽었으므로 즉시 도입
- **운영 로그 (12:17)**: "채널 본문에도" 입력 → broadcast 미발생, shadow_miss 로그도 없음 (패턴 미매칭)

## 6. 제약 사항 및 리스크

### 제약
- `MessageSender.send()`로 가는 경로를 새로 만들지 않는다 (MCP 브리지 구현은 별도 큰 작업)
- `_FORWARDED_METADATA_KEYS`는 절대 변경하지 않는다 (회귀 가드)
- 방금 도입한 `share_to_channel` 파라미터, `_broadcast_called` 추적, 3중 안전장치 코드는 **유지하되 dead code 표시**. 향후 MCP 브리지 도입 시 활용 가능. 또는 정리 PR로 제거.

### 리스크
| 리스크 | 가능성 | 영향 |
|---|---|---|
| 패턴 오탐 — 사용자가 의도하지 않은 broadcast | 중 | 중 (채널 노이즈, 정보 노출) |
| 패턴 미탐 — 의도했는데 안 됨 | 중 | 저 (사용자 재요청) |
| 패턴 broaden 후 ACL 우회 | 저 | 저 (안전장치 3개 그대로) |
| 사용자 인지 부재 — broadcast됐는지 모름 | 높 | 저 |

### 신규 안전장치 요구
LLM 판단이 사라지면 사용자 의도와 100% 일치 보장 불가. 다음이 추가 필요:
- broadcast 발생 시 **최종 응답 텍스트에 명시적 표시** (예: "_(채널 본문에도 공유됨)_")
- 운영 로그에 "regex hit + auto broadcast" 이벤트 기록 (감사 추적)

## 7. 분석 소견

1. **04-plan 페르소나 리뷰의 결정적 누락**: 백엔드 리뷰어가 5명 중 가장 깊게 코드를 봤음에도 `cli.py:418`의 명시적 주석을 놓침. 페르소나 리뷰가 만능이 아니며, 특히 "외부 subprocess와의 IPC 경계"를 검증하는 단계가 별도로 필요함을 시사.
2. **Shadow detector는 patch가 아니라 redesign이 필요한 부분이었음**: 원래 의도는 "LLM 미탐 감지"였으나 LLM 자체가 트리거를 못 하므로 detector가 곧 primary trigger가 되어야 함.
3. **`share_to_channel` 도구 시그니처는 보존 가치 있음**: 미래 MCP 브리지나 별도 LLM 호출 경로(예: 더 작은 LLM의 분류기)에서 활용 가능. dead code로 두되 docstring에 "현재 dead code; MCP 브리지 미구현으로 호출 경로 없음" 명시.
4. **Claude 본문 파싱 한계**: "자기소개 채널 본문에도"를 Claude가 "자기소개 채널" + "본문에도"로 잘못 파싱함. 이는 regex로 우회하면 자연 해결됨 — Claude 의도 해석이 필요 없어지므로.
5. **사용자 가시성**: regex 트리거는 결정적이지만 사용자 입장에서 "내가 한 발화로 이게 broadcast 되는구나"를 학습할 단서가 필요. 응답에 명시적 마커 추가가 필수.
6. **검증 가능성 향상**: regex는 LLM과 달리 단위 테스트로 100% 결정적 검증 가능. 패턴 변경 시 회귀 보호 견고함.
