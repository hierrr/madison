#!/usr/bin/env python3
"""과거 전사본 → token_daily 백필 (허브 로컬, 재실행 가능).

일 원장(token_daily)에만 쓴다. 시간 원장(token_hourly)에는 쓰지 않는다 — 시간 원장은 1·7일 롤링 창
전용인데 백필분은 라이브 수집 시작일 이전(전사본이 보존된 과거)이라 그 창에 잡힐 일이 없고, 전사본 줄에서
복원한 시각도 세션 판정 없이 추정한 값이라 시간 단위 정확도를 약속할 수 없다. 그래서 라이브 쓰기 경로
tokens.add_ledger(일·시간 동시 가산)를 거치지 않고 아래 전용 UPSERT(교체, 멱등)를 쓴다.

허브 기기에 남아 있는 Claude Code 전사본(~/.claude/projects, 약 30일 보존)과 Codex 롤아웃
(~/.codex/sessions, 장기 보존)을 스캔해 일별 토큰 집계를 source='backfill'로 넣는다.

  python3 scripts/backfill_tokens.py --device <기기명> [--dry-run] [--until YYYY-MM-DD]

- 멱등: 시작 시 해당 기기의 backfill 행을 지우고 다시 넣는다.
- 이중 계상 방지: 라이브 수집(source='events')의 에이전트별 최초 day 이후는 넣지 않는다.
  --until로 더 이른 경계를 줄 수 있다(경계 미포함).
- 자동화 판정(resolve_frontend 하나): 허브 sessions에 그 세션이 있으면 그 세션의 frontend 판정, 없으면
  워커 프로젝트 이름 규칙(tokens.auto_frontend(project, 수집기 값)). auto로 판정된 워커 디렉터리 이름은 원장에
  tokens.HUB_PROJECT로 적는다(tokens.ledger_project — 라이브 워커 계상·migrate 병합과 같은 규칙).
  세션 id를 얻는 경로: Claude는 줄의 sessionId, Codex는 session_meta.payload.id(허브 session_id와 같은 값을
  실데이터에서 확인). --from-json은 수집기가 날짜·프로젝트·모델·frontend로 이미 집계해 세션 id가 없어 이름 규칙(+수집기 값)만 쓴다 — 세션 조인 불가.
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
    """{(day, project, model, session_id, 수집기 frontend): {kind: n}} — 라인별 usage 합산.
    session_id는 허브 sessions 테이블의 frontend(자동화 여부)와 조인하기 위한 키 —
    remap_frontend()가 frontend로 바꾼다. Claude 전사본에는 frontend 정보가 없어 수집기 값은 항상 ''."""
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
            day = tokens.local_hour(str(ev.get("timestamp") or ""))[:10]
            if day >= until:
                continue
            project = proj_of(str(ev.get("cwd") or ""))
            acc = agg[(day, project, model[:60], str(ev.get("sessionId") or ""), "")]
            for k, v in tokens.claude_counts(u).items():   # 분류 규칙의 정본은 tokens.py
                acc[k] += v
    return agg


def resolve_frontend(c, device_id: int, agent: str, session_id: str, project: str, collected: str,
                     cache: dict | None = None) -> str:
    """백필이 쓰는 frontend(자동화 여부) 판정의 유일한 경로 — 모든 입력 경로(Claude 전사본·Codex 롤아웃·
    --from-json)가 이 함수를 거친다. 허브 sessions에 그 세션이 있고 판정이 있으면 그 판정(auto 포함)을 그대로
    따른다. 세션 기록이 없거나(session_id가 없는 경로 포함) 판정이 비어 있으면('' / 'unknown') 이름 규칙
    tokens.auto_frontend(project, 수집기 값)."""
    if session_id:
        cache = cache if cache is not None else {}
        key = (device_id, agent, session_id)
        if key not in cache:
            row = c.execute("SELECT frontend FROM sessions WHERE device_id=? AND agent=? AND session_id=?",
                            key).fetchone()
            cache[key] = (row["frontend"] or "") if row else ""
        if cache[key] not in ("", "unknown"):
            return cache[key]
    return tokens.auto_frontend(project, collected)


def remap_frontend(c, device_id: int, agent: str, agg: dict) -> dict:
    """스캐너 집계 {(day, project, model, session_id, 수집기 frontend): n} → {(day, project, model, frontend): n}."""
    out = defaultdict(lambda: {k: 0 for k in tokens.KINDS})
    cache: dict = {}
    for (day, project, model, sid, collected), v in agg.items():
        fe = resolve_frontend(c, device_id, agent, sid, project, collected, cache)
        acc = out[(day, tokens.ledger_project(project, fe), model, fe)]   # 워커 이름 → HUB_PROJECT (auto일 때만)
        for k in tokens.KINDS:
            acc[k] += v[k]
    return out


def scan_codex(root: Path, until: str) -> dict:
    """{(day, project, model, session_id, 수집기 frontend): {kind: n}} — token_count 누적 카운터의 연속 델타.
    session_id는 session_meta.payload.id(허브 session_id와 같은 값) — remap_frontend()가 세션 판정을 조인한다."""
    agg = defaultdict(lambda: {k: 0 for k in tokens.KINDS})
    for path in sorted(root.rglob("*.jsonl")):
        project, frontend, model, sid = "", "", "unknown", ""
        prev = {k: 0 for k in tokens.KINDS}
        for ev in _lines(path):
            if not isinstance(ev, dict):
                continue
            pl = ev.get("payload")
            if not isinstance(pl, dict):
                continue
            if ev.get("type") == "session_meta":
                project = proj_of(str(pl.get("cwd") or ""))
                sid = str(pl.get("id") or "")
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
            cur = tokens.codex_counts(total)   # 분류 규칙의 정본은 tokens.py
            delta = tokens._delta(prev, cur)
            prev = cur
            if not any(delta.values()):
                continue
            day = tokens.local_hour(str(ev.get("timestamp") or ""))[:10]
            if day >= until:
                continue
            acc = agg[(day, project, model, sid, frontend)]
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
                # 집계 JSON에는 세션 id가 없다 — 세션 조인 불가, resolve_frontend의 이름 규칙 경로만 탄다
                fe = resolve_frontend(c, device_id, str(r.get("agent") or ""), "", proj,
                                      str(r.get("frontend") or ""))
                key = (str(r.get("day") or ""), tokens.ledger_project(proj, fe), str(r.get("model") or "")[:60], fe)
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
                agg = remap_frontend(c, device_id, agent, agg)   # 세션 키 → frontend (허브 sessions 조인)
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
