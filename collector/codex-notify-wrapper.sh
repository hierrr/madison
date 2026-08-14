#!/bin/bash
# Codex notify 체이닝 래퍼 (IMPLEMENTATION.md §4.3)
# 1) MADISON에 turn_done 보고(백그라운드, 실패 무해) 2) 원래 notify 프로그램을 동일 인자로 exec.
set -u
MAD_DIR="$HOME/.claude/madison"

(
  # shellcheck disable=SC1090
  . "$MAD_DIR/env" 2>/dev/null || exit 0
  [ -n "${MADISON_URL:-}" ] && [ -n "${MADISON_TOKEN:-}" ] || exit 0
  command -v jq >/dev/null 2>&1 || exit 0
  PAYLOAD="${1:-}"
  if [ "${MADISON_DEBUG:-0}" = "1" ]; then
    mkdir -p "$MAD_DIR/samples"
    printf '%s' "$PAYLOAD" > "$MAD_DIR/samples/codex-notify.json" 2>/dev/null
  fi
  SID=$(printf '%s' "$PAYLOAD" | jq -r '."session-id" // .session_id // ."thread-id" // .thread_id // "codex"' 2>/dev/null) || SID="codex"
  MSG=$(printf '%s' "$PAYLOAD" | jq -r '(."last-assistant-message" // .last_assistant_message // "") | tostring | .[0:200]' 2>/dev/null) || MSG=""
  CWDX=$(printf '%s' "$PAYLOAD" | jq -r '.cwd // ""' 2>/dev/null) || CWDX=""
  # 모델·에포트: 해당 세션의 rollout(turn_context)에서 실제 값 → 없으면 config 기본값
  MODEL=""; EFFORT=""; ORIGINATOR=""
  ROLL=$(ls -t "$HOME"/.codex/sessions/*/*/*/rollout-*-"$SID".jsonl 2>/dev/null | head -1)
  if [ -n "$ROLL" ] && [ -f "$ROLL" ]; then
    TC=$(grep '"turn_context"' "$ROLL" 2>/dev/null | tail -1)
    MODEL=$(printf '%s' "$TC" | jq -r '.payload.model // ""' 2>/dev/null) || MODEL=""
    EFFORT=$(printf '%s' "$TC" | jq -r '.payload.effort // ""' 2>/dev/null) || EFFORT=""
    ORIGINATOR=$(head -1 "$ROLL" 2>/dev/null | jq -r '.payload.originator // ""' 2>/dev/null) || ORIGINATOR=""
  fi
  [ -z "$MODEL" ] && MODEL=$(sed -n 's/^model *= *"\(.*\)"/\1/p' "$HOME/.codex/config.toml" 2>/dev/null | head -1)
  [ -z "$EFFORT" ] && EFFORT=$(sed -n 's/^model_reasoning_effort *= *"\(.*\)"/\1/p' "$HOME/.codex/config.toml" 2>/dev/null | head -1)
  # 프런트엔드: originator 실측값 — codex-tui/codex_exec = CLI 계열, Codex Desktop 등 그 외 = 앱
  case "$ORIGINATOR" in
    codex_exec|exec) FRONTEND="auto" ;;   # headless 실행 = 자동화 계열
    codex-tui|cli|"") FRONTEND="cli" ;;
    *) FRONTEND="app" ;;
  esac
  PROJECT=""; BRANCH=""
  if [ -n "$CWDX" ] && [ -d "$CWDX" ]; then
    TOP=$(git -C "$CWDX" rev-parse --show-toplevel 2>/dev/null || true)
    if [ -n "$TOP" ]; then PROJECT=$(basename "$TOP"); BRANCH=$(git -C "$CWDX" branch --show-current 2>/dev/null || true)
    else PROJECT=$(basename "$CWDX"); fi
  fi
  TS_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  EV=$(jq -cn --arg sid "$SID" --arg ts "$TS_NOW" \
    --arg eid "$(uuidgen 2>/dev/null || echo "codex-$$-$RANDOM")" --arg s "$MSG" \
    --arg project "$PROJECT" --arg branch "$BRANCH" --arg m "$MODEL" --arg e "$EFFORT" \
    --arg f "$FRONTEND" \
    '{agent:"codex-cli", session_id:$sid, event:"turn_done", ts:$ts, event_id:$eid,
      project:$project, branch:$branch, detail:{summary:$s, model:$m, effort:$e, frontend:$f}}')
  BODY="[$EV]"
  if [ "$ORIGINATOR" = "codex_exec" ]; then
    # exec(단발 실행)은 턴 완료 = 세션 종료 — 대기 상태로 남지 않게 함께 보고
    EV2=$(jq -cn --arg sid "$SID" --arg ts "$TS_NOW" \
      --arg eid "$(uuidgen 2>/dev/null || echo "codexend-$$-$RANDOM")" \
      --arg project "$PROJECT" --arg branch "$BRANCH" \
      '{agent:"codex-cli", session_id:$sid, event:"session_end", ts:$ts, event_id:$eid,
        project:$project, branch:$branch, detail:{reason:"exec_done"}}')
    BODY="[$EV,$EV2]"
  fi
  CODE=$(curl -sS -m 2 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $MADISON_TOKEN" -H 'Content-Type: application/json' \
    -X POST "$MADISON_URL/api/events" --data-binary "$BODY" 2>/dev/null || echo 000)
  case "$CODE" in
    2*) : ;;
    *) printf '%s\n' "$EV" >> "$MAD_DIR/spool.jsonl" 2>/dev/null
       [ "$ORIGINATOR" = "codex_exec" ] && printf '%s\n' "$EV2" >> "$MAD_DIR/spool.jsonl" 2>/dev/null ;;
  esac
) &

# 원래 notify 프로그램 체이닝 — 기존 Computer Use 동작 보존
ORIG="$MAD_DIR/codex-orig-notify.json"
if [ -f "$ORIG" ] && command -v jq >/dev/null 2>&1; then
  CMD=()
  while IFS= read -r part; do CMD+=("$part"); done < <(jq -r '.[]' "$ORIG" 2>/dev/null)
  if [ "${#CMD[@]}" -gt 0 ]; then
    exec "${CMD[@]}" "$@"
  fi
fi
exit 0
