# Phase 개요: Broadcast 트리거 재설계 (LLM tool → regex)

> 작성일: 2026-04-11
> 대상 리포: /home/holmes/compan.io
> 에이전트: compan-io-agent (단일)

## 1. 배경 및 목적

직전에 머지·푸시·운영 배포된 PR `1e10b8e` (Channel Broadcast)이 운영 검증에서 동작 불가능 판명. AIDP 봇에 "채널 본문에도 브리핑해" 메시지를 보냈으나 broadcast 미발생, shadow detector 로그도 없음.

근본 원인:
- `MessageSender.send(share_to_channel=...)`는 Python 클래스 메서드이지만 Claude는 `claude -p` subprocess라 호출 불가 (cli.py:418의 명시적 주석을 04-plan 페르소나 리뷰에서 누락)
- Shadow detector regex 패턴이 너무 좁아 "채널 본문에도"를 미매칭

해결:
- LLM 도구 호출 경로를 dead code로 전환 (코드는 보존)
- regex 기반 결정적 트리거로 전환 (shadow detector → primary trigger 승격)
- 토큰 매칭 + 명령형 어미 gate + 과거형 제외로 오탐 방어
- 다중 chunk 응답에서 broadcast 비활성화 (단일 chunk 한정)
- 사용자 인지를 위한 마커 추가

## 2. Phase 구조

| Phase | 목표 | 주요 변경 | 대상 에이전트 | 선행 조건 |
|---|---|---|---|---|
| P1 | Regex 트리거 + 안전장치 + 마커 + 회귀 가드 | loop.py(트리거 함수, apply 헬퍼, 흐름), slack.py(단일 chunk 가드), tools/message.py(dead code warning), 단위 테스트 다수 | compan-io-agent | holmes 브랜치 (`00f30e8`) |

단일 Phase. 분할 불필요.

## 3. Phase 간 의존성

없음 (단일 Phase).

## 4. 전체 완료 기준

기능적:
- [ ] `채널에도 공유해`, `채널 본문에도 브리핑해` 등 명령형 발화 → broadcast 발생, 마커 표시, 채널 본문 노출
- [ ] DM에서 동일 발화 → silent skip + INFO 로그
- [ ] `이 채널에 PR 공지가 떴는데 요약해줘` 같은 참조 발화 → broadcast 미발생 (오탐 방어)
- [ ] `/help 채널에도 공유해` → broadcast 미적용 (slash command early return)
- [ ] 다중 chunk 응답 → broadcast 미적용 + INFO 로그

비기능적:
- [ ] pytest 전체 통과 (사전 실패 2건 무시)
- [ ] ruff 통과 (사전 위반 1건 무시)
- [ ] 7개 회귀 가드 모두 GREEN, 특히 `test_loop_py_uses_only_reply_to_inbound`
- [ ] `_FORWARDED_METADATA_KEYS` 변경 없음
- [ ] negative test ≥15 케이스 통과 (오탐 방어 잠금)
- [ ] 04-plan.md §1.1~1.10 모든 결정 코드로 반영
