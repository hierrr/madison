#!/bin/bash
# MADISON collector — Claude Code 훅 본체 (IMPLEMENTATION.md §7)
# 원칙: 어떤 경우에도 exit 0. 세션을 절대 방해하지 않는다. macOS bash 3.2 호환.
set -u

MAD_DIR="$HOME/.claude/madison"
ENV_FILE="$MAD_DIR/env"
SPOOL="$MAD_DIR/spool.jsonl"
LOCK="$MAD_DIR/lock"
THROTTLE_DIR="$MAD_DIR/throttle"
SAMPLES_DIR="$MAD_DIR/samples"
SPOOL_MAX_LINES=5000

EV="${1:-}"
[ -z "$EV" ] && exit 0
# 허브 요약 워커 등 내부 실행은 보고하지 않는다 (재귀 차단)
[ "${MADISON_SUPPRESS:-0}" = "1" ] && exit 0
[ -f "$ENV_FILE" ] || exit 0
# shellcheck disable=SC1090
. "$ENV_FILE" 2>/dev/null || exit 0
[ -n "${MADISON_URL:-}" ] && [ -n "${MADISON_TOKEN:-}" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
mkdir -p "$THROTTLE_DIR" 2>/dev/null

# ── 락 (mkdir 기반 — macOS엔 flock 기본 부재) ──────────
lock_acquire() {
  local i=0
  while ! mkdir "$LOCK" 2>/dev/null; do
    i=$((i + 1)); [ "$i" -ge 20 ] && return 1
    sleep 0.05
  done
  return 0
}
lock_release() { rmdir "$LOCK" 2>/dev/null; return 0; }

spool_append() {  # $1=json 한 줄. O_APPEND 단일 write라 락 실패 시에도 안전한 편
  printf '%s\n' "$1" >> "$SPOOL" 2>/dev/null
}

spool_cap() {  # 상한 초과 시 오래된 것부터 버림 (§7.1)
  local lines
  lines=$(wc -l < "$SPOOL" 2>/dev/null || echo 0)
  if [ "${lines:-0}" -gt "$SPOOL_MAX_LINES" ]; then
    tail -n "$SPOOL_MAX_LINES" "$SPOOL" > "$SPOOL.tmp" 2>/dev/null && mv "$SPOOL.tmp" "$SPOOL"
  fi
}

post_events() {  # $1=timeout, $2=body → http code
  curl -sS -m "$1" -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $MADISON_TOKEN" -H 'Content-Type: application/json' \
    -X POST "$MADISON_URL/api/events" --data-binary "$2" 2>/dev/null || echo 000
}

spool_flush() {  # 호출 전 락 획득 전제. rename-first로 동시(락 실패) append 유실 방지 (§7.2)
  local work="$SPOOL.inflight" n chunk body code total
  [ -s "$SPOOL" ] || return 0
  # 스풀을 먼저 옆으로 치운다 — 이후의 락-없는 append는 새로 생기는 $SPOOL로 감(유실 없음)
  mv "$SPOOL" "$work" 2>/dev/null || return 1
  while [ -s "$work" ]; do
    chunk=$(head -n 100 "$work")
    n=$(printf '%s\n' "$chunk" | wc -l | tr -d ' ')
    body=$(printf '%s\n' "$chunk" | jq -cs 'map(select(type=="object"))' 2>/dev/null) || {
      # 손상 라인 방어: 파싱 불가면 해당 청크 폐기
      total=$(wc -l < "$work"); tail -n $((total - n)) "$work" > "$work.tmp" && mv "$work.tmp" "$work"
      continue
    }
    code=$(post_events 5 "$body")
    case "$code" in
      2*)
        total=$(wc -l < "$work" | tr -d ' ')
        if [ "$total" -le "$n" ]; then : > "$work"; else
          tail -n $((total - n)) "$work" > "$work.tmp" && mv "$work.tmp" "$work"
        fi ;;
      *)
        # 전송 실패 — 남은 work(더 오래됨)를 현재 스풀 앞에 되돌리고 종료
        [ -s "$SPOOL" ] && cat "$SPOOL" >> "$work"
        mv "$work" "$SPOOL"
        return 1 ;;
    esac
  done
  rm -f "$work"
  return 0
}

# ── flush 전용 모드 (flush.sh/launchd 플러셔가 호출) ────
if [ "$EV" = "__flush" ]; then
  [ -s "$SPOOL" ] || exit 0
  if lock_acquire; then spool_flush; lock_release; fi
  exit 0
fi

IN="$(cat 2>/dev/null || true)"
if [ "${MADISON_DEBUG:-0}" = "1" ]; then
  mkdir -p "$SAMPLES_DIR" 2>/dev/null
  printf '%s' "$IN" > "$SAMPLES_DIR/$EV.json" 2>/dev/null
fi

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SID=$(printf '%s' "$IN" | jq -r '.session_id // "unknown"' 2>/dev/null) || SID="unknown"
CWD=$(printf '%s' "$IN" | jq -r '.cwd // ""' 2>/dev/null) || CWD=""

