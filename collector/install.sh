#!/bin/bash
# MADISON collector 설치 — 멱등 (IMPLEMENTATION.md §8.3)
# 사용: curl -fsSL https://madison-api.example.com/install.sh | bash -s -- --name studio --secret <등록암호>
# 옵션: --hub <URL> (허브 기기 자신은 http://127.0.0.1:8787), --no-codex (Codex 수집 제외)
# Claude Code와 Codex(CLI·데스크톱 앱 공통 lifecycle hooks) 수집이 모두 기본이다.
set -euo pipefail

HUB="https://madison-api.example.com"
NAME=""
SECRET="${MADISON_ENROLL_SECRET:-}"
WITH_CODEX=1
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --secret) SECRET="$2"; shift 2 ;;
    --hub) HUB="$2"; shift 2 ;;
    --with-codex) WITH_CODEX=1; shift ;;   # 하위호환 — 이제 기본값
    --no-codex) WITH_CODEX=0; shift ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

MAD_DIR="$HOME/.claude/madison"
ENV_FILE="$MAD_DIR/env"
SETTINGS="$HOME/.claude/settings.json"
SKILLS_DIR="$HOME/.claude/skills"
note() { printf '\033[36m[MADISON]\033[0m %s\n' "$*"; }

# 재설치/갱신: --hub 미지정이면 이미 등록된 env의 MADISON_URL을 기본값으로 쓴다
if [ "$HUB" = "https://madison-api.example.com" ] && [ -f "$ENV_FILE" ]; then
  SAVED_URL=$(sed -n 's/^MADISON_URL=//p' "$ENV_FILE" | head -1)
  [ -n "$SAVED_URL" ] && HUB="$SAVED_URL"
fi

command -v jq >/dev/null 2>&1 || { echo "jq가 필요합니다: brew install jq" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3가 필요합니다" >&2; exit 1; }

mkdir -p "$MAD_DIR/throttle" "$MAD_DIR/locks" "$MAD_DIR/bin" "$SKILLS_DIR/handoff" "$SKILLS_DIR/pickup"

# ── 1) 파일 배치 (로컬 체크아웃이면 복사, 아니면 허브에서 다운로드) ──
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || echo /nonexistent)"
fetch() { # $1 상대경로 → $2 대상
  if [ -f "$SRC_DIR/$1" ]; then
    cp "$SRC_DIR/$1" "$2"
  else
    curl -fsSL -m 10 "$HUB/collector/$1" -o "$2"
  fi
}
# launchd 잡의 실행 파일명 = 로그인 항목 표시 이름이므로, 잡 스크립트는 madison-* 스텁명으로 배치한다
fetch report.sh "$MAD_DIR/report.sh"
fetch flush.sh "$MAD_DIR/bin/madison-flush"
fetch skills/handoff/SKILL.md "$SKILLS_DIR/handoff/SKILL.md"
fetch skills/pickup/SKILL.md "$SKILLS_DIR/pickup/SKILL.md"
TMPL="$(mktemp)"; fetch hooks.template.json "$TMPL"
chmod 700 "$MAD_DIR/report.sh" "$MAD_DIR/bin/madison-flush"
note "collector 파일 배치 완료"

# ── 2) 등록 — env에 토큰이 이미 있으면 생략 (§8.3 멱등) ──
if [ -f "$ENV_FILE" ] && grep -q '^MADISON_TOKEN=..*' "$ENV_FILE" 2>/dev/null; then
  note "이미 등록된 기기 — enroll 생략"
else
  [ -n "$NAME" ] || { echo "--name <기기명> 필요" >&2; exit 1; }
  [ -n "$SECRET" ] || { echo "--secret <등록암호> 또는 MADISON_ENROLL_SECRET 필요" >&2; exit 1; }
  RESP=$(curl -fsS -m 10 -H 'Content-Type: application/json' -X POST "$HUB/api/enroll" \
    --data-binary "$(jq -cn --arg n "$NAME" --arg s "$SECRET" '{name:$n, secret:$s}')") \
    || { echo "등록 실패 — 허브 응답 없음 또는 거절" >&2; exit 1; }
  TOKEN=$(printf '%s' "$RESP" | jq -r '.token // empty')
  [ -n "$TOKEN" ] || { echo "등록 실패: $RESP" >&2; exit 1; }
  umask 177
  {
    echo "MADISON_URL=$HUB"
    echo "MADISON_TOKEN=$TOKEN"
    echo "MADISON_DEVICE=$NAME"
    echo "MADISON_DEBUG=0"
  } > "$ENV_FILE"
  umask 022
  note "기기 '$NAME' 등록 완료 (토큰은 $ENV_FILE, chmod 600)"
