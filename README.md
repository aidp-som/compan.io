# companio

Claude Code CLI 기반 개인 AI 어시스턴트.

Telegram 등 채팅 채널에서 메시지를 받아 Claude Code(`claude -p`)를 서브프로세스로 호출하는 경량 게이트웨이입니다.
파일 읽기/쓰기, 웹 검색, 코드 실행 등 모든 도구는 Claude Code가 자체적으로 처리합니다.
companio는 메시지 라우팅, 세션 관리, 메모리, 크론 스케줄링만 담당합니다.

> **macOS / Linux / Windows(WSL2)** — Claude Code CLI가 macOS와 Linux를 지원합니다. Windows는 WSL2 또는 Docker Desktop을 통해 사용할 수 있습니다.

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| **Claude Code 위임** | 모든 LLM 작업을 `claude -p` 서브프로세스에 위임 |
| **세션 유지** | `--session-id` / `--resume`으로 대화 컨텍스트 유지, 토큰 비용 절감 |
| **Telegram 연동** | Long polling 봇을 통한 채팅 (사진, 문서, 음성 첨부 지원) |
| **2계층 메모리** | MEMORY.md (장기 기억) + HISTORY.md (검색 가능한 이벤트 로그) |
| **세션 관리** | SQLite 기반 대화 영속화 + 턴별 비용/토큰 추적 |
| **크론 스케줄러** | 예약 리마인더, 반복 작업, 일회성 트리거 |
| **보안** | 환경변수 시크릿 필터링 + 출력 시크릿 마스킹 |

---

## 요구사항

- macOS 또는 Linux (Windows는 WSL2 / Docker Desktop 사용)
- Python 3.11 이상
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 설치 및 인증 완료

---

## 설치

### 비개발자용 (간편 설치)

터미널(Terminal.app)을 열고 아래 명령어를 순서대로 입력하세요.

**1. Python 확인**

```bash
python3 --version
```