CURL_M=2
[ "$EV" = "session_start" ] && CURL_M=1  # SessionStart는 stdout 주입 대기 때문에 예산 축소 (§7.2)

# ── 스로틀: heartbeat/tool_start는 세션당 60초 1회 ─────
if [ "$EV" = "heartbeat" ] || [ "$EV" = "tool_start" ]; then
  STAMP="$THROTTLE_DIR/$SID"
  NOW_EPOCH=$(date +%s)
  if [ -f "$STAMP" ]; then
    LAST=$(stat -f %m "$STAMP" 2>/dev/null || stat -c %Y "$STAMP" 2>/dev/null || echo 0)
    [ $((NOW_EPOCH - LAST)) -lt 60 ] && exit 0
  fi
  touch "$STAMP" 2>/dev/null
fi
# 승인 요청 직후엔 다음 이벤트가 즉시 통과해야 빨강 해제가 안 늦음 (§7.2)
[ "$EV" = "permission_request" ] && rm -f "$THROTTLE_DIR/$SID" 2>/dev/null

# ── 프런트엔드 판별: 앱(조상에 Claude.app) / auto(claude가 -p headless = launchd 등 자동화) / cli ──
# (TTY 기반은 불가 — Claude Code가 훅을 TTY 없이 띄우는 것 실측. 명령줄의 -p 유무가 결정적)
FRONTEND="cli"
if [ "$EV" = "session_start" ] || [ "$EV" = "prompt" ] || [ "$EV" = "turn_done" ]; then
  P="$PPID"; i=0
  while [ -n "$P" ] && [ "$P" -gt 1 ] && [ "$i" -lt 6 ]; do
    PCMD=$(ps -o command= -p "$P" 2>/dev/null || true)
    case "$PCMD" in
      *"/Applications/Claude.app/"*) FRONTEND="app"; break ;;
      *claude*" -p "*|*claude*" -p"|*claude*" --print"*) FRONTEND="auto"; break ;;
    esac
    P=$(ps -o ppid= -p "$P" 2>/dev/null | tr -d ' ') || break
    i=$((i + 1))
  done
fi

# ── 프로젝트/브랜치 ────────────────────────────────────
PROJECT=""; BRANCH=""; ORIGIN=""
if [ -n "$CWD" ] && [ -d "$CWD" ]; then
  TOPLEVEL=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)
  if [ -n "$TOPLEVEL" ]; then
    PROJECT=$(basename "$TOPLEVEL")
    BRANCH=$(git -C "$CWD" branch --show-current 2>/dev/null || true)
    ORIGIN=$(git -C "$CWD" remote get-url origin 2>/dev/null || true)
  else
    PROJECT=$(basename "$CWD")
  fi
fi

