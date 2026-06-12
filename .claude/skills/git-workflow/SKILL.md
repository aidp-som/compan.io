---
description: 작업 완료 후 이슈 생성 → sync → plan 이동 → 커밋을 한 번에 처리하는 통합 워크플로우
---

작업이 완료된 후 아래 4단계를 순서대로 진행하세요.
각 단계는 이전 단계 결과에 의존하므로 반드시 순서를 지킵니다.

---

## Step 1. GitHub 이슈 생성

`docs/workflow/issue.md` 가이드에 따라 이슈를 생성합니다.

1. 사용자에게 이슈 정보 수집:
   - 타입 (Bug / Feature / Task / Docs / Refactor)
   - 제목
   - 상세 내용 (배경, 작업 내용, 변경 파일 등)
   - 라벨 (기본: enhancement)
   - 담당자 (기본: @me)
2. 미리보기 표시 후 사용자 확인
3. 확인 후 `gh issue create` 실행 → 생성된 이슈 번호(#N) 기억

---

## Step 2. sync-issues 실행

이슈 생성 직후 아카이브 폴더를 동기화합니다.

```bash
node scripts/issues/sync.mjs
```

결과에서 Step 1에서 생성한 이슈 #N의 아카이브 폴더 경로를 확인합니다:
`.plans/archives/#N_<이슈제목>/`

---

## Step 3. Plan 파일 이동

`.plans/` 폴더의 plan 파일들 중 이번 작업과 관련된 파일을 이슈 아카이브 폴더로 이동합니다.

1. `.plans/*.md` 파일 목록 확인 (Glob 사용)
2. 각 파일의 첫 몇 줄을 읽어 이번 작업과 관련된 파일 식별
3. 관련 파일을 `mv` 명령으로 아카이브 폴더에 `plan.md`로 이동:
   ```bash
   mv ".plans/<파일명>.md" ".plans/archives/#N_<이슈제목>/plan.md"
   ```
4. 관련 plan 파일이 없으면 이 단계를 건너뜁니다.

---

## Step 4. Git 커밋 생성

`docs/workflow/commit.md` 가이드에 따라 커밋을 생성합니다.

1. `git branch --show-current` 확인 → 보호 브랜치(`main`, `master`, `develop`)면 새 브랜치 생성 여부 사용자에게 질문
2. `git status`로 변경 파일 목록 확인
3. **관련 파일만 선택적 스테이징** — 이번 작업과 무관한 파일은 제외:
   - Step 2~3 생성 파일(아카이브 폴더, plan.md)은 항상 포함
   - 무관한 파일이 있으면 사용자에게 어떤 파일을 포함할지 확인
4. `git log --oneline -5`로 기존 커밋 스타일 참고
5. 커밋 메시지 작성 (형식: `<타입>: <제목> (closes #N)`)
6. 사용자에게 커밋 내용 미리보기 후 확인
7. 커밋 실행 (파일 방식):
   ```
   # 가이드 순서: git add → 커밋 메시지 파일 생성 → git commit -F → 파일 삭제
   git add <선택된 파일들>
   # Write 도구로 .plans/.tmp/commit_msg.txt 생성
   git commit -F ".plans/.tmp/commit_msg.txt"
   # 임시 파일 삭제
   ```

---

## 완료 보고

4단계가 모두 끝나면 아래 형식으로 요약합니다:

| 단계        | 결과                                         |
| ----------- | -------------------------------------------- |
| 이슈 생성   | #N <제목>                                    |
| sync-issues | 아카이브 폴더 생성                           |
| plan 이동   | <파일명> → archives/#N/plan.md (또는 "없음") |
| 커밋        | <커밋 해시> — <브랜치>                       |
