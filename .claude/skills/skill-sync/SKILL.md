---
name: skill-sync
description: 여러 프로젝트에 퍼져 있는 Claude Code 스킬을 SSOT 프로젝트 기준으로 동기화한다. 스킬 변경 감지, diff 비교, 양방향 동기화(로컬→SSOT, SSOT→로컬)를 지원한다. 사용자가 "스킬 동기화", "스킬 싱크", "skill sync", "스킬 배포", "스킬 업데이트", "SSOT 동기화", "스킬 최신화", "스킬 반영" 등을 언급하거나 여러 프로젝트 간 스킬 일관성 유지가 필요할 때 반드시 이 스킬을 사용한다.
---

# /skill-sync — 스킬 동기화

여러 프로젝트에 퍼져 있는 Claude Code 스킬(`.claude/skills/`)을 하나의 SSOT(Single Source of Truth) 프로젝트 기준으로 동기화한다.

스킬은 개별 SKILL.md 파일 단위로 비교하고, 변경이 있으면 diff를 보여주어 사용자가 직접 확인하고 선택할 수 있게 한다.

---

## 설정

동기화 설정은 `~/.claude/skill-sync.json`에 저장한다.

```json
{
  "ssot": "/absolute/path/to/ssot-project",
  "search_roots": ["~/my-projects"],
  "exclude_patterns": ["node_modules", ".git", "dist", "build"]
}
```

| 필드 | 설명 | 기본값 |
|------|------|--------|
| `ssot` | SSOT 프로젝트 루트 경로 | (첫 실행 시 사용자에게 물어봄) |
| `search_roots` | 프로젝트 탐색 루트 디렉토리 목록 | `["~"]` |
| `exclude_patterns` | 탐색 제외 패턴 | `["node_modules", ".git", "dist", "build"]` |

### 첫 실행 시 설정

설정 파일이 없으면 대화형으로 생성한다:

1. "SSOT 프로젝트 경로를 알려주세요" → 경로 입력받아 `ssot` 저장
2. "프로젝트들이 있는 루트 디렉토리는?" → 경로 입력받아 `search_roots` 저장
3. `~/.claude/skill-sync.json`에 기록

---

## 워크플로

### Phase 1: 상태 확인

#### 1-1. 설정 로드

```bash
cat ~/.claude/skill-sync.json
```

설정 파일이 없으면 → "첫 실행 시 설정" 절차 실행.

#### 1-2. SSOT git 상태 확인

```bash
cd <ssot-path> && git status --porcelain && git fetch origin && git log HEAD..origin/main --oneline
```

- 미커밋 변경이 있으면 사용자에게 알림: "SSOT에 커밋되지 않은 변경이 있습니다"
- origin보다 뒤처져 있으면: "SSOT이 원격보다 N커밋 뒤에 있습니다. pull 하시겠습니까?"
- 사용자 승인 시 `git pull origin main` 실행

#### 1-3. 현재 프로젝트 스킬 목록 수집

```bash
ls <current-project>/.claude/skills/
ls <ssot-path>/.claude/skills/
```

스킬 목록을 3가지로 분류:
- **공통**: 양쪽 모두 존재하는 스킬
- **로컬 전용**: 현재 프로젝트에만 있는 스킬 (프로젝트 고유 스킬)
- **SSOT 전용**: SSOT에만 있는 스킬 (아직 이 프로젝트에 없음)

### Phase 2: 변경 감지 (Diff)

공통 스킬에 대해 SKILL.md를 비교한다:

```bash
diff <ssot-path>/.claude/skills/<name>/SKILL.md <current>/.claude/skills/<name>/SKILL.md
```

결과를 요약 테이블로 보여준다:

```
## 스킬 동기화 상태

| 스킬 | 상태 | 방향 |
|------|------|------|
| git-commit-create | 변경됨 | 로컬 ≠ SSOT |
| feature-develop | 동일 | ✓ |
| dispatch-agents | 변경됨 | 로컬 ≠ SSOT |

로컬 전용: db-migrate, portal-ontology, ...
SSOT 전용: debug, plan, vitest, ...
```