# ── 이벤트별 detail 추출 ──────────────────────────────
DETAIL="{}"
case "$EV" in
  session_start)
    DETAIL=$(printf '%s' "$IN" | jq -c --arg f "$FRONTEND" '{source: (.source // ""), frontend: $f}' 2>/dev/null) || DETAIL="{}"
    ;;
  prompt)
    DETAIL=$(printf '%s' "$IN" | jq -c --arg f "$FRONTEND" '{prompt: ((.prompt // "") | .[0:600]), frontend: $f}' 2>/dev/null) || DETAIL="{}"
    ;;
  heartbeat|tool_start)
    # 스로틀(분당 1회) 통과 시에만 오므로 전사본 꼬리에서 모델도 함께 추출
    TP=$(printf '%s' "$IN" | jq -r '.transcript_path // ""' 2>/dev/null) || TP=""
    MODEL=""
    if [ -n "$TP" ] && [ -f "$TP" ]; then
      MODEL=$(tail -n 60 "$TP" 2>/dev/null | jq -rs '
        [.[] | select(type=="object" and .type=="assistant")] | last | .message.model // ""
      ' 2>/dev/null) || MODEL=""
    fi
    # effort는 문자열 또는 {"level": "..."} 객체 두 형태가 실측됨
    EFF_JQ='(.effort | if type=="object" then (.level // "") elif type=="string" then . else "" end)'
    if [ "$EV" = "tool_start" ]; then
      DETAIL=$(printf '%s' "$IN" | jq -c --arg m "${MODEL:-}" "{tool: (.tool_name // .tool // \"\"), effort: $EFF_JQ, model: \$m}" 2>/dev/null) || DETAIL="{}"
    else
      DETAIL=$(printf '%s' "$IN" | jq -c --arg m "${MODEL:-}" "{effort: $EFF_JQ, model: \$m}" 2>/dev/null) || DETAIL="{}"
    fi
    ;;
  permission_request|idle)
    DETAIL=$(printf '%s' "$IN" | jq -c '{message: ((.message // .title // .tool_name // "") | tostring | .[0:200])}' 2>/dev/null) || DETAIL="{}"
    ;;
  turn_done)
    # 공식 필드 우선, transcript 꼬리 파싱은 폴백 (§4.2)
    SUMMARY=$(printf '%s' "$IN" | jq -r '(.last_assistant_message // "") | .[0:200]' 2>/dev/null) || SUMMARY=""
    TP=$(printf '%s' "$IN" | jq -r '.transcript_path // ""' 2>/dev/null) || TP=""
    MODEL=""
    if [ -n "$TP" ] && [ -f "$TP" ]; then
      MODEL=$(tail -n 60 "$TP" 2>/dev/null | jq -rs '
        [.[] | select(type=="object" and .type=="assistant")] | last | .message.model // ""
      ' 2>/dev/null) || MODEL=""
      if [ -z "$MODEL" ]; then
        # Stop 발화 직후 전사본 flush 레이스 — 한 번만 짧게 재시도
        sleep 0.3
        MODEL=$(tail -n 60 "$TP" 2>/dev/null | jq -rs '
          [.[] | select(type=="object" and .type=="assistant")] | last | .message.model // ""
        ' 2>/dev/null) || MODEL=""
      fi
      if [ -z "$SUMMARY" ]; then
        SUMMARY=$(tail -n 60 "$TP" 2>/dev/null | jq -rs '
          [.[] | select(type=="object" and .type=="assistant")] | last |
          (.message.content // []) | map(select(.type=="text") | .text) | join(" ") | .[0:200]
        ' 2>/dev/null) || SUMMARY=""
      fi
    fi
    DETAIL=$(jq -cn --arg s "${SUMMARY:-}" --arg m "${MODEL:-}" --arg f "$FRONTEND" \
      '{summary: $s, model: $m, frontend: $f}')
    ;;
  session_end)
    DETAIL=$(printf '%s' "$IN" | jq -c '{reason: (.reason // "")}' 2>/dev/null) || DETAIL="{}"
    ;;
esac
[ -z "$DETAIL" ] && DETAIL="{}"

EVJSON=$(jq -cn \
  --arg agent "claude-code" --arg sid "$SID" --arg ev "$EV" --arg ts "$TS" \
  --arg eid "$(uuidgen 2>/dev/null || echo "$SID-$TS-$EV-$$")" \
  --arg project "$PROJECT" --arg branch "$BRANCH" --argjson detail "$DETAIL" \
  '{agent:$agent, session_id:$sid, event:$ev, ts:$ts, event_id:$eid,
    project:$project, branch:$branch, detail:$detail}' 2>/dev/null) || exit 0

# ── 전송 (순서 보존: 스풀이 있으면 뒤에 붙여 함께 플러시) ──
if [ -s "$SPOOL" ]; then
  if lock_acquire; then
    spool_append "$EVJSON"; spool_cap; spool_flush; lock_release
  else
    spool_append "$EVJSON"
  fi
else
  CODE=$(post_events "$CURL_M" "$EVJSON")
  case "$CODE" in
    2*) : ;;
    *) if lock_acquire; then spool_append "$EVJSON"; lock_release; else spool_append "$EVJSON"; fi ;;
  esac
fi

# ── SessionStart: 핸드오프 큐 조회 → stdout 컨텍스트 주입 (§7.2, §9) ──
if [ "$EV" = "session_start" ]; then
  SRC=$(printf '%s' "$IN" | jq -r '.source // "startup"' 2>/dev/null) || SRC="startup"
  case "$SRC" in
    startup|resume)
      # 리포·origin 둘 다 모르면(비-git cwd 등) 조회 생략 — 무필터 전량 수령 방지
      if [ -z "$PROJECT" ] && [ -z "$ORIGIN" ]; then exit 0; fi
      QREPO=$(jq -rn --arg s "$PROJECT" '$s|@uri')
      QORIGIN=$(jq -rn --arg s "$ORIGIN" '$s|@uri')
      HLIST=$(curl -sS -m 1 -H "Authorization: Bearer $MADISON_TOKEN" \
        "$MADISON_URL/api/handoffs?mine=1&repo=$QREPO&origin=$QORIGIN" 2>/dev/null) || HLIST=""
      if [ -n "$HLIST" ] && printf '%s' "$HLIST" | jq -e 'type=="array" and length>0' >/dev/null 2>&1; then
        printf '%s' "$HLIST" | jq -r '.[] |
          "[MADISON] \(.from_name // "다른 기기")발 핸드오프 \(.hf) 도착: \(.summary // "요약 없음"). " +
          "브랜치 \(.branch // "?")를 체크아웃하고 \(.doc_path // "핸드오프 문서")를 읽은 뒤, " +
          "이어서 진행할지 사용자에게 확인하세요."' 2>/dev/null
        # 주입한 건만 delivered (§7.2)
        printf '%s' "$HLIST" | jq -r '.[].id' 2>/dev/null | while read -r HID; do
          [ -n "$HID" ] && curl -sS -m 1 -o /dev/null \
            -H "Authorization: Bearer $MADISON_TOKEN" -H 'Content-Type: application/json' \
            -X PATCH "$MADISON_URL/api/handoffs/$HID" \
            --data-binary '{"status":"delivered"}' 2>/dev/null
        done
      fi
      ;;
  esac
fi

exit 0