`3.11` 이상이면 다음 단계로. 없으면 [python.org](https://www.python.org/downloads/)에서 설치하세요.

**2. Claude Code CLI 확인**

```bash
claude --version
```

버전이 출력되면 인증 완료 상태입니다. 없으면 [Claude Code 문서](https://docs.anthropic.com/en/docs/claude-code)를 참고하세요.

**3. companio 설치**

```bash
git clone https://github.com/yonggill/compan.io.git
cd compan.io
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**4. 초기 설정**

```bash
companio onboard
```

대화형 마법사가 설정 파일(`~/.companio/config.json`)과 워크스페이스를 생성합니다.
Telegram 봇을 사용하려면 이 단계에서 봇 토큰을 입력하세요.

**5. 동작 확인**

```bash
# 단일 메시지 테스트
companio agent -m "안녕하세요!"

# 대화형 모드
companio agent

# Telegram 봇 + 크론 실행
companio gateway
```

### 개발자용

```bash
git clone https://github.com/yonggill/compan.io.git
cd compan.io
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

companio onboard

# 테스트 & 린트
pytest
ruff check .
mypy companio
```

### Windows에서 사용하기

Windows는 Claude Code CLI를 직접 지원하지 않으므로 아래 두 가지 방법 중 하나를 사용하세요.

#### 방법 1: WSL2 (권장)

WSL2는 Windows에서 Linux 환경을 네이티브로 실행합니다.

**WSL2 설치 (PowerShell 관리자 권한):**

```powershell
wsl --install
```

재부팅 후 Ubuntu 터미널을 열고:

```bash
# WSL2 홈 디렉토리에 클론 (Windows 파일시스템 /mnt/c/ 사용 시 성능 저하)
git clone https://github.com/yonggill/compan.io.git ~/compan.io
cd ~/compan.io

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

companio onboard
```

#### 방법 2: Docker Desktop

```bash
# 프로젝트 루트에서
docker run -it --rm \
  -v ~/.companio:/root/.companio \
  -e ANTHROPIC_API_KEY=your_key \
  python:3.11 bash

# 컨테이너 내부에서
pip install -e .
companio gateway
```

---

## Telegram 봇 설정

1. Telegram에서 [@BotFather](https://t.me/BotFather)에게 `/newbot` 명령으로 봇 생성
2. 발급받은 토큰을 `~/.companio/config.json`에 입력:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
      "allowFrom": ["your_telegram_username"]
    }
  }
}
```

3. `companio gateway` 실행 후 봇에게 메시지 전송

`allowFrom`이 비어있으면 모든 사용자가 차단됩니다. 허용할 Telegram 유저네임 또는 숫자 ID를 입력하세요.

### 그룹 채팅에서 사용하기

1. **BotFather에서 Group Privacy 끄기**: BotFather에게 `/mybots` → 봇 선택 → Bot Settings → Group Privacy → **Turn off**
2. **그룹에 봇 초대**: Telegram 그룹 설정에서 봇을 멤버로 추가
3. **`@봇이름`으로 멘션하거나 봇 메시지에 reply**하면 응답합니다

> **참고**: Group Privacy 설정을 변경한 후에는 봇을 그룹에서 **제거했다가 다시 초대**해야 적용됩니다.

그룹 채팅에서는 `allowFrom` 제한이 적용되지 않으며, 멘션 또는 reply로 봇을 호출한 모든 사용자에게 응답합니다. DM에서는 기존대로 `allowFrom` 허용 목록이 적용됩니다.

---

## 설정

설정 파일: `~/.companio/config.json`

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.companio/workspace",
      "memoryWindow": 200
    }
  },
  "claude": {
    "maxTurns": 50,
    "timeout": 300,
    "maxConcurrent": 5,
    "model": null
  },
  "channels": {
    "sendProgress": true,
    "telegram": {
      "enabled": false,
      "token": "",
      "allowFrom": [],
      "proxy": null,
      "replyToMessage": false
    }
  },
  "gateway": {
    "host": "0.0.0.0",
    "port": 18790
  }
}
```

### 설정 항목

| 섹션 | 항목 | 설명 | 기본값 |
|------|------|------|--------|
| `claude` | `maxTurns` | 요청당 최대 agentic 턴 수 | `50` |
| | `timeout` | 요청 타임아웃 (초) | `300` |
| | `maxConcurrent` | 최대 동시 세션 수 | `5` |
| | `model` | 모델 오버라이드 (`null` = CLI 기본값) | `null` |
| `channels` | `sendProgress` | "생각 중..." 진행 알림 전송 | `true` |
| | `telegram.enabled` | Telegram 봇 활성화 | `false` |
| | `telegram.token` | 봇 토큰 (@BotFather 발급) | — |
| | `telegram.allowFrom` | 허용된 사용자 목록 | `[]` |
| | `telegram.proxy` | HTTP/SOCKS5 프록시 URL | — |
| | `telegram.replyToMessage` | 원본 메시지 인용 답장 | `false` |
| `gateway` | `host` | 바인드 호스트 | `0.0.0.0` |
| | `port` | 게이트웨이 포트 | `18790` |
| `agents` | `memoryWindow` | 통합 전 유지할 메시지 수 | `200` |

환경 변수로도 오버라이드 가능합니다 (`COMPANIO_` 접두사, `__` 구분자):

```bash
export COMPANIO_CLAUDE__TIMEOUT=600
export COMPANIO_CHANNELS__TELEGRAM__TOKEN="123456:ABC..."
```

---

## CLI 명령어

```
companio onboard          # 초기 설정 마법사
companio agent -m "..."   # 단일 메시지
companio agent            # 대화형 모드
companio gateway          # 게이트웨이 (Telegram + 크론)
companio status           # 상태 확인
companio channels status  # 채널 상태
```

### 채팅 명령어

| 명령어 | 설명 |
|--------|------|
| `/new` | 새 세션 시작 (메모리 통합 후 초기화) |
| `/stop` | 진행 중인 작업 취소 |
| `/help` | 도움말 |

---

## 아키텍처

```
사용자 메시지 (Telegram / CLI)
    ↓
MessageBus (inbound)
    ↓
