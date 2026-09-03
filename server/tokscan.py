"""허브 내부 LLM 워커 전사본 스캔 — 훅이 못 잡는 사용량 보완.

summarizer·llm-cwd 전용 cwd에서 도는 허브 워커(요약·리포트 생성)는 재귀 방지로 훅을
억제하므로(llm.py의 MADISON_SUPPRESS) 라이브 토큰 수집에 잡히지 않는다. 이 스캐너가
해당 전사본만 주기 스캔해 파일별 누적의 **증가분**을 token_daily에 frontend='auto'로
가산한다(자동화 제외 필터와 일관). 첫 가동 시엔 기존 파일들의 누적을 기준점으로만
저장한다 — 과거분은 백필 몫이라 이중 계상하지 않는다.
"""
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from . import db, tokens
from .config import CFG

log = logging.getLogger("madison.tokscan")

CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"
DEVICE_ENV = Path.home() / ".claude" / "madison" / "env"
_BASELINE_KEY = "tokscan.baseline"
SWEEP_SEC = 600
RECENT_SEC = 3 * 86_400        # 정기 스윕은 최근 파일만 (기준점 없는 옛 파일은 어차피 불변)


def hub_device_id(c) -> int | None:
    """이 허브 기기의 devices.id — 수집기 env의 MADISON_DEVICE로 식별한다."""
    name = ""
    try:
        for line in DEVICE_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("MADISON_DEVICE="):
                name = line.split("=", 1)[1].strip()
                break
    except OSError:
        pass
    if not name:
        return None
    row = c.execute("SELECT id FROM devices WHERE name=? AND revoked=0", (name,)).fetchone()
    return row["id"] if row else None


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


def claude_cum(path: Path):
    """워커 전사본 전체 누적 — ({model: counts}, project). 수집기 report.sh와 같은 규칙."""
    cum: dict = {}
    project = ""
    for ev in _lines(path):
        if not isinstance(ev, dict):
            continue
        if not project and ev.get("cwd"):
            project = Path(str(ev["cwd"])).name
        if ev.get("type") != "assistant":
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
        acc = cum.setdefault(model[:60], {k: 0 for k in tokens.KINDS})
        acc["in"] += int(u.get("input_tokens") or 0)
        acc["out"] += int(u.get("output_tokens") or 0)
        acc["cr"] += int(u.get("cache_read_input_tokens") or 0)
        acc["cw"] += int(u.get("cache_creation_input_tokens") or 0)
        details = u.get("output_tokens_details")
        acc["th"] += int(details.get("thinking_tokens") or 0) if isinstance(details, dict) else 0
    return cum, project


def codex_cum(path: Path):
    """롤아웃의 마지막 token_count 누적 — ({model: counts}, project).
    cwd가 워커 디렉터리가 아니거나 판별 불가면 (None, project)로 스킵 신호."""
    project, model, total = "", "unknown", None
    for ev in _lines(path):
        if not isinstance(ev, dict):
            continue
        pl = ev.get("payload")
        if not isinstance(pl, dict):
            continue
        if ev.get("type") == "session_meta":
            project = Path(str(pl.get("cwd") or "")).name
            if project not in tokens.AUTO_PROJECTS:
                return None, project   # 일반 세션 — 훅이 담당
            continue
        if ev.get("type") == "turn_context" and pl.get("model"):
            model = str(pl.get("model"))[:60]
            continue
        if pl.get("type") != "token_count":
            continue
        info = pl.get("info")
        t = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(t, dict):
            continue
        raw_in = int(t.get("input_tokens") or 0)
        cached = int(t.get("cached_input_tokens") or 0)
        total = {"in": max(0, raw_in - cached), "out": int(t.get("output_tokens") or 0),
                 "cr": cached, "cw": int(t.get("cache_write_input_tokens") or 0),
                 "th": int(t.get("reasoning_output_tokens") or 0)}
    if project not in tokens.AUTO_PROJECTS:
        return None, project   # session_meta가 없어 판별 불가 — 건드리지 않는다
    return ({} if total is None else {model: total}), project


