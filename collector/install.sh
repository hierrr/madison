#!/bin/bash
# MADISON collector 설치 — 멱등 (IMPLEMENTATION.md §8.3)
# 사용: curl -fsSL https://madison-api.example.com/install.sh | bash -s -- --name studio --secret <등록암호>
# 옵션: --hub <URL> (허브 기기 자신은 http://127.0.0.1:8787), --no-codex (Codex 수집 제외)
# Claude Code(CLI·데스크탑앱 공통 전역 훅)와 Codex(notify 체이닝) 수집이 모두 기본이다.
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

# ── 5) Codex 수집 (기본 — --no-codex로 제외) ──
if [ "$WITH_CODEX" = "1" ] && [ ! -f "$HOME/.codex/config.toml" ]; then
  note "Codex 설정(~/.codex/config.toml) 없음 — Codex 수집 건너뜀 (Codex 로그인 후 재실행하면 자동 구성)"
fi
if [ "$WITH_CODEX" = "1" ] && [ -f "$HOME/.codex/config.toml" ]; then
  fetch codex-notify-wrapper.sh "$MAD_DIR/codex-notify-wrapper.sh"
  fetch codex-watch.sh "$MAD_DIR/bin/madison-codex-watch"
  chmod 700 "$MAD_DIR/codex-notify-wrapper.sh" "$MAD_DIR/bin/madison-codex-watch"
  python3 - "$HOME/.codex/config.toml" "$MAD_DIR" <<'PY'
import json, re, sys, time, tomllib
from pathlib import Path

cfg_path, mad_dir = Path(sys.argv[1]), Path(sys.argv[2])
text = cfg_path.read_text()
cfg = tomllib.loads(text)
wrapper = str(mad_dir / "codex-notify-wrapper.sh")
orig = cfg.get("notify")
orig_file = mad_dir / "codex-orig-notify.json"

if orig == [wrapper]:
    print("[MADISON] Codex notify 이미 체이닝됨 — 변경 없음")
elif orig:
    # 원본 명령 보존(체이닝 대상) 후 래퍼로 교체 (§4.3)
    if not orig_file.exists():
        orig_file.write_text(json.dumps(orig, ensure_ascii=False))
    backup = cfg_path.with_name(f"config.toml.bak-madison-{time.strftime('%Y%m%d%H%M%S')}")
    backup.write_text(text)
    new_line = f'notify = ["{wrapper}"]'
    # notify는 여러 줄 배열일 수 있다 — 다음 최상위 키(줄머리 word=) 또는 EOF까지 전부 치환
    pat = re.compile(r"(?ms)^notify\s*=.*?(?=^\s*[A-Za-z0-9_.\[]|\Z)")
    if pat.search(text):
        text = pat.sub(new_line + "\n", text, count=1)
    else:
        text = re.sub(r"(?m)^notify\s*=.*$", new_line, text, count=1)
    cfg_path.write_text(text)
    print(f"[MADISON] Codex notify 체이닝 설치 (원본은 {orig_file.name}에 보존, config 백업 생성)")
else:
    orig_file.write_text("[]")
    with cfg_path.open("a") as f:
        f.write(f'\nnotify = ["{wrapper}"]\n')
    print("[MADISON] Codex notify 신규 설정")
PY
  if [ "$(uname)" = "Darwin" ]; then
    WPLIST="$HOME/Library/LaunchAgents/dev.madison.codexwatch.plist"
    cat > "$WPLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.madison.codexwatch</string>
  <key>ProgramArguments</key><array>
    <string>$MAD_DIR/bin/madison-codex-watch</string>
  </array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$MAD_DIR/codex-watch.log</string>
  <key>StandardErrorPath</key><string>$MAD_DIR/codex-watch.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/dev.madison.codexwatch" 2>/dev/null || true
    sleep 1
    launchctl bootstrap "gui/$(id -u)" "$WPLIST" 2>/dev/null \
      || { sleep 1; launchctl bootstrap "gui/$(id -u)" "$WPLIST"; }
    note "Codex history watcher 등록 (60초 주기)"
  fi
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
note "  · 이미 열려 있는 Claude Code 세션은 재시작해야 훅이 적용됩니다"
note "  · 대시보드: https://madison.example.com (Access 로그인)"
