---
name: handoff
description: 현재 작업을 다른 기기로 이관한다 — 커밋·push 없이 핸드오프 문서와 변경 diff를 MADISON 허브 큐로 직접 전송. 사용법 /handoff <대상기기> [한 줄 요약]. 대상 기기에 데스크탑 알림이 가고, 세션을 열면 자동 안내된다.
---

# /handoff — 작업을 다른 기기로 이관

원리: **문서(의도·맥락)와 변경 diff(코드 상태)를 허브가 직접 나른다.** 리포에 커밋·push하지 않는다.
수신 인지는 대상 기기의 알림·SessionStart 안내가 담당하고, 착수 여부는 사용자가 정한다.

인자: 첫 단어 = 대상 기기 이름(예: studio), 나머지 = 한 줄 요약(없으면 네가 작성).

## 절차 (순서대로, 전부 수행)

1. **전제 확인**: `~/.claude/madison/env`가 존재해야 한다(없으면 "이 기기는 MADISON 미등록"이라 안내하고 중단). 현재 디렉터리가 git 리포인지 확인한다.
2. **대상 기기 확인**: `source ~/.claude/madison/env` 후
   `curl -sS -m 5 -H "Authorization: Bearer $MADISON_TOKEN" "$MADISON_URL/api/devices"`
   응답은 이 기기를 제외한 피어 목록 `[{"name":"imac2","online":true}, …]`.
   - 대상 이름이 목록에 없으면 가능한 이름들을 보여주고 중단한다(오타 방지).
   - 대상이 `"online": false`면 "꺼져 있거나 신호가 끊겨 알림·세션 안내가 늦을 수 있다"고 알리고 계속할지 확인한다.
3. **기준 커밋 확인**: patch는 수신 기기에도 있는 커밋 위에서만 적용된다.
   `git branch -r --contains HEAD`가 비어 있으면 HEAD가 미push 커밋이다 — §폴백으로 안내하고 중단.
4. **변경 diff 추출** (리포마다 `{repo, base, diff}` 한 항목):
   - 기준: `BASE=$(git rev-parse HEAD)`
   - 워킹트리 변경(미추적 포함): `git add -N . && git diff HEAD > <임시>.patch; git reset -q`
   - 이관할 스태시가 있으면: `git stash show -p --include-untracked stash@{n}`을 별도 patch로 추가.
   - 서브모듈·연관 리포에도 변경이 있으면 각 리포에서 같은 방식으로 추출해 patches 배열에 항목 추가.
   - diff에 바이너리 파일 변경이 있으면 실리지 않는다 — 사용자에게 알리고 §폴백 여부 확인.
   - 변경이 전혀 없으면(문서만 이관) patches 없이 진행.
5. **핸드오프 문서 작성** (파일로 커밋하지 않는다 — 본문을 허브로 전송): 마크다운, 섹션 전부 구체적으로:
   - `## 목표` — 이 작업이 끝나면 무엇이 되는가
   - `## 완료` — 지금까지 한 것
   - `## 미완` — 남은 것 (체크박스)
   - `## 주의` — 함정, 결정 사항, 하지 말 것
   - `## 다음 단계` — 이어받은 에이전트가 바로 실행할 순서. patches가 있으면 첫 항목은 적용 지침
     (예: "각 patch를 해당 리포에서 `git apply -3`").
6. **허브 큐 등록**:
   ```bash
   source ~/.claude/madison/env
   REPO=$(basename "$(git rev-parse --show-toplevel)")
   ORIGIN=$(git remote get-url origin 2>/dev/null || echo "")
   BRANCH=$(git branch --show-current)
   jq -n --arg to "<대상기기>" --arg repo "$REPO" --arg origin "$ORIGIN" --arg branch "$BRANCH" \
     --arg s "<한 줄 요약>" --rawfile doc <문서파일> \
     --arg base "$BASE" --rawfile diff <patch파일> \
     '{to:$to, repo:$repo, origin:$origin, branch:$branch, summary:$s, doc:$doc,
       patches: [{repo:$repo, base:$base, diff:$diff}]}' \
   | curl -sS -m 10 -H "Authorization: Bearer $MADISON_TOKEN" -H 'Content-Type: application/json' \
       -X POST "$MADISON_URL/api/handoffs" --data-binary @-
   ```
   (patch가 여럿이면 patches 배열을 그에 맞게 구성, 없으면 patches 생략.)
   400 응답(상한 초과: doc 64KB / patches 1MB)이면 §폴백으로 전환.
7. **보고**: 응답의 HF-ID와 함께 "대상 기기에 데스크탑 알림(최대 5분 내)이 가고, 세션을 열면 자동 안내된다. 사용자가 /pickup으로 착수를 지시할 때까지 대기 상태로 남는다"라고 알린다.

## 폴백 — git 경로 (미push 커밋 위의 작업 · 상한 초과 · 바이너리 변경)

사용자에게 이유를 설명하고 동의를 받은 뒤: `wip/<주제-슬러그>` 브랜치에 변경을 커밋하고 push한다.
문서(§5)의 "다음 단계"를 patch 적용 대신 "브랜치 `wip/…` checkout"으로 쓰고, patches 없이 §6으로 등록한다.
origin이 없어 push도 불가하면 이관 수단이 없다 — 사용자에게 한계를 알린다.