def candidates(claude_root: Path, codex_root: Path, *, all_files: bool, now: float):
    """(agent, path) 후보 — Claude는 워커 cwd 슬러그 디렉터리만, Codex는 최근 롤아웃 전부
    (cwd 판별은 codex_cum에서). all_files=True(기준점 잡기)면 mtime 필터 없음."""
    cutoff = 0 if all_files else now - RECENT_SEC
    for pat in ("*-summarizer", "*-llm-cwd"):
        for d in claude_root.glob(pat):
            for p in d.glob("*.jsonl"):
                try:
                    if p.stat().st_mtime >= cutoff:
                        yield "claude-code", p
                except OSError:
                    continue
    for p in codex_root.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime >= cutoff:
                yield "codex-cli", p
        except OSError:
            continue


def sweep(c, device_id: int, *, baseline: bool, claude_root=CLAUDE_DIR, codex_root=CODEX_DIR,
          now: float | None = None) -> int:
    """한 바퀴 스캔. 반환: token_daily에 가산한 파일 수. baseline이면 기준점만 저장."""
    now = now or time.time()
    folded = 0
    for agent, path in candidates(claude_root, codex_root, all_files=baseline, now=now):
        try:
            st = path.stat()
        except OSError:
            continue
        row = c.execute("SELECT cum, size, mtime FROM token_scan_state WHERE path=?",
                        (str(path),)).fetchone()
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
            continue
        if agent == "claude-code":
            cum, project = claude_cum(path)
        else:
            cum, project = codex_cum(path)
            if cum is None:      # 워커 cwd가 아닌 일반 세션 — 훅이 담당. 상태만 남겨 재판별 생략
                c.execute("INSERT INTO token_scan_state (path, cum, size, mtime, updated_at)"
                          " VALUES (?, NULL, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
                          " ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
                          " updated_at=excluded.updated_at",
                          (str(path), st.st_size, st.st_mtime))
                continue
        if not cum:
            continue
        if not baseline:
            try:
                old = json.loads(row["cum"]) if row and row["cum"] else {}
            except (ValueError, TypeError):
                old = {}
            day = datetime.fromtimestamp(st.st_mtime).astimezone().strftime("%Y-%m-%d")
            any_delta = False
            if agent == "codex-cli":
                # 세션 전역 카운터 — 기준점은 값 하나, 델타는 현재 모델로 (tokens.fold와 동일 원칙)
                model = next(iter(cum))
                d = tokens._delta(next(iter(old.values())) if old else None, cum[model])
                if any(d.values()):
                    tokens.add_daily(c, day, device_id, agent, project, model, "auto", "events", d)
                    any_delta = True
            else:
                for model, v in cum.items():
                    d = tokens._delta(old.get(model), v)
                    if any(d.values()):
                        tokens.add_daily(c, day, device_id, agent, project, model, "auto", "events", d)
                        any_delta = True
            if any_delta:
                folded += 1
        c.execute("INSERT INTO token_scan_state (path, cum, size, mtime, updated_at)"
                  " VALUES (?,?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
                  " ON CONFLICT(path) DO UPDATE SET cum=excluded.cum, size=excluded.size,"
                  " mtime=excluded.mtime, updated_at=excluded.updated_at",
                  (str(path), json.dumps(cum, ensure_ascii=False), st.st_size, st.st_mtime))
    return folded


def loop():
    while True:
        try:
            with db.tx() as c:
                device_id = hub_device_id(c)
                if device_id is None:
                    log.info("tokscan 비활성 — 이 기기의 MADISON_DEVICE를 못 찾음")
                    return
                first = c.execute("SELECT value FROM settings WHERE key=?", (_BASELINE_KEY,)).fetchone() is None
                if first:
                    n = sweep(c, device_id, baseline=True)
                    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                              (_BASELINE_KEY, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                    log.info("tokscan 기준점 저장 완료")
                else:
                    n = sweep(c, device_id, baseline=False)
                    if n:
                        log.info("tokscan: %d개 파일 증가분 가산", n)
        except Exception:
            log.exception("tokscan 오류")
        time.sleep(SWEEP_SEC)


def start():
    threading.Thread(target=loop, daemon=True).start()