fi

# ── 3) 전역 훅 병합 — 이미 동일하면 no-op, 다른 도구 훅은 보존 ──
python3 - "$SETTINGS" "$TMPL" <<'PY'
import json, sys, time
from pathlib import Path

settings_path, tmpl_path = Path(sys.argv[1]), Path(sys.argv[2])
settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
tmpl = json.loads(tmpl_path.read_text())["hooks"]

hooks = settings.setdefault("hooks", {})
changed = False
for event, entries in tmpl.items():
    existing = hooks.setdefault(event, [])
    for entry in entries:
        cmd = entry["hooks"][0]["command"]
        present = any(
            h.get("command") == cmd
            for e in existing if isinstance(e, dict)
            for h in e.get("hooks", [])
        )
        if not present:
            existing.append(entry)
            changed = True

if changed:
    if settings_path.exists():
        backup = settings_path.with_name(f"settings.json.bak-madison-{time.strftime('%Y%m%d%H%M%S')}")
        backup.write_text(settings_path.read_text())
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print("[MADISON] 전역 훅 설치됨 (기존 설정은 .bak-madison-* 백업)")
else:
    print("[MADISON] 전역 훅 이미 최신 — 변경 없음")
PY
rm -f "$TMPL"

# ── 4) 스풀 플러셔 launchd 잡 (macOS) ──
if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/dev.madison.flush.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.madison.flush</string>
  <key>ProgramArguments</key><array>
    <string>$MAD_DIR/bin/madison-flush</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$MAD_DIR/flush.log</string>
  <key>StandardErrorPath</key><string>$MAD_DIR/flush.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/dev.madison.flush" 2>/dev/null || true
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
    || { sleep 1; launchctl bootstrap "gui/$(id -u)" "$PLIST"; }
  note "스풀 플러셔 등록 (dev.madison.flush, 5분 주기)"
fi

# ── 5) Codex lifecycle hooks (기본 — --no-codex로 제외) ──
if [ "$WITH_CODEX" = "1" ]; then
  CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
  CODEX_HOOKS="$CODEX_DIR/hooks.json"
  mkdir -p "$CODEX_DIR"
  CTMPL="$(mktemp)"; fetch codex-hooks.template.json "$CTMPL"

  # Codex에서도 /handoff·/pickup을 쓸 수 있게 같은 스킬(SKILL.md 호환)을 설치한다 — 에이전트 선택은 사용자 몫.
  mkdir -p "$CODEX_DIR/skills/handoff" "$CODEX_DIR/skills/pickup"
  cp "$SKILLS_DIR/handoff/SKILL.md" "$CODEX_DIR/skills/handoff/SKILL.md"
  cp "$SKILLS_DIR/pickup/SKILL.md" "$CODEX_DIR/skills/pickup/SKILL.md"
  note "Codex에도 /handoff·/pickup 스킬 설치"

  # 기존 사용자 훅을 보존하며 MADISON 훅만 멱등 병합한다.
  python3 - "$CODEX_HOOKS" "$CTMPL" <<'PY'
import json, sys, time, tomllib
from pathlib import Path

hooks_path, tmpl_path = Path(sys.argv[1]), Path(sys.argv[2])
doc = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
tmpl = json.loads(tmpl_path.read_text())
hooks = doc.setdefault("hooks", {})
changed = False

for event, entries in tmpl["hooks"].items():
    existing = hooks.setdefault(event, [])
    for entry in entries:
        wanted = entry["hooks"][0]
        cmd = wanted["command"]
        found = None
        for group in existing:
            if not isinstance(group, dict):
                continue
            for handler in group.get("hooks", []):
                if isinstance(handler, dict) and handler.get("command") == cmd:
                    found = handler
                    break
            if found is not None:
                break
        if found is None:
            existing.append(entry)
            changed = True
        elif found != wanted:
            found.clear()
            found.update(wanted)
            changed = True

if changed:
    if hooks_path.exists():
        backup = hooks_path.with_name(f"hooks.json.bak-madison-{time.strftime('%Y%m%d%H%M%S')}")
        backup.write_text(hooks_path.read_text())
    hooks_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print("[MADISON] Codex lifecycle hooks 설치됨 (기존 훅 보존)")
else:
    print("[MADISON] Codex lifecycle hooks 이미 최신 — 변경 없음")

