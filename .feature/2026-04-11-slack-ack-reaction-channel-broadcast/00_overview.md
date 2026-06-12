# Phase 개요: Slack ACK 리액션 + Progress Pulse + Channel Broadcast

> 작성일: 2026-04-11
> 대상 리포: /home/holmes/compan.io
> 에이전트: compan-io-agent (단일 리포, 순차 실행)
> 기획 산출물: 같은 폴더의 01~04-plan.md

## 1. 배경 및 목적

PR#2(Slack thread isolation) 머지 이후 사용자가 추가로 요청한 두 개의 UX 개선 + 이터레이션으로 추가된 한 개의 waiting experience 개선을 묶은 작업이다. 04-plan.md에서 결정된 대로 3개의 독립 PR로 분리하여 리스크를 격리한다.

핵심 문제와 해결:
- **Pain 1**: 멘션 ACK가 텍스트만으로 전달되어 이전 요청 lock 대기 시 사용자가 봇이 죽었는지 인지 못함 → **Feature 1: 즉시 리액션 ACK** (lock 밖 발송)
- **Pain 2**: 처리 시간이 길어질 때 정적인 "생각 중..." 메시지가 죽은 듯 보임 → **Feature 3: Progress pulse** (5초마다 경과시간 표시)
- **Need**: 응답을 채널 본문에도 공지하고 싶을 때 자연어로 트리거 → **Feature 2: Broadcast** (LLM 도구 옵션)

## 2. Phase 구조

| Phase | 목표 | 주요 변경 | 대상 에이전트 | 선행 조건 |
|---|---|---|---|---|
| P1 | Feature 1 — Reaction ACK | slack.py(`_add_reaction`/`_remove_reaction`/lifecycle), loop.py(`_dispatch` try/finally), schema.py(flag), 신규 테스트 | compan-io-agent | holmes 브랜치 (`904e2bc`) |
| P2 | Feature 3 — Progress Pulse | loop.py(pulse loop, cancel), schema.py(flag), 신규 테스트 | compan-io-agent | holmes 브랜치 (P1 머지 무관, 독립) |
| P3 | Feature 2 — Channel Broadcast | message.py(`share_to_channel`), slack.py(`reply_broadcast`), loop.py(shadow detector), schema.py(flag/blocked), 신규 테스트 | compan-io-agent | holmes 브랜치 (독립) |

## 3. Phase 간 의존성

- **3개 모두 `holmes` 브랜치에서 독립 분기**. 머지 순서/의존 없음.
- 워크트리 미사용 → control-agent가 **순차 디스패치**: P1 → P2 → P3
- 각 worker-agent는 자기 브랜치에 커밋하지 않음. 코드 변경만 수행, 커밋은 control-agent + 사용자 승인으로.

## 4. 전체 완료 기준

기능적:
- [ ] PR 1: Slack 멘션 시 사용자 메시지에 `eyes` 리액션 즉시 등장 (lock 밖 경로)
- [ ] PR 1: 응답 완료/에러/취소 시 `eyes` 제거
- [ ] PR 1: `ack_reactions_enabled=False` 인스턴스는 미발송
- [ ] PR 1: scope 누락 시 런타임 자동 비활성화 + ERROR 로그
- [ ] PR 2: 5초 간격으로 4번까지 `"생각 중... (약 N초)"` 메시지 update
- [ ] PR 2: 응답 도착 시 pulse task 즉시 cancel
- [ ] PR 2: `progress_pulse_enabled=False` 인스턴스는 미발송
- [ ] PR 3: `MessageSender.send(share_to_channel=True)`가 metadata에 `reply_broadcast` 세팅
- [ ] PR 3: 3중 안전장치(is_channel + thread_ts + flag) 작동
- [ ] PR 3: 마지막 chunk에만 `reply_broadcast=True` 전달
- [ ] PR 3: Shadow detector — 트리거 발화 후 도구 미호출 시 WARN 로그

비기능적:
- [ ] 모든 PR: pytest 통과 (기존 + 신규 테스트)
- [ ] 모든 PR: ruff check 통과
- [ ] 모든 PR: `_FORWARDED_METADATA_KEYS`가 변하지 않음 (회귀 가드)
- [ ] 모든 PR: 04-plan.md의 결정 사항(1.1~1.14a) 준수
