#!/bin/bash
# MADISON 스풀 플러셔 — launchd가 5분마다 실행. 허브 다운 중 쌓인 이벤트를
# 새 훅 발화 없이도 재전송한다 (IMPLEMENTATION.md §7.1).
exec "$HOME/.claude/madison/report.sh" __flush
