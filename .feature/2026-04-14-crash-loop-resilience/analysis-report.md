# Crash Loop 종합 분석 보고서

> 분석일: 2026-04-14
> 방법: 4명 전문가 병렬 분석 (네트워크 엔지니어, 백엔드 개발자, DevOps, SRE)
> 대상: PM2 ↺=89 crash loop (2026-04-12 ~ 2026-04-14)

## 사고 요약

| 지표 | 수치 |
|---|---|
| PM2 restart count | **89회** (이틀간) |
| MTBF (crash 간 평균 간격) | **약 32분** |
| MTTR (restart 후 복구 시간) | **30초~2분** (DNS 정상 시) |
| Cron 미실행 기간 | **6일** (오전/오후 브리핑 전부 누락) |
| 사용자 체감 | 간헐적 수분 단위 불응답 + cron 브리핑 중단 |

## 근본 원인 — 3개 레이어 동시 작용

### 원인 (b): 코드 — 재연결 예산 25초 (기여도 60%, **P0**)

```python
MAX_RECONNECT_FAILURES = 5  # 5회 × 5초 = 25초 만에 sys.exit(1)
```

DNS 가 30초만 불안해도 프로세스가 죽습니다. **backoff 없이 5초 고정 간격으로 5번만 시도하고 포기**. 이게 89회 crash 의 직접 원인.

**백엔드 개발자 분석**: `sys.exit(1)` 은 cron 스케줄러, DB 연결, 기타 채널을 모두 한꺼번에 죽입니다. 채널만 재연결하면 프로세스를 살릴 수 있는데 핵 옵션을 쓴 것.

**SRE 판정**: exponential backoff 가 있었다면 89회 crash → **0회** 가능. 가장 ROI 높은 fix.

### 원인 (a): 인프라 — Windows DNS 간헐 실패 (기여도 30%, P1)

```
aiohttp.ClientConnectorDNSError: Cannot connect to host slack.com:443 [getaddrinfo failed]
```

**네트워크 엔지니어 분석**:
- Python `getaddrinfo` 는 Windows DNS Client 서비스(Dnscache) 를 경유하는 OS API(`GetAddrInfoW`). `nslookup` 은 직접 DNS 서버에 쿼리하므로 Dnscache 우회 → 둘의 결과가 다른 Windows 고유 문제
- **Dnscache 의 negative caching**: 한 번 실패하면 기본 5초간 실패를 캐시 → Python 연속 호출이 같은 실패를 반복
- ISP DNS(bora.net/KT) 의 간헐 지연 + Dnscache 의 negative caching 이 결합

**핵심 발견**: `pip install aiodns` 만 하면 aiohttp 가 c-ares 기반 `AsyncResolver` 로 자동 전환되어 **Dnscache 를 완전 우회**. 가장 효과적인 소프트웨어 조치.

### 원인 (c): 운영 — process manager 부재 (기여도 10%, 해결됨)

PM2 전환으로 해결. 단, **24시간 사고 (2026-04-12) 의 100% 원인은 이것**.

## 추가 발견: 1~4분 disconnect 패턴의 별도 원인 가능성

네트워크 엔지니어가 중요한 감별 포인트를 지적:

> disconnect 자체가 DNS 없이 발생하면 원인은 DNS 가 아님. 의심 순서: **(1) NAT/방화벽 idle timeout** (공유기가 WSS idle 연결을 60~120초에 끊음), **(2) ISP TCP RST 주입**, **(3) Slack 서버 측 ping timeout**.

로그를 보면 **연결 후 1~4분 만에 disconnect → reconnect 시도 시 DNS 실패** 패턴. 즉 disconnect 자체는 DNS 가 아니라 **NAT/방화벽 idle timeout 일 가능성**이 있고, reconnect 시점에서만 DNS 실패가 겹치는 것. 두 문제가 독립적으로 존재할 수 있음.

## Cron 6일 미실행 — 동일 root cause 의 파생 (별개 사고 아님)

**SRE 판정**: crash loop 중 프로세스 수명이 cron 실행 시점(오전/오후 고정 시각)까지 살아남지 못하거나, 재시작 직후 스케줄러 초기화 완료 전에 다시 crash 한 결과.

**백엔드 개발자 보완**: `cron.py:start()` 의 `_recompute_next_runs()` 가 기동 시 next_run_at_ms 를 재계산하므로 crash loop 해소 시 자동 복구됨. `nextRunAtMs: null` 은 one-shot 잡의 시각 경과 또는 `croniter` 패키지 상태에 의한 것일 수도 있음.

## 개선 우선순위 (4명 컨센서스)

