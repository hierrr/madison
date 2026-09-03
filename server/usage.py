"""Claude Code·Codex 구독 한도 — 허브가 직접 수집한다(외부 도구·파일 의존 없음).

- Claude: Claude Code가 macOS 키체인("Claude Code-credentials") 또는 ~/.claude/.credentials.json에 두는 OAuth
  액세스 토큰으로 /usage가 부르는 읽기 전용 엔드포인트를 조회한다. 토큰은 메모리에만 두고 기록하지 않는다.
  만료·거절(401)이면 이번 회차는 건너뛴다 — Claude Code를 쓰면 토큰이 갱신되어 다음 회차에 회복된다(자체 갱신은
  세션·훅을 유발하므로 하지 않는다).
- Codex: `codex app-server --stdio` JSON-RPC(initialize → initialized → account/rateLimits/read). 실패하면
  ~/.codex/sessions 최신 전사본의 token_count 스냅샷으로 대체.
한도는 계정 단위라 허브 기기 하나에서 읽으면 된다.
"""
import json
import logging
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import CFG

log = logging.getLogger("madison.usage")
_STORE_KEY = "usage.snapshot"     # settings 테이블에 마지막 성공값 보존 — 허브 재시작 직후 빈 타일 방지

PROVIDERS = ("claude", "codex")
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_RPC_TTL = 300          # 초 — app-server(node 프로세스)를 매 회차 띄우지 않도록 결과 재사용

_lock = threading.Lock()
_snap: dict = {p: None for p in PROVIDERS}
_codex_cache: dict = {"at": 0.0, "parsed": None}
_claude_backoff_until = 0.0
CLAUDE_BACKOFF_SEC = 600     # 429(과호출) 뒤 쉬는 시간 — 같은 계정을 다른 도구도 조회할 수 있다


class UsageError(RuntimeError):
    pass


# ── 공통 ──────────────────────────────────────────────

