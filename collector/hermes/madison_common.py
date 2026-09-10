"""Shared emitter for the MADISON reporter plugin.

Runs in every hermes process that loads plugins (gateway, CLI, TUI, API server).
Events are queued and sent by a daemon worker thread; the worker enriches them
from hermes' own state.db (actor, project/branch, prompt/summary text, tokens).
Never raises into hermes: every public entry point swallows its own errors.
"""

import atexit
import json
import queue
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / "env"
STATE_FILE = BASE / "state.json"
SPOOL_FILE = BASE / "spool.jsonl"
HERMES_STATE_DB = Path.home() / ".hermes" / "state.db"

MAX_BATCH = 500  # madison /api/events per-request cap

# 어댑터가 메시지 앞에 주입하는 기술 스캐폴딩(디스코드 message_id 안내 등)은
# 태스크 표시용 프롬프트에서 걷어낸다.
_SCAFFOLD_RE = re.compile(r"^\s*\[Triggering message id:[^\]]*\]\s*", re.IGNORECASE)


def _scrub(text):
    return _SCAFFOLD_RE.sub("", text or "").strip()


def _load_env():
    cfg = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


_CFG = _load_env()
URL = _CFG.get("MADISON_URL", "").rstrip("/")
TOKEN = _CFG.get("MADISON_TOKEN", "")

_lock = threading.Lock()
_q = queue.Queue()
_worker = None

_turn_sid = {}   # turn_id -> session_id (in-memory; for approval resolution)

# persisted: keys = session_key -> {"sid": ...} (auto-reset close),
#            open = session_id -> last event ts (sweep이 종료 감지에 사용)
try:
    _state = json.loads(STATE_FILE.read_text())
    if not isinstance(_state, dict):
        _state = {}
except Exception:
    _state = {}
_key_sid = _state.get("keys") if isinstance(_state.get("keys"), dict) else {}
_open = _state.get("open") if isinstance(_state.get("open"), dict) else {}


def _save_state():
    try:
        STATE_FILE.write_text(json.dumps({"keys": _key_sid, "open": _open}))
    except OSError:
        pass


def map_turn(turn_id, session_id):
    if turn_id and session_id:
        with _lock:
            _turn_sid[str(turn_id)] = str(session_id)
            if len(_turn_sid) > 512:
                for k in list(_turn_sid)[:256]:
                    _turn_sid.pop(k, None)


def sid_for_turn(turn_id):
    with _lock:
        return _turn_sid.get(str(turn_id))


def sid_for_key(key):
    with _lock:
        entry = _key_sid.get(str(key))
    return entry.get("sid") if isinstance(entry, dict) else entry


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(session_id, event, detail=None, enrich=None):
    """Queue one madison event. enrich: None | 'session' | 'turn' (worker fills from state.db)."""
    if not URL or not TOKEN or not session_id:
        return
    ev = {
        "agent": "hermes",
        "session_id": str(session_id),
        "event": str(event),
        "ts": _now(),
        "event_id": "hm-" + uuid.uuid4().hex,
        "project": "",
        "branch": "",
        "origin": "",
        "subdir": "",
        "detail": detail or {},
    }
    if enrich:
        ev["_enrich"] = enrich
    _ensure_worker()
    _q.put(ev)


def _ensure_worker():
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="madison-reporter", daemon=True)
            _worker.start()


def flush(timeout=2.0):
    """큐가 빌 때까지 대기 — CLI가 Ctrl+C 등으로 빠르게 죽을 때 데몬 워커가
    session_end를 보내기 전에 프로세스가 끝나 이벤트가 유실되는 것을 막는다."""
    deadline = time.time() + timeout
    try:
        while _q.unfinished_tasks and time.time() < deadline:
            time.sleep(0.05)
    except Exception:
        pass


atexit.register(flush)


# ---------- worker side: state.db enrichment ----------

def _db():
    return sqlite3.connect("file:%s?mode=ro" % HERMES_STATE_DB, uri=True, timeout=2)


