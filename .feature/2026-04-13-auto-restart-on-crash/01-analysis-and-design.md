# 분석 + 설계 (condensed): 봇 crash 후 자동 재시작

> 작성일: 2026-04-13
> 트리거: 2026-04-12 08:40 Slack WebSocket 5회 연속 재연결 실패 → sys.exit(1) → 24시간 무인 다운타임
> 환경: SOM_SERVER_2 (Windows 11, Git Bash + tmux, Node v24.14.1, npm 11.11.0, PM2 미설치)

## 현재 상태

- companio 는 unrecoverable Slack 에러 시 `sys.exit(1)` 로 **의도적으로 죽음** (`slack.py:253`)
- 설계 가정: systemd (Linux) 또는 PM2 (macOS) 같은 process manager 가 자동 재시작
- 이 머신: tmux 만 있고 process manager 없음 → 죽으면 수동 재기동 때까지 down
- companio-init skill (`repos/compan.io/.claude/skills/companio-init/SKILL.md`):
  - Linux: systemd user service 자동 등록
  - macOS: PM2 사용
  - **Windows: 미지원** ("이 머신은 수동 세팅" — CLAUDE.md 명시)

## 5개 옵션 비교 (이 머신 기준)

| # | 방법 | crash 재시작 | reboot 재시작 | 설치 난이도 | 새 의존성 | 로그 관리 | 비고 |
|---|---|---|---|---|---|---|---|
| **1** | bash while-loop wrapper | ✅ | ❌ (tmux 자체가 안 뜸) | 매우 쉬움 (2분) | 0 | ❌ (tmux 버퍼만) | 즉시 가능. tmux 안에서만 동작 |
| **2** | Windows Task Scheduler | ✅ (poll 방식) | ✅ (On Startup 트리거) | 중간 (schtasks CLI) | 0 | ❌ | poll 간격만큼 지연 (30s~1m). 실행 계정 주의 |
| **3** | nssm / WinSW (Windows Service) | ✅ (즉시) | ✅ (서비스 auto-start) | 중간~높음 | 외부 바이너리 | stdout → 파일 | UAC elevated 필요, 서비스 계정 제약 |
| **4** | **PM2** | ✅ (즉시) | ✅ (`pm2 save` + startup) | **쉬움 (5분)** | npm (이미 있음) | ✅ (`pm2 logs`) | companio-init 이 macOS 용으로 이미 사용 중. Windows 도 동작 |
| **5** | companio 자체 retry loop | ✅ | ❌ | 중간 (코드 변경) | 0 | 기존 loguru | 설계 철학 변경. 메모리 누수/partial cleanup 위험 |

## 권장: 옵션 4 (PM2)

**이유:**
1. **npm/node 이미 설치됨** (v24 + npm 11) — `npm install -g pm2` 한 줄이면 끝
2. **companio-init skill 이 macOS 에서 이미 PM2 사용** — 팀 내 패턴 일관성
3. **crash restart + reboot restart + log management** 세 가지를 한 번에 해결
4. **tmux 불필요해짐** — PM2 가 process lifecycle 을 관장. `pm2 logs som-agent` 로 로그 확인
5. **Windows 에서도 동작** — `pm2-windows-startup` 또는 `pm2-installer` 로 boot-time 자동 시작 가능

### PM2 설치 + 설정 단계

```bash
# 1. PM2 설치
npm install -g pm2

# 2. 봇 등록 (tmux 에서 기존 봇 먼저 종료)
tmux send-keys -t som-agent C-c
sleep 2
pm2 start companio -- gateway --config C:/Users/som_server_2/.companio/config.json
pm2 save  # 프로세스 목록 persist

# 3. Windows boot-time 자동 시작 (pm2-windows-startup 사용)
npm install -g pm2-windows-startup
pm2-startup install
# → Windows 서비스 "PM2" 가 등록됨. 재부팅 시 pm2 resurrect 자동 실행

# 4. 상태 확인
pm2 status
pm2 logs som-agent --lines 50

# 5. (선택) tmux 정리 — PM2 가 대체하므로 tmux 세션 불필요
tmux kill-session -t som-agent
```

### PM2 ecosystem 파일 (선택 — 더 세밀한 설정)

```javascript
// ecosystem.config.js (인스턴스 루트에 저장)
module.exports = {
  apps: [{
    name: "som-agent",
    script: "companio",
    args: "gateway --config C:/Users/som_server_2/.companio/config.json",
    interpreter: "none",    // companio 는 자체 실행 파일
    cwd: "C:/Users/som_server_2/Documents/companio-som-erp",
    restart_delay: 5000,    // crash 후 5초 대기 후 재시작
    max_restarts: 50,       // 50회 연속 실패 시 멈춤 (무한 loop 방지)
    min_uptime: 10000,      // 10초 이상 살아야 "정상 시작" 으로 인정
    autorestart: true,
    watch: false,
    log_date_format: "YYYY-MM-DD HH:mm:ss Z",
  }]
};
```

```bash
pm2 start ecosystem.config.js
pm2 save
```

### CLAUDE.md 업데이트 포인트

기존 "tmux 방식" 섹션을 PM2 로 교체. 핵심 명령어:

| 작업 | tmux (기존) | PM2 (새) |
|---|---|---|
| 상태 확인 | `tmux capture-pane -t som-agent -p -S -100` | `pm2 status` |
| 로그 보기 | `tmux attach -t som-agent` | `pm2 logs som-agent` |
| 재시작 | `tmux send-keys C-c` + 재기동 | `pm2 restart som-agent` |
| 종료 | `tmux kill-session -t som-agent` | `pm2 stop som-agent` |

## 스코프 외

- **companio 코드 변경 (옵션 5)**: 설계 철학 변경이라 별도 PR 급. crash-on-unrecoverable 은 process manager 위임이 운영 사업자 측 책임이라는 원래 의도가 맞음
- **다중 인스턴스 PM2 관리**: PM2 의 ecosystem.config.js 에 여러 앱을 나열하면 자연스럽게 지원됨. 지금은 단일 인스턴스만
- **PM2 monitoring (pm2 plus)**: 유료. 지금은 `pm2 logs` + `pm2 monit` 로 충분
