#!/usr/bin/env python3
"""기기 로컬 전사본 → 토큰 일별 집계 JSON (MADISON 백필 운반용, 표준 라이브러리만).

허브 DB가 없는 기기에서 실행해 집계 JSON 파일을 만들고, 핸드오프 문서에 실어 허브 기기로
보낸 뒤 허브에서 `scripts/backfill_tokens.py --device <기기명> --from-json <파일>`로 넣는다.

  curl -fsSL https://<허브 API 호스트>/collector/collect_tokens_local.py | python3 - [--out FILE]

집계 단위는 일 × 에이전트 × 프로젝트(cwd basename) × 모델 × frontend — 원문은 어떤 내용도
싣지 않는다(토큰 수와 경로 이름뿐). 서버 모듈에 의존하지 않도록 판정 로직을 자체 포함한다
(정본: server/tokens.py · scripts/backfill_tokens.py — 수정 시 함께 맞출 것).
"""
import argparse
import functools
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("in", "out", "cr", "cw", "th")
CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"
ENV_FILE = Path.home() / ".claude" / "madison" / "env"
HANDOFF_DOC_BUDGET = 55_000   # 핸드오프 doc 상한(64KB)보다 여유 있게

FRONTEND_OF = {"codex_exec": "auto", "exec": "auto", "codex-tui": "cli", "cli": "cli",
               "Codex Desktop": "app", "codex-desktop": "app", "codex_desktop": "app"}
AUTO_PROJECTS = ("summarizer", "llm-cwd")   # 허브 내부 LLM 워커 cwd — 항상 자동화로 분류


@functools.lru_cache(maxsize=4096)
def proj_of(cwd: str) -> str:
    """cwd → 프로젝트명 — 라이브 수집기와 같은 규칙: 디렉터리가 남아 있으면 git 최상위
    basename, 없으면 basename (정본: scripts/backfill_tokens.py)."""
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


def day_local(ts: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d")


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


def scan_claude(root: Path, agg):
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
            day = day_local(ev.get("timestamp") or "")
            if not day:
                continue
            project = proj_of(str(ev.get("cwd") or ""))
            acc = agg[(day, "claude-code", project, model[:60],
                       "auto" if project in AUTO_PROJECTS else "")]
            acc["in"] += int(u.get("input_tokens") or 0)
            acc["out"] += int(u.get("output_tokens") or 0)
            acc["cr"] += int(u.get("cache_read_input_tokens") or 0)
            acc["cw"] += int(u.get("cache_creation_input_tokens") or 0)
            details = u.get("output_tokens_details")
            acc["th"] += int(details.get("thinking_tokens") or 0) if isinstance(details, dict) else 0


def scan_codex(root: Path, agg):
    for path in sorted(root.rglob("*.jsonl")):
        project, frontend, model = "", "", "unknown"
        prev = {k: 0 for k in KINDS}
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
            delta = {k: max(0, cur[k] - prev[k]) for k in KINDS}   # 카운터 역행은 버림
            prev = cur
            if not any(delta.values()):
                continue
            day = day_local(ev.get("timestamp") or "")
            if not day:
                continue
            acc = agg[(day, "codex-cli", project, model,
                       "auto" if project in AUTO_PROJECTS else frontend)]
            for k in KINDS:
                acc[k] += delta[k]


def device_name() -> str:
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("MADISON_DEVICE="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, help="출력 JSON 경로 (기본 /tmp/madison-tokens-<기기명>.json)")
    ap.add_argument("--device", default="", help="기기 이름 (기본: ~/.claude/madison/env의 MADISON_DEVICE)")
    ap.add_argument("--claude-dir", type=Path, default=CLAUDE_DIR)
    ap.add_argument("--codex-dir", type=Path, default=CODEX_DIR)
    args = ap.parse_args()

    device = args.device or device_name()
    if not device:
        sys.exit("기기 이름을 알 수 없음 — --device를 지정하세요")
    agg = defaultdict(lambda: {k: 0 for k in KINDS})
    if args.claude_dir.is_dir():
        scan_claude(args.claude_dir, agg)
    if args.codex_dir.is_dir():
        scan_codex(args.codex_dir, agg)
    rows = [{"day": d, "agent": a, "project": p, "model": m, "frontend": f, **v}
            for (d, a, p, m, f), v in sorted(agg.items())]
    payload = {"v": 1, "device": device,
               "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rows": rows}
    out = args.out or Path(f"/tmp/madison-tokens-{device}.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    per_day = defaultdict(lambda: {k: 0 for k in KINDS})
    for r in rows:
        for k in KINDS:
            per_day[(r["agent"], r["day"])][k] += r[k]
    print(f"기기 {device} — 집계 {len(rows)}행")
    for (agent, day) in sorted(per_day):
        v = per_day[(agent, day)]
        print(f"  {agent:12} {day}  in {v['in']:>12,}  out {v['out']:>12,}  cr {v['cr']:>14,}")
    size = out.stat().st_size
    print(f"\n출력: {out} ({size:,} bytes)")
    if size > HANDOFF_DOC_BUDGET:
        print("※ 핸드오프 doc 상한(64KB) 초과 우려 — 다음으로 압축해 실으세요:")
        print(f"  gzip -c {out} | base64")
    return 0


if __name__ == "__main__":
    sys.exit(main())
