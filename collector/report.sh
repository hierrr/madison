#!/bin/bash
# MADISON collector — Claude Code / Codex 공용 훅 본체 (IMPLEMENTATION.md §7)
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
AGENT="${2:-claude-code}"
[ -z "$EV" ] && exit 0
case "$AGENT" in claude-code|codex-cli) : ;; *) exit 0 ;; esac
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

notify_handoffs() {  # 새 pending 핸드오프 → 데스크탑 알림 (macOS). 실패 무해 (§9)
  [ "$(uname)" = "Darwin" ] || return 0
  command -v osascript >/dev/null 2>&1 || return 0
  local list stamp
  stamp="$MAD_DIR/notified-handoffs"
  list=$(curl -sS -m 3 -H "Authorization: Bearer $MADISON_TOKEN" \
    "$MADISON_URL/api/handoffs?mine=1" 2>/dev/null) || return 0
  printf '%s' "$list" | jq -e 'type=="array" and length>0' >/dev/null 2>&1 || return 0
  touch "$stamp" 2>/dev/null
  printf '%s' "$list" | jq -r '.[] |
    "\(.id)\t\(.hf)\t\(.from_name // "다른 기기")\t\(.repo // "?")\t\((.summary // "요약 없음") | .[0:120])"' 2>/dev/null |
  while IFS=$'\t' read -r hid hf from repo summary; do
    [ -n "$hid" ] || continue
    grep -qx "$hid" "$stamp" 2>/dev/null && continue
    osascript -e 'on run argv
      display notification (item 2 of argv) with title "MADISON 핸드오프 도착" subtitle (item 1 of argv)
    end run' -- "$from → $repo" "$hf $summary — /pickup으로 수령" >/dev/null 2>&1
    printf '%s\n' "$hid" >> "$stamp" 2>/dev/null
  done
  return 0
}

# ── flush 전용 모드 (flush.sh/launchd 플러셔가 호출) ────
if [ "$EV" = "__flush" ]; then
  notify_handoffs
  if [ -s "$SPOOL" ]; then
    if lock_acquire; then spool_flush; lock_release; fi
  fi
  exit 0
fi

IN="$(cat 2>/dev/null || true)"
if [ "${MADISON_DEBUG:-0}" = "1" ]; then
  mkdir -p "$SAMPLES_DIR" 2>/dev/null
  printf '%s' "$IN" > "$SAMPLES_DIR/$EV.json" 2>/dev/null
fi

SID=$(printf '%s' "$IN" | jq -r '.session_id // "unknown"' 2>/dev/null) || SID="unknown"

# ── 스로틀: heartbeat/tool_start는 세션당 60초 1회 ─────
# Codex 훅은 동기 실행(async 미지원)이라, 걸러질 이벤트는 아래 파싱 비용 전에 최대한 빨리 나간다.
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

TS="$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))' 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
CWD=$(printf '%s' "$IN" | jq -r '.cwd // ""' 2>/dev/null) || CWD=""
TP=$(printf '%s' "$IN" | jq -r '.transcript_path // ""' 2>/dev/null) || TP=""
TURN_ID=$(printf '%s' "$IN" | jq -r '.turn_id // ""' 2>/dev/null) || TURN_ID=""
TOOL_USE_ID=$(printf '%s' "$IN" | jq -r '.tool_use_id // ""' 2>/dev/null) || TOOL_USE_ID=""

CURL_M=2
[ "$EV" = "session_start" ] && CURL_M=1  # SessionStart는 stdout 주입 대기 때문에 예산 축소 (§7.2)

# ── 프런트엔드 판별: CLI / 데스크톱 앱 / headless 자동 실행 ──
# Claude는 부모 프로세스, Codex는 rollout session_meta.originator를 우선 사용한다.
# transcript 형식은 안정 API가 아니므로 Codex도 부모 프로세스를 폴백으로 함께 확인한다.
FRONTEND=""
ORIGINATOR=""
SESSION_SOURCE=""
if [ "$AGENT" = "codex-cli" ] && [ -n "$TP" ] && [ -f "$TP" ]; then
  META=$(head -n 1 "$TP" 2>/dev/null)
  ORIGINATOR=$(printf '%s' "$META" | jq -r '
    if .type == "session_meta" then (.payload.originator // "") else "" end
  ' 2>/dev/null) || ORIGINATOR=""
  SESSION_SOURCE=$(printf '%s' "$META" | jq -r '
    if .type == "session_meta" then (.payload.source // "") else "" end
  ' 2>/dev/null) || SESSION_SOURCE=""
  case "$ORIGINATOR" in
    codex_exec|exec) FRONTEND="auto" ;;
    codex-tui|cli) FRONTEND="cli" ;;
    "Codex Desktop"|codex-desktop|codex_desktop) FRONTEND="app" ;;
    *VSCode*|*vscode*|*"Visual Studio Code"*) FRONTEND="ide" ;;
  esac