def to_epoch(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:                 # 시간대 없는 ISO는 UTC로
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def format_remaining(resets_at, now=None) -> str:
    """리셋까지 남은 시간: '3d05h', '2h31m', '45m'; 지났으면 'resetting'."""
    if not isinstance(resets_at, (int, float)):
        return ""
    now = time.time() if now is None else now
    delta = int(resets_at - now)
    if delta <= 0:
        return "resetting"
    days, rem = divmod(delta, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{max(minutes, 1)}m"


def _window(title, percent, resets_at, order=9):
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    return {"title": title, "pct": int(round(max(0.0, min(float(percent), 100.0)))),
            "resets_at": to_epoch(resets_at), "order": order}


def _finish(windows, now=None) -> list:
    out = []
    for w in sorted(windows, key=lambda x: (x["order"], x["title"])):
        left = format_remaining(w["resets_at"], now)
        out.append({"title": w["title"], "pct": w["pct"], "left": left, "resets_at": w["resets_at"],
                    "value": f"{w['pct']}%" + (f" · {left} left" if left else "")})
    return out


# ── Claude ────────────────────────────────────────────

def claude_oauth() -> dict:
    raw = ""
    try:
        done = subprocess.run(["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
                              capture_output=True, text=True, timeout=10, check=False)
        raw = done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raw = ""
    if not raw:
        try:
            raw = CLAUDE_CREDENTIALS_FILE.read_text("utf-8")
        except OSError as exc:
            raise UsageError("Claude Code 자격증명 없음(키체인·~/.claude/.credentials.json)") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise UsageError("Claude Code 자격증명이 JSON이 아님") from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise UsageError("OAuth 액세스 토큰 없음")
    return oauth


def claude_fetch(token: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "anthropic-beta": CLAUDE_OAUTH_BETA, "User-Agent": "madison-hub/usage"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise UsageError("토큰 거절(401) — Claude Code를 한 번 쓰면 갱신됨") from exc
        if exc.code == 429:
            global _claude_backoff_until
            _claude_backoff_until = time.time() + CLAUDE_BACKOFF_SEC
            raise UsageError(f"usage HTTP 429 — {CLAUDE_BACKOFF_SEC // 60}분 뒤 재시도") from exc
        raise UsageError(f"usage HTTP {exc.code}") from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise UsageError(f"usage 조회 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError("usage 응답이 객체가 아님")
    return data


def _scope_name(item: dict):
    scope = item.get("scope")
    if not isinstance(scope, dict):
        return None
    model = scope.get("model")
    name = (model.get("display_name") or model.get("id")) if isinstance(model, dict) else None
    return name or scope.get("surface")


def parse_claude(data: dict) -> list:
    """limits[] (또는 구형 five_hour/seven_day) → 창 목록: 5h, 7d 전체, 7d 모델별, 기타."""
    windows = []
    limits = data.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            if kind == "session":
                title, order = "5h", 0
            elif kind == "weekly_all":
                title, order = "7d all models", 1
            elif kind == "weekly_scoped":
                title, order = f"7d {_scope_name(item) or 'scoped'}", 2
            else:
                title, order = kind.replace("_", " ") or "limit", 3
            w = _window(title, item.get("percent"), item.get("resets_at"), order)
            if w:
                windows.append(w)
    if not windows:
        for key, title, order in (("five_hour", "5h", 0), ("seven_day", "7d all models", 1)):
            item = data.get(key)
            if isinstance(item, dict):
                w = _window(title, item.get("utilization"), item.get("resets_at"), order)
                if w:
                    windows.append(w)
    return windows


def collect_claude(now=None) -> dict:
    if time.time() < _claude_backoff_until:
        raise UsageError("429 백오프 중")
    oauth = claude_oauth()
    windows = parse_claude(claude_fetch(str(oauth["accessToken"])))
    if not windows:
        raise UsageError("usage 응답에 한도 창이 없음")
    plan = str(oauth.get("subscriptionType") or "").strip()
    return {"plan": plan.capitalize() if plan else "", "windows": _finish(windows, now), "extras": [],
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))}


# ── Codex ─────────────────────────────────────────────

def _codex_bin() -> str:
    from . import llm
    try:
        return llm.conf("summary")["codex_bin"]
    except Exception:
        return CFG.codex_bin


def codex_rpc(timeout: float = 20) -> dict:
    """`codex app-server --stdio`: initialize → initialized → account/rateLimits/read."""
    codex = _codex_bin()
    try:
        # launchd의 최소 PATH엔 nvm node가 없다 — codex(셔뱅 `env node`)의 디렉터리를 PATH 앞에 (llm.run과 동일)
        env = {**os.environ, "PATH": os.path.dirname(codex) + os.pathsep + os.environ.get("PATH", "")}
        proc = subprocess.Popen([codex, "app-server", "--stdio"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
    except OSError as exc:
        raise UsageError(f"codex app-server 시작 실패: {exc}") from exc
    lines: queue.Queue = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)
    threading.Thread(target=pump, daemon=True).start()

    def send(msg):
        proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    deadline = time.monotonic() + timeout
    try:
        send({"method": "initialize", "id": 1,
              "params": {"clientInfo": {"name": "madison", "title": "MADISON hub", "version": "0.1"}}})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageError("codex app-server 응답 없음(timeout)")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise UsageError("codex app-server 응답 없음(timeout)") from exc
            if line is None:
                raise UsageError("codex app-server가 먼저 종료됨")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == 1:
                send({"method": "initialized", "params": {}})
                send({"method": "account/rateLimits/read", "id": 2, "params": {}})
            elif msg.get("id") == 2:
                if msg.get("error"):
                    err = msg["error"]
                    raise UsageError(f"rateLimits/read 실패: {err.get('message') if isinstance(err, dict) else err}")
                result = msg.get("result")
                if not isinstance(result, dict):
                    raise UsageError("rateLimits/read 결과 없음")
                return result
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass


def _window_title(minutes) -> str:
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
        return "window"
    v = int(minutes)
    if v % 10_080 == 0:
        return "7d" if v == 10_080 else f"{v // 10_080}w"
    if v % 1_440 == 0:
        return f"{v // 1_440}d"
    if v % 60 == 0:
        return f"{v // 60}h"
    return f"{v}m"


def parse_codex_rpc(result: dict) -> dict:
    """account/rateLimits/read 결과 → {windows, plan, reset_credits}."""
    by_id = result.get("rateLimitsByLimitId")
    if not isinstance(by_id, dict) or not by_id:
        single = result.get("rateLimits")
        by_id = {"codex": single} if isinstance(single, dict) else {}
    windows, plan = [], None
    for limit_id, limit in by_id.items():
        if not isinstance(limit, dict):
            continue
        plan = plan or limit.get("planType")
        for slot in ("primary", "secondary", "individualLimit"):
            win = limit.get(slot)
            if not isinstance(win, dict):
                continue
            mins = win.get("windowDurationMins")
            title = _window_title(mins)
            if len(by_id) > 1:
                title = f"{title} {limit.get('limitName') or limit_id}"
            w = _window(title, win.get("usedPercent"), win.get("resetsAt"),
                        float(mins) if isinstance(mins, (int, float)) else 1e9)
            if w:
                windows.append(w)
    credits = result.get("rateLimitResetCredits")
    avail = credits.get("availableCount") if isinstance(credits, dict) else None
    return {"windows": windows, "plan": plan,
            "reset_credits": int(avail) if isinstance(avail, (int, float)) else None}


def codex_transcript_snapshot():
    """~/.codex/sessions 최신 전사본의 token_count.rate_limits — RPC 실패 시 대체."""
    try:
        files = sorted(CODEX_SESSIONS_DIR.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files[:5]:
        latest = None
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    pl = ev.get("payload") if isinstance(ev, dict) else None
                    if isinstance(pl, dict) and pl.get("type") == "token_count" and isinstance(pl.get("rate_limits"), dict):
                        latest = pl["rate_limits"]
        except OSError:
            continue
        if latest:
            return latest
    return None


def parse_codex_transcript(rl: dict) -> dict:
    windows = []
    for slot in ("primary", "secondary"):
        win = rl.get(slot)
        if isinstance(win, dict):
            mins = win.get("window_minutes")
            w = _window(_window_title(mins), win.get("used_percent"), win.get("resets_at"),
                        float(mins) if isinstance(mins, (int, float)) else 1e9)
            if w:
                windows.append(w)
    return {"windows": windows, "plan": rl.get("plan_type"), "reset_credits": None}


def collect_codex(now=None) -> dict:
    now = now or time.time()
    parsed = _codex_cache["parsed"] if now - _codex_cache["at"] < CODEX_RPC_TTL else None
    if parsed is None:
        try:
            parsed = parse_codex_rpc(codex_rpc())
        except UsageError as exc:
            snap = codex_transcript_snapshot()
            if snap is None:
                raise UsageError(f"{exc}; 전사본 스냅샷도 없음") from exc
            parsed = parse_codex_transcript(snap)
        _codex_cache.update(at=now, parsed=parsed)
    if not parsed.get("windows"):
        raise UsageError("Codex 응답에 한도 창이 없음")
    plan = str(parsed.get("plan") or "").strip()
    extras = []
    if isinstance(parsed.get("reset_credits"), int):
        extras.append({"title": "Reset credits", "value": f"{parsed['reset_credits']} available",
                       "count": parsed["reset_credits"]})
    return {"plan": plan.capitalize() if plan else "", "windows": _finish(parsed["windows"], now),
            "extras": extras, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_codex_cache["at"]))}


# ── 수집 루프·스냅샷 ─────────────────────────────────

def _persist():
    try:
        with _lock:
            payload = json.dumps(_snap, ensure_ascii=False)
        with db.tx() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_STORE_KEY, payload))
    except Exception:
        log.exception("usage 스냅샷 저장 실패")


def load_persisted():
    try:
        with db.tx() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (_STORE_KEY,)).fetchone()
        data = json.loads(row["value"]) if row else None
    except Exception:
        return
    if isinstance(data, dict):
        with _lock:
            for p in PROVIDERS:
                if isinstance(data.get(p), dict) and not _snap[p]:
                    _snap[p] = data[p]


