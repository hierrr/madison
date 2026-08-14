#!/bin/bash
# MADISON launchd 래퍼 — 스텁 위치 기준으로 저장소 루트를 찾아 .venv python으로 실행.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR" || exit 1
exec "$DIR/.venv/bin/python" -m server "$@"