AgentLoop
    ├─ 세션 조회/생성 (SQLite)
    ├─ CLAUDE.md 생성 (시스템 프롬프트 + 메모리 + 런타임 컨텍스트)
    ├─ claude -p --output-format json 서브프로세스 호출
    │   ├─ 첫 호출: --session-id <UUID>
    │   └─ 이후: --resume <SESSION_ID>
    ├─ JSON 응답 파싱 + 시크릿 필터링
    └─ 세션 저장 + 턴별 비용 기록
    ↓
MessageBus (outbound)
    ↓
채널 (Telegram / CLI)
```

### Claude CLI 호출 방식

companio는 Claude Code를 서브프로세스로 호출합니다:

```bash
# 새 세션
claude -p --output-format json --max-turns 50 --add-dir ~/ \
  --session-id <UUID> --dangerously-skip-permissions

# 기존 세션 이어가기
claude -p --output-format json --max-turns 50 --add-dir ~/ \
  --resume <SESSION_ID> --dangerously-skip-permissions
```

- 사용자 메시지는 **stdin**으로 전달 (ARG_MAX 제한 회피)
- 시스템 프롬프트는 프로젝트 디렉토리(`~/.companio/project/`)의 `CLAUDE.md` 파일로 주입
- 환경변수에서 API 키, 시크릿, Claude 내부 변수를 제거한 뒤 서브프로세스 생성

### 2계층 메모리

| 계층 | 파일 | 컨텍스트 포함 | 용도 |
|------|------|:---:|------|
| 장기 메모리 | `memory/MEMORY.md` | ✅ | 핵심 사실, 사용자 선호 |
| 이벤트 로그 | `memory/HISTORY.md` | ❌ | grep 검색 가능한 대화 요약 |

세션 메시지가 `memoryWindow`를 초과하면 자동 통합(consolidation)이 실행되어 오래된 대화를 MEMORY.md에 요약 저장하고 HISTORY.md에 이벤트를 기록합니다.

### 크론 스케줄러

에이전트가 대화 중 직접 크론 작업을 생성할 수 있습니다:

- **반복** — `every_seconds: 3600` (매 1시간)
- **크론식** — `cron_expr: "0 9 * * *"` (매일 오전 9시)
- **일회성** — `at: "2024-12-25T09:00:00"` (실행 후 자동 삭제)

작업 목록은 `~/.companio/cron/jobs.json`에 영속 저장됩니다.

### 데이터 디렉토리

```
~/.companio/
├── config.json              # 설정 파일
├── .env                     # 환경 변수 (선택)
├── project/                 # Claude CLI 프로젝트 디렉토리
│   └── CLAUDE.md            # 시스템 프롬프트 (매 요청마다 재생성)
├── workspace/               # 에이전트 워크스페이스
│   ├── AGENTS.md            # 에이전트 지침
│   ├── SOUL.md              # 성격 정의
│   ├── USER.md              # 사용자 프로필
│   ├── TOOLS.md             # 도구 가이드
│   ├── memory/              # 메모리 파일
│   ├── skills/              # 스킬 확장 (마크다운)
│   └── companio.db        # SQLite 세션 DB
└── cron/
    └── jobs.json            # 크론 작업 목록
```

### 프로젝트 구조

```
compan.io/
├── companio/
│   ├── core/              # AgentLoop, ClaudeCLI, ContextBuilder, MemoryStore
│   ├── channels/          # 채팅 채널 (Telegram)
│   ├── config/            # 설정 스키마, 로더, 경로
│   ├── tools/             # companio 전용 도구 (message, cron)
│   ├── templates/         # 워크스페이스 템플릿
│   ├── bus.py             # 비동기 메시지 버스
│   ├── cli.py             # CLI 명령어 (typer)
│   ├── cron.py            # 크론 스케줄러
│   ├── session.py         # SQLite 세션 관리
│   └── helpers.py         # 유틸리티
├── tests/
└── pyproject.toml
```

---

## 라이선스

MIT

## Acknowledgments

이 프로젝트는 [nanobot](https://github.com/HKUDS/nanobot) (HKUDS)에서 영감을 받아 개발되었습니다.
