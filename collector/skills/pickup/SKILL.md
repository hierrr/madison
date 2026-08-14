---
name: pickup
description: 이 기기로 온 MADISON 핸드오프를 지금 즉시 가져와 이어서 작업한다 — 대기 중인 핸드오프 조회, 브랜치 체크아웃, 핸드오프 문서 낭독, 진행 계획 제시. 사용법 /pickup.
---

# /pickup — 핸드오프 즉시 수령

세션 시작을 기다리지 않고 지금 당장 이 기기의 대기 중 핸드오프를 가져온다.

## 절차

1. `~/.claude/madison/env` 존재 확인(없으면 미등록 안내 후 중단).
2. **대기 목록 조회**:
   ```bash
   source ~/.claude/madison/env
   # 현재 디렉터리가 git 리포면 리포 필터를 붙인다
   REPO=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || echo "")
   curl -sS -m 5 -H "Authorization: Bearer $MADISON_TOKEN" \
     "$MADISON_URL/api/handoffs?mine=1${REPO:+&repo=$REPO}"
   ```
   - 비어 있으면: 리포 필터 없이 한 번 더 조회해 다른 리포 대상 건이 있는지 알려주고 종료.
3. **수령 처리**: 가져온 각 건에 대해
   - 해당 리포가 아니면 그 리포 디렉터리로 이동(없으면 clone 여부를 사용자에게 확인).
   - `git fetch && git checkout <branch> && git pull` 로 브랜치를 맞춘다.
   - `doc_path`의 핸드오프 문서를 읽고 **목표/완료/미완/주의/다음 단계를 요약해 사용자에게 보고**한다.
   - `curl -sS -m 5 -H "Authorization: Bearer $MADISON_TOKEN" -H 'Content-Type: application/json' -X PATCH "$MADISON_URL/api/handoffs/<id>" --data-binary '{"status":"delivered"}'`
4. **이어가기**: 문서의 "다음 단계"를 기준으로 진행 계획을 제시하고, 사용자가 승인하면 바로 착수한다. 작업이 끝나면 해당 핸드오프를 `{"status":"done"}`으로 갱신한다.