def _session_row(con, sid):
    row = con.execute(
        "SELECT source, user_id, model, session_key, cwd, git_branch, git_repo_root"
        " FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    if not row:
        return {}
    return dict(zip(
        ("source", "user_id", "model", "session_key", "cwd", "git_branch", "git_repo_root"), row))


def _last_message(con, sid, role):
    row = con.execute(
        "SELECT content FROM messages WHERE session_id=? AND role=? AND content IS NOT NULL"
        " AND content != '' ORDER BY id DESC LIMIT 1", (sid, role)
    ).fetchone()
    return row[0] if row else ""


def _tokens(con, sid):
    rows = con.execute(
        "SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),"
        " SUM(cache_write_tokens), SUM(reasoning_tokens)"
        " FROM session_model_usage WHERE session_id=? GROUP BY model", (sid,)
    ).fetchall()
    cum = {}
    for m, i, o, cr, cw, th in rows:
        if m:
            cum[str(m)[:60]] = {
                "in": int(i or 0), "out": int(o or 0), "cr": int(cr or 0),
                "cw": int(cw or 0), "th": int(th or 0),
            }
    return cum


def _apply_base(ev, info):
    platform = info.get("source") or ev["detail"].get("frontend") or "hermes"
    repo_root = info.get("git_repo_root") or ""
    ev["project"] = Path(repo_root).name if repo_root else "hermes-" + platform
    ev["branch"] = info.get("git_branch") or ""
    d = ev["detail"]
    d.setdefault("frontend", platform)
    d.setdefault("collection_mode", "hooks")
    if info.get("model") and not d.get("model"):
        d["model"] = info["model"]
    if info.get("user_id"):
        d["actor"] = {"platform": platform, "user_id": str(info["user_id"])}


def _enrich(ev):
    mode = ev.pop("_enrich", None)
    sid = ev["session_id"]
    info = {}
    try:
        con = _db()
        try:
            info = _session_row(con, sid)
            _apply_base(ev, info)
            if mode == "turn":
                prompt = _scrub(_last_message(con, sid, "user"))
                summary = _scrub(_last_message(con, sid, "assistant"))
                if prompt:
                    ev["detail"]["prompt"] = prompt[:600]
                if summary:
                    ev["detail"]["summary"] = summary[:300]
                cum = _tokens(con, sid)
                if cum:
                    ev["detail"]["tokens"] = {"v": 1, "cum": cum}
        finally:
            con.close()
    except Exception:
        ev["detail"].setdefault("frontend", "hermes")
        ev["detail"].setdefault("collection_mode", "hooks")

    # 자동 리셋(24h 유휴·04시)은 종료 이벤트 없이 새 세션만 시작되므로,
    # 같은 session_key의 이전 세션을 여기서 합성 종료한다.
    synth = None
    if mode == "session":
        key = info.get("session_key")
        if key:
            with _lock:
                prev = _key_sid.get(key)
                _key_sid[key] = {"sid": sid}
                _save_state()
            prev_sid = prev.get("sid") if isinstance(prev, dict) else prev
            if prev_sid and prev_sid != sid:
                synth = {
                    "agent": "hermes", "session_id": prev_sid, "event": "session_end",
                    "ts": ev["ts"], "event_id": "hm-" + uuid.uuid4().hex,
                    "project": ev["project"], "branch": "", "origin": "", "subdir": "",
                    "detail": {"frontend": ev["detail"].get("frontend", "hermes"),
                               "collection_mode": "hooks", "reason": "auto_reset"},
                }
    return synth


# ---------- worker side: transport ----------

def _post(payload):
    req = urllib.request.Request(
        URL + "/api/events",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
    )
    with urllib.request.urlopen(req, timeout=4) as r:
        r.read()


def _spool_write(events):
    try:
        with SPOOL_FILE.open("a") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
    except OSError:
        pass


def _send(events):
    """Send events (list), prepending any spool backlog; on failure spool them."""
    backlog = []
    if SPOOL_FILE.exists():
        try:
            lines = [l for l in SPOOL_FILE.read_text().splitlines() if l.strip()]
            for l in lines[: MAX_BATCH - len(events)]:
                try:
                    backlog.append(json.loads(l))
                except ValueError:
                    pass
            rest = lines[MAX_BATCH - len(events):]
        except OSError:
            rest = []
    batch = backlog + events
    try:
        _post(batch if len(batch) > 1 else batch[0])
        try:
            if SPOOL_FILE.exists():
                if rest:
                    SPOOL_FILE.write_text("\n".join(rest) + "\n")
                else:
                    SPOOL_FILE.unlink()
        except OSError:
            pass
    except Exception:
        _spool_write(events)


def _track(ev):
    """세션 열림/닫힘 장부 — 스윕이 자동 리셋(훅 없음)된 세션을 감지할 근거."""
    sid, event = ev.get("session_id"), ev.get("event")
    if not sid:
        return
    with _lock:
        if event == "session_end":
            _open.pop(sid, None)
        else:
            _open[sid] = ev.get("ts", "")
        _save_state()


def _make_end(sid, reason, ts=None):
    return {
        "agent": "hermes", "session_id": str(sid), "event": "session_end",
        "ts": ts or _now(), "event_id": "hm-" + uuid.uuid4().hex,
        "project": "", "branch": "", "origin": "", "subdir": "",
        "detail": {"collection_mode": "hooks", "reason": str(reason or "expired")},
    }


_SWEEP_SEC = 60
_IDLE_END_MIN = 1440  # hermes session_reset.idle_minutes와 동일 — 이후엔 어차피 새 세션
_last_sweep = 0.0


def _sweep():
    """훅 없이 끝나는 세션을 닫는다: (1) state.db에 ended_at이 기록된 세션(자동 리셋은
    다음 메시지 도착 시 lazy 적용됨), (2) 유휴 24h를 넘긴 세션(hermes 정책상 다음
    메시지에서 새 세션으로 갈리므로 종료로 간주)."""
    global _last_sweep
    _last_sweep = time.time()
    with _lock:
        snapshot = dict(_open)
    if not snapshot:
        return
    sids = list(snapshot)
    ended = []
    try:
        con = _db()
        try:
            for i in range(0, len(sids), 100):
                chunk = sids[i:i + 100]
                ended += con.execute(
                    "SELECT id, end_reason FROM sessions WHERE ended_at IS NOT NULL"
                    " AND id IN (%s)" % ",".join("?" * len(chunk)), chunk).fetchall()
        finally:
            con.close()
    except Exception:
        return
    closed = {sid for sid, _ in ended}
    for sid, reason in ended:
        ev = _make_end(sid, reason)
        _enrich(ev)
        _send([ev])
        _track(ev)
    cutoff = datetime.now(timezone.utc).timestamp() - _IDLE_END_MIN * 60
    for sid, ts in snapshot.items():
        if sid in closed:
            continue
        try:
            last = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if last < cutoff:
            ev = _make_end(sid, "idle")
            _enrich(ev)
            _send([ev])
            _track(ev)


def _run():
    global _last_sweep
    while True:
        try:
            ev = _q.get(timeout=_SWEEP_SEC)
        except queue.Empty:
            _sweep()
            continue
        try:
            out = []
            synth = _enrich(ev)
            if synth:
                out.append(synth)
                _track(synth)
            out.append(ev)
            _send(out)
            _track(ev)
        except Exception:
            pass
        finally:
            _q.task_done()
        if time.time() - _last_sweep > _SWEEP_SEC:
            _sweep()
