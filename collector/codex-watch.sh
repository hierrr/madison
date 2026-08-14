#!/bin/bash
# Codex history.jsonl 감시 → prompt 이벤트 합성 (IMPLEMENTATION.md §4.3). launchd 60초 주기.
set -u
MAD_DIR="$HOME/.claude/madison"
# shellcheck disable=SC1090
. "$MAD_DIR/env" 2>/dev/null || exit 0
[ -n "${MADISON_URL:-}" ] && [ -n "${MADISON_TOKEN:-}" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
HIST="$HOME/.codex/history.jsonl"
[ -f "$HIST" ] || exit 0
OFF_FILE="$MAD_DIR/codex-history.offset"

SIZE=$(stat -f %z "$HIST" 2>/dev/null || stat -c %s "$HIST" 2>/dev/null || echo 0)
# 최초 실행: 과거 이력을 쏟아내지 않고 현재 크기부터 시작
if [ ! -f "$OFF_FILE" ]; then echo "$SIZE" > "$OFF_FILE"; exit 0; fi
OFF=$(cat "$OFF_FILE" 2>/dev/null || echo 0)
case "$OFF" in '' | *[!0-9]*) OFF=0 ;; esac
[ "$SIZE" -lt "$OFF" ] && OFF=0   # 파일 축소/로테이션 → 처음부터
[ "$SIZE" -eq "$OFF" ] && exit 0

NEW=$(tail -c +"$((OFF + 1))" "$HIST" 2>/dev/null || true)
echo "$SIZE" > "$OFF_FILE"
[ -n "$NEW" ] || exit 0

# event_id를 (session_id, ts)로 결정적으로 만들어 오프셋 리셋 시에도 중복 흡수
BATCH=$(printf '%s\n' "$NEW" | jq -c '
  select(type=="object" and has("text")) |
  {agent:"codex-cli", session_id:(.session_id // "codex"), event:"prompt",
   ts:((.ts // 0) | todate),
   event_id:("codexh-" + ((.session_id // "x")|tostring) + "-" + ((.ts // 0)|tostring)),
   detail:{prompt:((.text // "") | .[0:200])}}' 2>/dev/null | jq -cs '.' 2>/dev/null) || exit 0
[ -n "$BATCH" ] && [ "$BATCH" != "[]" ] || exit 0

curl -sS -m 5 -o /dev/null \
  -H "Authorization: Bearer $MADISON_TOKEN" -H 'Content-Type: application/json' \
  -X POST "$MADISON_URL/api/events" --data-binary "$BATCH" 2>/dev/null
exit 0