def record_history(c, provider: str, windows: list, ts: str | None = None) -> int:
    """창별 마지막 행과 pct가 다를 때만 usage_history에 append. 반환: 추가 행 수.
    변화만 쌓아 폴링 주기와 무관하게 용량을 억제한다(같은 pct로 돌아오는 리셋은 안 보이지만
    차트 해상도에선 무해). 조회는 계단선(step-after)으로 사이를 메운다."""
    ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n = 0
    for w in windows:
        last = c.execute(
            "SELECT pct FROM usage_history WHERE provider=? AND win=? ORDER BY id DESC LIMIT 1",
            (provider, w["title"])).fetchone()
        if last is None or last["pct"] != w["pct"]:
            c.execute("INSERT INTO usage_history (ts, provider, win, pct, resets_at) VALUES (?,?,?,?,?)",
                      (ts, provider, w["title"], w["pct"], w.get("resets_at")))
            n += 1
    return n


RESET_EARLY_SLACK = 600   # 초 — 예정 리셋 시각보다 이만큼 이르면 프로바이더 발 조기 리셋으로 본다


def detect_resets(c, provider: str, win: str, frm: float | None) -> list:
    """pct 하락 = 창 리셋. 직전 행의 resets_at보다 이르면 early(프로바이더가 임의 초기화한 경우).
    반환: [{ts, from, to, early}] — 범위 안의 하락만 (직전 기준행은 범위 밖에서 이어받는다)."""
    if frm is not None:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frm))
        prev = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND win=?"
                         " AND ts < ? ORDER BY id DESC LIMIT 1", (provider, win, cutoff)).fetchone()
        rows = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND win=?"
                         " AND ts >= ? ORDER BY id", (provider, win, cutoff))
    else:
        prev = None
        rows = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND win=?"
                         " ORDER BY id", (provider, win))
    out = []
    for r in rows:
        if prev is not None and r["pct"] < prev["pct"]:
            ts = to_epoch(r["ts"])
            early = (isinstance(prev["resets_at"], (int, float)) and ts is not None
                     and ts < prev["resets_at"] - RESET_EARLY_SLACK)
            out.append({"ts": int(ts) if ts else None, "from": prev["pct"], "to": r["pct"],
                        "early": bool(early)})
        prev = r
    return out