### Phase 3: 동기화 방향 결정

변경된 각 스킬에 대해 사용자에게 물어본다:

1. **diff 표시**: 변경 내용을 보여줌
2. **방향 선택**:
   - `→ SSOT`: 로컬 버전을 SSOT에 반영
   - `← SSOT`: SSOT 버전을 로컬에 반영  
   - `skip`: 이 스킬은 건너뜀

SSOT 전용 스킬에 대해서는:
- "이 스킬을 현재 프로젝트에 가져오시겠습니까?" → 선택적 복사

로컬 전용 스킬에 대해서는:
- "이 스킬을 SSOT에 추가하시겠습니까?" → 범용 스킬인지 확인 후 추가

### Phase 4: 동기화 실행

선택에 따라 파일을 복사한다.

**로컬→SSOT 반영 시:**

```bash
# SKILL.md 복사
cp <current>/.claude/skills/<name>/SKILL.md <ssot>/.claude/skills/<name>/SKILL.md

# SSOT에서 git commit + push
cd <ssot> && git add .claude/skills/<name>/SKILL.md && git commit -m "sync: update <name> skill from <project>"
```

- 커밋 메시지에 어디서 왔는지 기록
- push는 사용자 확인 후 실행

**SSOT→로컬 반영 시:**

```bash
# 폴더가 없으면 생성
mkdir -p <current>/.claude/skills/<name>/
cp <ssot>/.claude/skills/<name>/SKILL.md <current>/.claude/skills/<name>/SKILL.md
```

- 로컬 프로젝트의 git 커밋은 사용자에게 맡김 (자동 커밋하지 않음)

### Phase 5: 다른 프로젝트에 배포 (선택)

SSOT이 업데이트되었으면:

1. "다른 프로젝트들에도 동기화하시겠습니까?"
2. 승인 시 → 프로젝트 탐색

#### 프로젝트 탐색

`search_roots` 하위에서 `.claude/skills/` 디렉토리가 있는 git 프로젝트를 찾는다:

```bash
find <search_root> -maxdepth 5 -name "skills" -path "*/.claude/skills" -type d 2>/dev/null
```

결과를 목록으로 제안:

```
발견된 프로젝트:
 1. /Users/holmes/my-projects/customers/cfactory/cura-ai
 2. /Users/holmes/my-projects/customers/jsauto/bbakcar
 3. /Users/holmes/my-projects/internal/project-management
 ...

동기화할 프로젝트를 선택하세요 (번호, 쉼표 구분, 'all'):
```

선택된 프로젝트 각각에 대해 Phase 2~4를 반복한다. 단, 대량 배포 시에는 변경 스킬만 요약하고 일괄 승인을 받을 수 있다.

---

## 주의사항

- **SKILL.md만 동기화**: `references/`, `evals/` 등 하위 리소스는 동기화하지 않는다. 프로젝트별로 다를 수 있기 때문.
- **자동 커밋 범위**: SSOT 프로젝트에만 자동 커밋. 로컬 프로젝트는 사용자가 직접 커밋.
- **git push는 항상 확인**: SSOT push 전에 반드시 사용자 확인.
- **프로젝트 고유 스킬 보호**: `portal-ontology`, `db-migrate` 같은 프로젝트 전용 스킬을 SSOT에 올릴지는 항상 물어봄.
- **충돌 시 덮어쓰기 금지**: 양쪽 다 변경된 경우 반드시 diff를 보여주고 사용자가 선택.

---

## 빠른 사용 예시

**현재 프로젝트 → SSOT 동기화:**
> "스킬 싱크해줘" → 현재 프로젝트와 SSOT 비교 → 변경된 것 diff → 방향 선택 → 반영

**SSOT → 전체 배포:**
> "SSOT 스킬 전체 배포해줘" → SSOT 최신화 확인 → 프로젝트 탐색 → 선택 → 일괄 동기화

**특정 스킬만 동기화:**
> "feature-develop 스킬만 싱크해줘" → 해당 스킬만 비교 → 반영
