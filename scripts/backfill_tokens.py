#!/usr/bin/env python3
"""과거 전사본 → token_daily 백필 (허브 로컬, 재실행 가능).

허브 기기에 남아 있는 Claude Code 전사본(~/.claude/projects, 약 30일 보존)과 Codex 롤아웃
(~/.codex/sessions, 장기 보존)을 스캔해 일별 토큰 집계를 source='backfill'로 넣는다.

  python3 scripts/backfill_tokens.py --device <기기명> [--dry-run] [--until YYYY-MM-DD]

- 멱등: 시작 시 해당 기기의 backfill 행을 지우고 다시 넣는다.
- 이중 계상 방지: 라이브 수집(source='events')의 에이전트별 최초 day 이후는 넣지 않는다.
  --until로 더 이른 경계를 줄 수 있다(경계 미포함).
- 프로젝트 귀속은 cwd의 basename(최선 노력) — Claude는 라인별 cwd, Codex는 session_meta.cwd.
- 턴 수는 복원하지 않는다(turns=0) — 합계 턴은 라이브 수집분에서만.
"""
import argparse
import functools
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import db, tokens  # noqa: E402


@functools.lru_cache(maxsize=4096)
def proj_of(cwd: str) -> str:
    """cwd → 프로젝트명 — 라이브 수집기(report.sh)와 같은 규칙: 디렉터리가 아직 있으면
    git 최상위 basename(서브디렉터리 세션이 리포 이름으로 귀속), 없으면 basename."""
    if not cwd:
        return ""
    p = Path(cwd)
    if p.is_dir():
        try:
            done = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=5, check=False)
            top = done.stdout.strip()
            if done.returncode == 0 and top:
                return Path(top).name
        except (OSError, subprocess.SubprocessError):
            pass
    return p.name

CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"

# report.sh의 originator → frontend 매핑과 동일
FRONTEND_OF = {"codex_exec": "auto", "exec": "auto", "codex-tui": "cli", "cli": "cli",
               "Codex Desktop": "app", "codex-desktop": "app", "codex_desktop": "app"}