config_path = hooks_path.with_name("config.toml")
if config_path.exists():
    try:
        # hooks.state는 Codex의 신뢰 해시 저장소일 뿐 훅 정의가 아니다 — 그 외 키가 있을 때만 안내
        if any(k != "state" for k in tomllib.loads(config_path.read_text()).get("hooks", {})):
            print("[MADISON] 참고: config.toml inline hooks도 있어 Codex가 두 소스를 병합합니다")
    except tomllib.TOMLDecodeError:
        pass
PY
  rm -f "$CTMPL"

  # 구버전 MADISON notify 체이닝을 제거하고, 설치 전에 보존했던 사용자 notify를 복원한다.
  # bare notify를 TOML 끝에 붙여 다른 테이블 안으로 들어갔던 과거 설치 오류도 함께 정리한다.
  if [ -f "$CODEX_DIR/config.toml" ]; then
    python3 - "$CODEX_DIR/config.toml" "$MAD_DIR" <<'PY'
import json, re, sys, time, tomllib
from pathlib import Path

cfg_path, mad_dir = Path(sys.argv[1]), Path(sys.argv[2])
original_text = cfg_path.read_text()
lines = original_text.splitlines(keepends=True)
# 래퍼 경로는 다른 프로그램(Computer Use 등)이 --previous-notify 인자로 JSON 이스케이프(\/)해
# 품고 있을 수 있어 전체 경로 대신 파일명으로 매칭한다 — 파일명은 MADISON 고유라 오탐 없음.
kept = [line for line in lines if not (re.match(r"^\s*notify\s*=", line) and "codex-notify-wrapper.sh" in line)]
text = "".join(kept)

orig_file = mad_dir / "codex-orig-notify.json"
saved = []
if orig_file.exists():
    try:
        value = json.loads(orig_file.read_text())
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            saved = value
    except (json.JSONDecodeError, OSError):
        pass

parsed = tomllib.loads(text)
if saved and not parsed.get("notify"):
    notify_line = "notify = " + json.dumps(saved, ensure_ascii=False) + "\n"
    current = text.splitlines(keepends=True)
    insert_at = next((i for i, line in enumerate(current) if line.lstrip().startswith("[")), len(current))
    current.insert(insert_at, notify_line)
    text = "".join(current)

# 쓰기 전에 최종 TOML을 다시 검증한다.
tomllib.loads(text)
if text != original_text:
    backup = cfg_path.with_name(f"config.toml.bak-madison-{time.strftime('%Y%m%d%H%M%S')}")
    backup.write_text(original_text)
    cfg_path.write_text(text)
    print("[MADISON] 기존 Codex notify 체이닝 제거 및 사용자 notify 복원 (config 백업 생성)")
PY
  fi

  # 60초 history watcher는 lifecycle hooks와 중복되므로 정확한 기존 MADISON 작업만 제거한다.
  if [ "$(uname)" = "Darwin" ]; then
    launchctl bootout "gui/$(id -u)/dev.madison.codexwatch" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/dev.madison.codexwatch.plist"
  fi
  rm -f "$MAD_DIR/bin/madison-codex-watch" "$MAD_DIR/codex-notify-wrapper.sh"

  if command -v codex >/dev/null 2>&1; then
    if codex features list 2>/dev/null | awk '$1 == "hooks" && $3 == "true" { found=1 } END { exit !found }'; then
      note "Codex hooks 기능 확인됨"
    else
      note "경고: 이 Codex 버전에서 hooks 활성화를 확인하지 못함 — Codex 업데이트 필요"
    fi
  fi
  note "Codex에서 /hooks를 열어 새 MADISON command hooks를 검토·신뢰해야 합니다"
fi

# ── 6) 검증 + 안내 ──
. "$ENV_FILE"
CODE=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $MADISON_TOKEN" "$MADISON_URL/api/state" 2>/dev/null || echo 000)
if [ "$CODE" = "200" ]; then
  note "허브 연결 확인 (GET /api/state → 200)"
else
  note "경고: 허브 연결 확인 실패 (code=$CODE) — 네트워크/토큰 확인 필요"
fi
note "설치 완료. 유의사항:"
note "  · 이미 열려 있는 Claude Code/Codex 세션은 재시작해야 새 훅이 적용됩니다"
note "  · Codex CLI에서 /hooks를 열어 MADISON 훅을 신뢰하세요"
note "  · 대시보드: https://madison.example.com (Access 로그인)"