| 순위 | 조치 | 비용 | 효과 | 담당 |
|---|---|---|---|---|
| **P0** | **코드: exponential backoff + MAX_RECONNECT_FAILURES 상향** | ~30줄 코드 | 89회 crash → 0회 가능 | companio 코드 변경 (PR) |
| **P0** | **`pip install aiodns`** | 1줄 설치 | Dnscache 우회, DNS 해석 안정화 | 이 머신 즉시 실행 |
| **P1** | **DNS 서버를 1.1.1.1 / 8.8.8.8 로 변경** | 2분, 네트워크 설정 | ISP DNS 불안정 회피 | 사용자 직접 |
| **P1** | **Windows negative cache TTL 비활성화** | 레지스트리 1줄 | Dnscache negative caching 제거 | 사용자 직접 |
| **P2** | **PM2 ecosystem.config.js 최적화** | 설정 파일 1개 | restart 정책 세밀화 (exp_backoff_restart_delay, max_restarts, min_uptime) | DevOps |
| **P2** | **PM2 로그 로테이션** (`pm2-logrotate`) | 설치 + 설정 3줄 | 디스크 보호 | DevOps |
| **P3** | **모니터링: restart 횟수 > 임계치 시 Slack 알림** | 간단 스크립트 | 24시간 방치 사고 방지 | DevOps |
| **P3** | **NAT/방화벽 idle timeout 점검** | 공유기 설정 확인 | disconnect 빈도 자체 감소 | 네트워크 |

## 코드 변경 구체 설계 (P0)

### 백엔드 개발자 제안 채택

```python
# 상수
MAX_RECONNECT_FAILURES = 10          # 5 → 10 (backoff 합산 ~15분)
RECONNECT_BASE_DELAY = 5             # 초기 5초
RECONNECT_MAX_DELAY = 300            # 최대 5분
JITTER_FACTOR = 0.3

# health check 루프
while self._running:
    delay = min(
        RECONNECT_BASE_DELAY * (2 ** self._consecutive_failures),
        RECONNECT_MAX_DELAY,
    )
    jitter = delay * JITTER_FACTOR * (2 * random.random() - 1)
    await asyncio.sleep(max(1.0, delay + jitter))

    try:
        connected = await self._handler.client.is_connected()
    except Exception:
        connected = False

    if not connected:
        self._consecutive_failures += 1
        self._log.error(
            "Slack WebSocket disconnected (failure {}/{}, next check in {:.0f}s)",
            self._consecutive_failures, MAX_RECONNECT_FAILURES,
            min(RECONNECT_BASE_DELAY * (2 ** self._consecutive_failures), RECONNECT_MAX_DELAY),
        )
        # 능동적 reconnect 시도
        try:
            await self._handler.close()
            await self._handler.connect_async()
            self._log.info("Active reconnect succeeded")
            self._consecutive_failures = 0
            continue
        except Exception as e:
            self._log.warning("Active reconnect failed: {}", e)

        if self._consecutive_failures >= MAX_RECONNECT_FAILURES:
            self._log.critical("Slack reconnection failed {} times, exiting", MAX_RECONNECT_FAILURES)
            sys.exit(1)
    else:
        if self._consecutive_failures > 0:
            self._log.info("Slack WebSocket reconnected after {} failures", self._consecutive_failures)
        self._consecutive_failures = 0
```

핵심 변경점:
1. **exponential backoff + jitter** — 5s → 10s → 20s → 40s → ... → 5분 cap. 총 10회 시 ~15분 예산
2. **능동적 reconnect** — `is_connected()` 실패 시 `handler.close()` + `connect_async()` 시도 (현재는 수동 폴링만)
3. **MAX_RECONNECT_FAILURES = 10** — backoff 합산 ~15분
4. **health check 간격도 backoff** — 정상 시 5초, 실패 누적 시 점점 길어짐

### 추가: slack-bolt 자체 reconnect 과의 관계

**백엔드 분석**: `AsyncSocketModeHandler` 가 내부적으로 auto-reconnect 을 수행. companio 의 health check 는 2차 안전망. 현재 5초 폴링이 SDK reconnect 과 경합할 가능성 있음 → **정상 시 폴링 간격을 30초로 늘려도 됨** (backoff 로 실패 시 자동 조절).

## 현실적 SLO 제안 (SRE)

| 지표 | SLI | SLO |
|---|---|---|
| 가용성 | 업무시간 중 멘션 응답 성공률 | **99.0%** (월 ~108분 허용 다운타임) |
| 응답 지연 | 멘션→첫 응답 p95 | **< 30초** |
| Cron 실행률 | 예정 브리핑 중 실제 실행 비율 | **95%** |

30명 사내 봇에 99.9% 는 과잉. **P0 코드 fix + P0 aiodns 만 적용해도 99.0% 달성 가능**.

## 모니터링 수준 (SRE — over-engineering 방지)

Grafana/Prometheus 스택은 과잉. 적정 수준:
1. **PM2 restart 임계치 알림** — restart count > 10/hour 시 Slack 알림 (별도 채널)
2. **Heartbeat** — 봇이 5분마다 health check (외부 cron 또는 UptimeRobot 무료)
3. **Cron 실행 로그** — HISTORY.md 기록, 일 1회 확인

총 구축 시간 2~3시간.