fi
if [ -z "$FRONTEND" ]; then
  P="$PPID"; i=0
  while [ -n "$P" ] && [ "$P" -gt 1 ] && [ "$i" -lt 8 ]; do
    PCMD=$(ps -o command= -p "$P" 2>/dev/null || true)
    case "$PCMD" in
      *"/Applications/Claude.app/"*|*"/Applications/Codex.app/"*) FRONTEND="app"; break ;;
      *"/Applications/Visual Studio Code.app/"*|*.vscode/extensions/*)
        if [ "$AGENT" = "codex-cli" ]; then FRONTEND="ide"; break; fi ;;
      *claude*" -p "*|*claude*" -p"|*claude*" --print"*|*codex*" exec "*) FRONTEND="auto"; break ;;
    esac
    P=$(ps -o ppid= -p "$P" 2>/dev/null | tr -d ' ') || break
    i=$((i + 1))
  done
fi
if [ -z "$FRONTEND" ] && [ "$AGENT" = "codex-cli" ]; then
  case "$SESSION_SOURCE" in
    cli) FRONTEND="cli" ;;
    exec) FRONTEND="auto" ;;
    vscode) FRONTEND="ide" ;;
  esac
fi
if [ -z "$FRONTEND" ]; then
  if [ "$AGENT" = "claude-code" ]; then FRONTEND="cli"; else FRONTEND="unknown"; fi
fi

# ── 모델/effort ───────────────────────────────────────
# Codex는 model을 공식 훅 필드로 제공한다. effort는 훅 필드 우선, rollout turn_context 폴백.
MODEL=$(printf '%s' "$IN" | jq -r '.model // ""' 2>/dev/null) || MODEL=""
EFFORT=$(printf '%s' "$IN" | jq -r '
  .effort | if type=="object" then (.level // "") elif type=="string" then . else "" end
' 2>/dev/null) || EFFORT=""
if [ "$AGENT" = "codex-cli" ] && [ -n "$TP" ] && [ -f "$TP" ]; then
  # rollout은 매우 커질 수 있으므로 전체 grep 대신 최근 레코드만 본다.
  TC=$(tail -n 200 "$TP" 2>/dev/null | jq -cs '[.[] | select(.type=="turn_context")] | last // {}' 2>/dev/null) || TC="{}"
  [ -n "$MODEL" ] || MODEL=$(printf '%s' "$TC" | jq -r '.payload.model // ""' 2>/dev/null) || MODEL=""
  [ -n "$EFFORT" ] || EFFORT=$(printf '%s' "$TC" | jq -r '
    .payload.effort | if type=="object" then (.level // "") elif type=="string" then . else "" end
  ' 2>/dev/null) || EFFORT=""
elif [ "$AGENT" = "claude-code" ] && [ -z "$MODEL" ] && [ -n "$TP" ] && [ -f "$TP" ]; then
  MODEL=$(tail -n 60 "$TP" 2>/dev/null | jq -rs '
    [.[] | select(type=="object" and .type=="assistant")] | last | .message.model // ""
  ' 2>/dev/null) || MODEL=""
fi

# ── 프로젝트/브랜치 ────────────────────────────────────
PROJECT=""; BRANCH=""; ORIGIN=""; SUBDIR=""
if [ -n "$CWD" ] && [ -d "$CWD" ]; then
  TOPLEVEL=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)
  if [ -n "$TOPLEVEL" ]; then
    PROJECT=$(basename "$TOPLEVEL")
    BRANCH=$(git -C "$CWD" branch --show-current 2>/dev/null || true)
    ORIGIN=$(git -C "$CWD" remote get-url origin 2>/dev/null || true)
    # 저장소 안 상대 경로(모노리포 하위 서비스 식별) — 경로 이름뿐, 내용은 싣지 않는다
    SUBDIR=$(git -C "$CWD" rev-parse --show-prefix 2>/dev/null | sed 's#/$##' || true)
  else
    PROJECT=$(basename "$CWD")
  fi
fi

# ── 토큰 누적 추출 (turn_done·session_end) ────────────
# 세션 **누적** 토큰 맵 {model: {in,out,cr,cw,th}}을 stdout으로. 허브가 직전 값과의 델타만
# 가산하므로 누락된 턴은 다음 이벤트가 따라잡는다. 실패/없음이면 빈 값 — 이벤트는 그대로 전송.
# 전체 스캔이지만 jq 스트리밍이라 수십 MB 전사본도 0.1초대 (훅 예산 내).
extract_tokens() {
  [ -n "$TP" ] && [ -f "$TP" ] || return 0
  if [ "$AGENT" = "claude-code" ]; then
    # assistant 라인의 최상위 usage 필드만 모델별 합산 — iterations[]는 같은 값의 반복 기재라
    # 더하면 이중 계상. 사이드체인(서브에이전트)도 실소비이므로 포함, <synthetic>은 제외.
    # fromjson?로 라인 단위 파싱 — 마지막 라인이 쓰다 만 상태여도 나머지는 집계된다.
    jq -Rcn '
      reduce (inputs | fromjson? |
        select(type=="object" and .type=="assistant"
               and ((.message.model // "") != "<synthetic>")
               and ((.message.usage | type) == "object")) |
        {m: (.message.model // "unknown"), u: .message.usage}) as $x
      ({}; .[$x.m] = {
        "in": ((.[$x.m]["in"] // 0) + ($x.u.input_tokens // 0)),
        "out": ((.[$x.m]["out"] // 0) + ($x.u.output_tokens // 0)),
        "cr": ((.[$x.m]["cr"] // 0) + ($x.u.cache_read_input_tokens // 0)),
        "cw": ((.[$x.m]["cw"] // 0) + ($x.u.cache_creation_input_tokens // 0)),
        "th": ((.[$x.m]["th"] // 0) + ($x.u.output_tokens_details.thinking_tokens // 0))})
      | if . == {} then empty else . end
    ' "$TP" 2>/dev/null
  else
    # Codex: 마지막 token_count의 total_token_usage(세션 누적 카운터).
    # cached_input_tokens는 input_tokens의 부분집합 → 분리해서 Claude와 의미를 맞춘다.
    jq -Rcn --arg m "${MODEL:-unknown}" '
      reduce (inputs | fromjson? | .payload? |
        select(type=="object" and .type=="token_count") |
        .info.total_token_usage | select(type=="object")) as $t (null; $t)
      | if . == null then empty else
          {($m): {"in": ((((.input_tokens // 0) - (.cached_input_tokens // 0))) | if . < 0 then 0 else . end),
                  "out": (.output_tokens // 0),
                  "cr": (.cached_input_tokens // 0),
                  "cw": (.cache_write_input_tokens // 0),
                  "th": (.reasoning_output_tokens // 0)}} end
    ' "$TP" 2>/dev/null
  fi
  return 0
}

# ── 이벤트별 detail 추출 ──────────────────────────────
DETAIL="{}"
case "$EV" in
  session_start)
    DETAIL=$(printf '%s' "$IN" | jq -c --arg f "$FRONTEND" --arg m "$MODEL" --arg e "$EFFORT" \
      '{source: (.source // ""), frontend: $f, model: $m, effort: $e, collection_mode: "hooks"}' 2>/dev/null) || DETAIL="{}"
    ;;
  prompt)
    DETAIL=$(printf '%s' "$IN" | jq -c --arg f "$FRONTEND" --arg m "$MODEL" --arg e "$EFFORT" \
      '{prompt: ((.prompt // "") | .[0:600]), frontend: $f, model: $m, effort: $e, collection_mode: "hooks"}' 2>/dev/null) || DETAIL="{}"
    ;;
  heartbeat|tool_start)
    if [ "$EV" = "tool_start" ]; then
      DETAIL=$(printf '%s' "$IN" | jq -c --arg f "$FRONTEND" --arg m "$MODEL" --arg e "$EFFORT" \
        '{tool: (.tool_name // .tool // ""), frontend: $f, model: $m, effort: $e, collection_mode: "hooks"}' 2>/dev/null) || DETAIL="{}"
    else
      DETAIL=$(jq -cn --arg f "$FRONTEND" --arg m "$MODEL" --arg e "$EFFORT" \
        '{frontend: $f, model: $m, effort: $e, collection_mode: "hooks"}')
    fi
    ;;
  permission_request|idle)
    DETAIL=$(printf '%s' "$IN" | jq -c --arg f "$FRONTEND" --arg m "$MODEL" --arg e "$EFFORT" '
      {message: ((.message // .title //
        (.tool_input | if type=="object" then (.description // empty) else empty end) //
        .tool_name // "") | tostring | .[0:200]),
       frontend: $f, model: $m, effort: $e, collection_mode: "hooks"}
    ' 2>/dev/null) || DETAIL="{}"
    ;;
  turn_done)
    # 공식 필드 우선, transcript 꼬리 파싱은 폴백 (§4.2)
    # 응답 발췌 2,000자 (2026-08-28 결정) — 넘으면 머리 1,500 + 꼬리 500 (결론은 앞에, 남은 일은 끝에)
    SUMMARY=$(printf '%s' "$IN" | jq -r '(.last_assistant_message // "") | if length > 2000 then .[0:1500] + " …(중략)… " + .[-500:] else . end' 2>/dev/null) || SUMMARY=""
    if [ "$AGENT" = "claude-code" ] && [ -n "$TP" ] && [ -f "$TP" ]; then
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
          (.message.content // []) | map(select(.type=="text") | .text) | join(" ") | if length > 2000 then .[0:1500] + " …(중략)… " + .[-500:] else . end
        ' 2>/dev/null) || SUMMARY=""
      fi
    elif [ "$AGENT" = "codex-cli" ] && [ -z "$SUMMARY" ] && [ -n "$TP" ] && [ -f "$TP" ]; then
      SUMMARY=$(tail -n 80 "$TP" 2>/dev/null | jq -rs '
        [.[] | select(.type=="response_item" and .payload.type=="message" and .payload.role=="assistant")] | last |
        [.payload.content[]? | select(.type=="output_text") | .text] | join(" ") | if length > 2000 then .[0:1500] + " …(중략)… " + .[-500:] else . end
      ' 2>/dev/null) || SUMMARY=""
    fi
    DETAIL=$(jq -cn --arg s "${SUMMARY:-}" --arg m "$MODEL" --arg e "$EFFORT" --arg f "$FRONTEND" \
      '{summary: $s, model: $m, effort: $e, frontend: $f, collection_mode: "hooks"}')
    ;;
  session_end)
    DETAIL=$(printf '%s' "$IN" | jq -c --arg f "$FRONTEND" --arg m "$MODEL" --arg e "$EFFORT" \
      '{reason: (.reason // ""), frontend: $f, model: $m, effort: $e, collection_mode: "hooks"}' 2>/dev/null) || DETAIL="{}"
    ;;
esac
[ -z "$DETAIL" ] && DETAIL="{}"

# 턴 경계 이벤트에는 세션 누적 토큰을 싣는다 (구형 허브는 미지 키를 무시하므로 호환)
case "$EV" in
  turn_done|session_end)
    TOKCUM=$(extract_tokens) || TOKCUM=""
    if [ -n "$TOKCUM" ]; then
      MERGED=$(printf '%s' "$DETAIL" | jq -c --argjson t "$TOKCUM" \
        '. + {tokens: {v: 1, cum: $t}}' 2>/dev/null) && [ -n "$MERGED" ] && DETAIL="$MERGED"
    fi
    ;;
esac

EID=""
if [ "$AGENT" = "codex-cli" ] && [ -n "$TURN_ID" ]; then
  case "$EV" in
    prompt|turn_done) EID="codex-$SID-$TURN_ID-$EV" ;;
    tool_start|heartbeat)
      [ -n "$TOOL_USE_ID" ] && EID="codex-$SID-$TURN_ID-$EV-$TOOL_USE_ID"
      ;;
  esac
fi
[ -n "$EID" ] || EID=$(uuidgen 2>/dev/null || echo "$SID-$TS-$EV-$$")
EVJSON=$(jq -cn \
  --arg agent "$AGENT" --arg sid "$SID" --arg ev "$EV" --arg ts "$TS" \
  --arg eid "$EID" \
  --arg project "$PROJECT" --arg branch "$BRANCH" --arg origin "$ORIGIN" --arg subdir "$SUBDIR" \
  --argjson detail "$DETAIL" \
  '{agent:$agent, session_id:$sid, event:$ev, ts:$ts, event_id:$eid,
    project:$project, branch:$branch, origin:$origin, subdir:$subdir, detail:$detail}' 2>/dev/null) || exit 0

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
        # 상태는 바꾸지 않는다 — 수신됨(delivered)은 사용자가 착수를 지시할 때 pickup 절차가 찍는다 (§9).
        # 그래서 픽업 전까지는 새 세션마다(에이전트 무관) 같은 안내가 반복 주입된다.
        printf '%s' "$HLIST" | jq -r '.[] |
          "[MADISON] \(.from_name // "다른 기기")발 핸드오프 \(.hf) 대기 중: \(.summary // "요약 없음") " +
          "(리포 \(.repo // "?"), 브랜치 \(.branch // "?")). " +
          "이어받을지 사용자에게 확인하고, 승인하면 pickup 스킬(/pickup) 절차로 진행하세요."' 2>/dev/null
      fi
      ;;
  esac
fi

exit 0