def _lines(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def scan_claude(root: Path, until: str) -> dict:
    """{(day, project, model, session_id): {kind: n}} — 라인별 usage 합산.
    session_id는 허브 sessions 테이블의 frontend(자동화 여부)와 조인하기 위한 키 —
    remap_claude()가 frontend로 바꾼다."""
    agg = defaultdict(lambda: {k: 0 for k in tokens.KINDS})
    for path in sorted(root.glob("*/*.jsonl")):
        for ev in _lines(path):
            if not isinstance(ev, dict) or ev.get("type") != "assistant":
                continue
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            model = str(msg.get("model") or "unknown")
            if model == "<synthetic>":
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            day = tokens._day_local(str(ev.get("timestamp") or ""), "")
            if day >= until:
                continue
            project = proj_of(str(ev.get("cwd") or ""))
            acc = agg[(day, project, model[:60], str(ev.get("sessionId") or ""))]
            acc["in"] += int(u.get("input_tokens") or 0)
            acc["out"] += int(u.get("output_tokens") or 0)
            acc["cr"] += int(u.get("cache_read_input_tokens") or 0)
            acc["cw"] += int(u.get("cache_creation_input_tokens") or 0)
            details = u.get("output_tokens_details")
            acc["th"] += int(details.get("thinking_tokens") or 0) if isinstance(details, dict) else 0
    return agg


def remap_claude(c, device_id: int, agg: dict) -> dict:
    """세션 키 → frontend 키. 허브 sessions에 그 세션이 있으면 그 frontend(자동화 세션이면
    'auto'), 없으면 워커 프로젝트 규칙(auto_frontend)만 적용."""
    out = defaultdict(lambda: {k: 0 for k in tokens.KINDS})
    cache: dict = {}
    for (day, project, model, sid), v in agg.items():
        if sid not in cache:
            row = c.execute("SELECT frontend FROM sessions WHERE device_id=? AND agent='claude-code'"
                            " AND session_id=?", (device_id, sid)).fetchone() if sid else None
            cache[sid] = (row["frontend"] or "") if row else ""
        fe = tokens.auto_frontend(project, cache[sid])
        acc = out[(day, project, model, fe)]
        for k in tokens.KINDS:
            acc[k] += v[k]
    return out


def scan_codex(root: Path, until: str) -> dict:
    """{(day, project, model, frontend): {kind: n}} — token_count 누적 카운터의 연속 델타."""
    agg = defaultdict(lambda: {k: 0 for k in tokens.KINDS})
    for path in sorted(root.rglob("*.jsonl")):
        project, frontend, model = "", "", "unknown"
        prev = {k: 0 for k in tokens.KINDS}
        for ev in _lines(path):
            if not isinstance(ev, dict):
                continue
            pl = ev.get("payload")
            if not isinstance(pl, dict):
                continue
            if ev.get("type") == "session_meta":
                project = proj_of(str(pl.get("cwd") or ""))
                frontend = FRONTEND_OF.get(str(pl.get("originator") or ""), "")
                continue
            if ev.get("type") == "turn_context" and pl.get("model"):
                model = str(pl.get("model"))[:60]
                continue
            if pl.get("type") != "token_count":
                continue
            info = pl.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(total, dict):
                continue
            raw_in = int(total.get("input_tokens") or 0)
            cached = int(total.get("cached_input_tokens") or 0)
            cur = {"in": max(0, raw_in - cached), "out": int(total.get("output_tokens") or 0),
                   "cr": cached, "cw": int(total.get("cache_write_input_tokens") or 0),
                   "th": int(total.get("reasoning_output_tokens") or 0)}
            delta = tokens._delta(prev, cur)
            prev = cur
            if not any(delta.values()):
                continue
            day = tokens._day_local(str(ev.get("timestamp") or ""), "")
            if day >= until:
                continue
            acc = agg[(day, project, model, tokens.auto_frontend(project, frontend))]
            for k in tokens.KINDS:
                acc[k] += delta[k]
    return agg


def cutoff_for(c, device_id: int, agent: str, until: str | None) -> str:
    row = c.execute("SELECT MIN(day) d FROM token_daily WHERE source='events' AND device_id=? AND agent=?",
                    (device_id, agent)).fetchone()
    live_start = row["d"] or "9999-12-31"
    return min(until, live_start) if until else live_start


def report_table(agg: dict) -> str:
    per_day = defaultdict(lambda: {k: 0 for k in tokens.KINDS})
    for (day, _p, _m, _f), v in agg.items():
        for k in tokens.KINDS:
            per_day[day][k] += v[k]
    lines = [f"  {'day':10}  {'input':>12}  {'output':>12}  {'cache_r':>14}  {'cache_w':>12}"]
    for day in sorted(per_day):
        v = per_day[day]
        lines.append(f"  {day:10}  {v['in']:>12,}  {v['out']:>12,}  {v['cr']:>14,}  {v['cw']:>12,}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", required=True, help="이 전사본들을 귀속시킬 기기 이름(devices.name)")
    ap.add_argument("--until", help="이 날짜 이전까지만(YYYY-MM-DD, 미포함). 기본: 라이브 수집 시작일")
    ap.add_argument("--dry-run", action="store_true", help="요약만 출력하고 쓰지 않음")
    ap.add_argument("--from-json", type=Path,
                    help="collector/collect_tokens_local.py가 만든 집계 JSON에서 넣기 (전사본 스캔 대신 — 핸드오프 운반용)")
    ap.add_argument("--claude-dir", type=Path, default=CLAUDE_DIR)
    ap.add_argument("--codex-dir", type=Path, default=CODEX_DIR)
    args = ap.parse_args()

    with db.tx() as c:
        row = c.execute("SELECT id FROM devices WHERE name=? AND revoked=0", (args.device,)).fetchone()
        if not row:
            sys.exit(f"기기 없음: {args.device} (등록된 이름은 sqlite3 data/madison.db 'SELECT name FROM devices')")
        device_id = row["id"]
        plans = []
        if args.from_json:
            payload = json.loads(args.from_json.read_text(encoding="utf-8"))
            if payload.get("device") and payload["device"] != args.device:
                sys.exit(f"JSON의 기기({payload['device']})와 --device({args.device})가 다름 — 확인 후 다시")
            by_agent: dict = {}
            for r in payload.get("rows", []):
                proj = str(r.get("project") or "")
                key = (str(r.get("day") or ""), proj, str(r.get("model") or "")[:60],
                       tokens.auto_frontend(proj, str(r.get("frontend") or "")))
                acc = by_agent.setdefault(str(r.get("agent") or ""), {}).setdefault(
                    key, {k: 0 for k in tokens.KINDS})
                for k in tokens.KINDS:   # auto 재분류로 키가 합쳐질 수 있어 가산
                    acc[k] += int(r.get(k) or 0)
            for agent in ("claude-code", "codex-cli"):
                until = cutoff_for(c, device_id, agent, args.until)
                agg = {k: v for k, v in by_agent.get(agent, {}).items() if k[0] < until}
                plans.append((agent, until, agg))
                print(f"\n== {agent} (until {until}, {len(agg)}개 집계 키, {args.from_json})")
                print(report_table(agg) if agg else "  (없음)")
        else:
            for agent, scan, root in (("claude-code", scan_claude, args.claude_dir),
                                      ("codex-cli", scan_codex, args.codex_dir)):
                until = cutoff_for(c, device_id, agent, args.until)
                agg = scan(root, until) if root.is_dir() else {}
                if agent == "claude-code":
                    agg = remap_claude(c, device_id, agg)   # 세션 키 → frontend (허브 sessions 조인)
                plans.append((agent, until, agg))
                print(f"\n== {agent} (until {until}, {len(agg)}개 집계 키, {root})")
                print(report_table(agg) if agg else "  (없음)")
        if args.dry_run:
            print("\n--dry-run: 쓰지 않음")
            return
        c.execute("DELETE FROM token_daily WHERE source='backfill' AND device_id=?", (device_id,))
        n = 0
        for agent, _until, agg in plans:
            for (day, project, model, frontend), v in agg.items():
                c.execute(
                    "INSERT INTO token_daily (day, device_id, agent, project, model, frontend, source,"
                    " input, output, cache_read, cache_write, thinking, turns)"
                    " VALUES (?,?,?,?,?,?,'backfill',?,?,?,?,?,0)"
                    " ON CONFLICT(day, device_id, agent, project, model, frontend, source) DO UPDATE SET"
                    " input=excluded.input, output=excluded.output, cache_read=excluded.cache_read,"
                    " cache_write=excluded.cache_write, thinking=excluded.thinking",
                    (day, device_id, agent, project[:120], model, frontend,
                     v["in"], v["out"], v["cr"], v["cw"], v["th"]))
                n += 1
        print(f"\n기록 완료: {n}행 (source='backfill', device={args.device})")


if __name__ == "__main__":
    main()