def history(c, days: int = 30) -> dict:
    """한도 % 시계열 — {provider: {windows: {win: [[epoch, pct], …]}, resets: {win: […]}}, from, to}.
    변화 시점만 저장돼 있으므로 범위 직전 carry-in 1행을 붙여 계단선 시작 레벨을 준다.
    다운샘플은 **실제 데이터 폭** 기준(≤14일 원본, ≤92일 시간별, 그 이상 일별) — 요청 범위가
    길어도 이력이 짧으면 원본 그대로. 버킷은 마지막 행(MAX(id)의 bare column, SQLite 보장) —
    MAX(ts)+MAX(pct)처럼 다른 행의 값이 짝지어지지 않게. 리셋 감지는 항상 원본 행 기준."""
    now = time.time()
    days = max(0, min(int(days or 0), 3660))
    frm = now - days * 86_400 if days else None
    first = to_epoch((c.execute("SELECT MIN(ts) t FROM usage_history").fetchone() or {"t": None})["t"])
    span_start = max(first, frm) if (first is not None and frm is not None) else (first if first is not None else now)
    span_days = max(1, int((now - span_start) // 86_400) + 1)
    out = {p: {"windows": {}, "resets": {}} for p in PROVIDERS}
    for provider in PROVIDERS:
        for row in c.execute("SELECT DISTINCT win FROM usage_history WHERE provider=?", (provider,)):
            win = row["win"]
            series = []
            if frm is not None:
                cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frm))
                carry = c.execute(
                    "SELECT ts, pct FROM usage_history WHERE provider=? AND win=? AND ts < ?"
                    " ORDER BY id DESC LIMIT 1", (provider, win, cutoff)).fetchone()
                if carry:
                    series.append([int(frm), carry["pct"]])
                pred, args = "AND ts >= ?", (provider, win, cutoff)
            else:
                pred, args = "", (provider, win)
            if span_days <= 14:
                q = f"SELECT ts, pct FROM usage_history WHERE provider=? AND win=? {pred} ORDER BY id"
            elif span_days <= 92:
                q = (f"SELECT MAX(id) _last, ts, pct FROM usage_history WHERE provider=? AND win=? {pred}"
                     f" GROUP BY strftime('%Y-%m-%dT%H', ts) ORDER BY ts")
            else:
                q = (f"SELECT MAX(id) _last, ts, pct FROM usage_history WHERE provider=? AND win=? {pred}"
                     f" GROUP BY date(ts) ORDER BY ts")
            for r in c.execute(q, args):
                epoch = to_epoch(r["ts"])
                if epoch is not None:
                    series.append([int(epoch), r["pct"]])
            if series:
                out[provider]["windows"][win] = series
            resets = detect_resets(c, provider, win, frm)
            if resets:
                out[provider]["resets"][win] = resets
    return {**out, "from": int(frm) if frm else None, "to": int(now)}


def refresh() -> dict:
    """두 프로바이더를 한 번 수집. 실패한 쪽은 마지막 성공값을 유지하고 error만 붙인다."""
    changed = False
    for name, fn in (("claude", collect_claude), ("codex", collect_codex)):
        try:
            data = fn()
            data["error"] = ""
            with _lock:
                _snap[name] = data
            changed = True
            try:
                with db.tx() as c:
                    record_history(c, name, data["windows"])
            except Exception:
                log.exception("usage 히스토리 append 실패")
        except UsageError as exc:
            log.info("usage %s 건너뜀: %s", name, exc)
            with _lock:
                if _snap[name]:
                    _snap[name]["error"] = str(exc)
        except Exception:
            log.exception("usage %s 오류", name)
    if changed:
        _persist()
    return snapshot()


def snapshot() -> dict:
    """현황 KPI용 — 남은 시간은 조회 시점 기준으로 다시 계산한다."""
    now = time.time()
    with _lock:
        out = {}
        for p in PROVIDERS:
            d = _snap[p]
            if not d:
                out[p] = None
                continue
            out[p] = {**d, "windows": [
                {**w, "left": format_remaining(w.get("resets_at"), now)} for w in d["windows"]]}
    return out


def loop():
    load_persisted()
    while True:
        try:
            refresh()
        except Exception:
            log.exception("usage loop 오류")
        time.sleep(CFG.usage_poll_sec)
