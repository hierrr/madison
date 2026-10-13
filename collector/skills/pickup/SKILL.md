---
name: pickup
description: 이 기기의 대기 중인 MADISON 핸드오프를 조회하고 내용을 확인해 수신 처리한다. 승인된 후속 작업에 착수하면 작업 중, 끝나면 완료로 기록한다. 사용법 /pickup.
---

# /pickup — 핸드오프 즉시 수령

대기 중인 핸드오프를 가져와 맥락을 넘겨받는다. 수신과 작업 착수는 별개다.

## 절차

1. `~/.claude/madison/env` 존재 확인(없으면 미등록 안내 후 중단).
2. **대기 목록 조회**:
   ```bash
   source ~/.claude/madison/env
   # 현재 디렉터리가 git 리포면 리포 필터를 붙인다
   PICKUP_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)
   PICKUP_REPO=${PICKUP_ROOT##*/}
   curl -sS -m 5 -G -H "Authorization: Bearer $MADISON_TOKEN" \
     --data-urlencode 'mine=1' --data-urlencode "repo=$PICKUP_REPO" \
     "$MADISON_URL/api/handoffs"
   ```
   - `mine=1`은 이 기기의 `pending`만 반환한다. 수신됨·작업 중인 건까지 조회 범위를 넓히지 않는다.
   - 비어 있으면: 리포 필터 없이 한 번 더 조회해 다른 리포 대상 건이 있는지 알려주고 종료. 목록 안내만 한 건은 수신 처리하지 않는다.
3. **내용 확인 → 수신 처리**: 가져온 각 건에 대해
   - `doc`을 읽고 **목표/완료/미완/주의/다음 단계를 요약해 사용자에게 보고**한다.
   - `patches`(JSON 문자열 — `[{repo, base, diff}]`)가 있으면 리포별 대상·기준 커밋·diff 크기를 함께 알린다.
   - 구버전 건(doc 없음, doc_path만 있음)은 `git fetch` 후 `git show origin/<branch>:<doc_path>`로 읽는다.
   - 내용을 확인한 건은 후속 작업 승인 여부와 관계없이 즉시 수신됨으로 갱신한다. `/pickup` 요청 자체가 수신 처리 지시이므로 별도 확인을 요구하지 않는다.
     ```bash
     curl -sS -m 5 -H "Authorization: Bearer $MADISON_TOKEN" -H 'Content-Type: application/json' \
       -X PATCH "$MADISON_URL/api/handoffs/<id>" --data-binary '{"status":"delivered"}'
     ```
   - 문서를 읽지 못했으면 대기 중으로 남긴다. 상태 갱신이 실패하면 수신 처리 완료라고 보고하지 않는다. 이 단계에서는 branch checkout·pull·patch 적용을 하지 않는다.
4. **후속 작업 착수**: 문서의 다음 단계를 기준으로 진행한다.
   - 이미 사용자가 해당 작업을 지시했으면 다시 허락을 묻지 않는다. 수신만 요청했고 후속 작업 승인이 필요하면 구체적인 진행 범위를 제시해 확인한다. 사용자가 보류하면 `delivered`로 남긴다.
   - 실제 작업에 착수할 때 먼저 `PATCH /api/handoffs/<id>`에 `{"status":"in_progress"}`를 보낸다. 수신 처리와 같은 인증·요청 형식을 사용하며, 실패하면 작업 중으로 기록됐다고 보고하지 않는다.
   - 코드 이관이 필요한 경우 해당 리포에서 기존 변경과 branch를 확인하고 fetch·checkout·pull을 진행한다. 리포가 없으면 clone 여부를 확인한다. 문서만 전달된 건에 불필요한 git 작업을 하지 않는다.
   - patches 각 항목의 기준 커밋 존재를 확인한다(`git cat-file -e <base>`, 없으면 fetch 후 재시도). diff를 임시 파일로 저장해 해당 리포에서 `git apply -3 <파일>`로 적용한다. 이미 반영된 변경은 중복 적용하지 않는다. 충돌이 나면 중단하고 상황을 보고하며 기존 사용자 변경을 초기화하지 않는다.
   - 작업이 끝나면 `{"status":"done"}`, 사용자가 취소를 지시하면 `{"status":"cancelled"}`로 갱신한다.

이미 수신한 건을 나중에 이어갈 때는 대화에 남은 HF-ID로 `GET /api/handoffs/<id>`를 조회한다. `/pickup`으로 다시 수령하지 않는다.

상태: **대기 중(pending) → 수신됨(delivered) → 작업 중(in_progress) → 완료(done)**. 취소는 `cancelled`다. 도착 알림·SessionStart 안내만으로는 상태를 바꾸지 않는다.
