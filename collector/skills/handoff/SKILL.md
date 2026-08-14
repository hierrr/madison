---
name: handoff
description: 현재 작업을 다른 기기로 이관한다 — WIP 커밋·push 후 핸드오프 문서를 작성하고 MADISON 허브 큐에 등록. 사용법 /handoff <대상기기> [한 줄 요약]. 대상 기기에 데스크탑 알림이 가고, 세션을 열면 자동 안내된다.
---

# /handoff — 작업을 다른 기기로 이관

원리: **코드 상태는 git이, 의도·맥락은 문서가, 수신 인지는 대상 기기의 알림·SessionStart 안내가** 담당하고, 착수 여부는 사용자가 정한다.

인자: 첫 단어 = 대상 기기 이름(예: studio), 나머지 = 한 줄 요약(없으면 네가 작성).

## 절차 (순서대로, 전부 수행)

1. **전제 확인**: `~/.claude/madison/env`가 존재해야 한다(없으면 "이 기기는 MADISON 미등록"이라 안내하고 중단). 현재 디렉터리가 git 리포인지 확인한다.
2. **대상 기기 확인**: `source ~/.claude/madison/env` 후
   `curl -sS -m 5 -H "Authorization: Bearer $MADISON_TOKEN" "$MADISON_URL/api/devices"`
   응답은 이 기기를 제외한 피어 목록 `[{"name":"imac2","online":true}, …]`.
   - 대상 이름이 목록에 없으면 가능한 이름들을 보여주고 중단한다(오타 방지 — git 작업 전에 잡는다).
   - 대상이 `"online": false`면 "꺼져 있거나 신호가 끊겨 알림·세션 안내가 늦을 수 있다"고 알리고 계속할지 확인한다.
3. **코드 상태 운반 (git)**:
   - 미커밋 변경이 있으면 현재 브랜치가 main/master일 땐 `wip/<주제-슬러그>` 브랜치를 만들어 커밋하고, 이미 작업 브랜치면 그 브랜치에 WIP 커밋한다.
   - origin이 있으면 push한다. origin이 없으면 사용자에게 "push 없이는 코드가 이관되지 않는다(문서만 전달됨)"라고 경고하고 계속 진행 여부를 확인한다.
4. **핸드오프 문서 작성**: `docs/handoffs/<YYYYMMDD-HHMM>-<슬러그>.md`를 만들어 커밋·push에 포함한다. 섹션(전부 구체적으로):
   - `## 목표` — 이 작업이 끝나면 무엇이 되는가
   - `## 완료` — 지금까지 한 것
   - `## 미완` — 남은 것 (체크박스)
   - `## 주의` — 함정, 결정 사항, 하지 말 것
   - `## 다음 단계` — 이어받은 에이전트가 바로 실행할 명령/행동 순서
5. **허브 큐 등록**:
   ```bash
   source ~/.claude/madison/env
   REPO=$(basename "$(git rev-parse --show-toplevel)")
   ORIGIN=$(git remote get-url origin 2>/dev/null || echo "")
   BRANCH=$(git branch --show-current)
   curl -sS -m 5 -H "Authorization: Bearer $MADISON_TOKEN" -H 'Content-Type: application/json' \
     -X POST "$MADISON_URL/api/handoffs" --data-binary "$(jq -cn \
       --arg to "<대상기기>" --arg repo "$REPO" --arg origin "$ORIGIN" --arg branch "$BRANCH" \
       --arg doc "<문서 경로>" --arg s "<한 줄 요약>" \
       '{to:$to, repo:$repo, origin:$origin, branch:$branch, doc_path:$doc, summary:$s}')"
   ```
6. **보고**: 응답의 HF-ID와 함께 "대상 기기에 데스크탑 알림(최대 5분 내)이 가고, 그 기기에서 이 리포의 세션을 열면 자동 안내된다(에이전트 무관). 사용자가 /pickup으로 착수를 지시할 때까지 대기 상태로 남는다"라고 사용자에게 알린다.
