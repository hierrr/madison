"""CLI에서 계정별 사용 가능 모델 목록을 조회한다 (설정 탭 드롭다운용).

- Claude Code: stream-json 제어 프로토콜 initialize 응답의 models
- Codex: app-server JSON-RPC model/list
둘 다 모델을 실제 호출하지 않아 조회 비용이 없다. (phdkim-regular ai_cli_meta.py 이식)
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from contextlib import suppress

_CACHE = {}
_CACHE_TTL = 3600


def _cache_get(key):
    v = _CACHE.get(key)
    if not v or time.time() - v[1] > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return v[0]


def _norm(mid, display, efforts, default_effort, is_default):
    return {"id": mid, "display_name": display or mid, "efforts": [e for e in efforts if e],
            "default_effort": default_effort or "", "is_default": bool(is_default)}


def _env_for(cli_path):
    """CLI 디렉터리를 PATH 앞에 — launchd 최소 PATH에서 shebang(env node) 해석 실패 방지."""
    return {**os.environ, "MADISON_SUPPRESS": "1",
            "PATH": os.path.dirname(cli_path) + os.pathsep + os.environ.get("PATH", "")}


def _query_claude(cli_path):
    req_id = "madison-models"
    req = {"request_id": req_id, "type": "control_request", "request": {"subtype": "initialize"}}
    out = subprocess.run(
        [cli_path, "--output-format", "stream-json", "--verbose", "--input-format", "stream-json"],
        input=json.dumps(req) + "\n", capture_output=True, text=True, timeout=15,
        env=_env_for(cli_path), start_new_session=True,
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "응답 없음").strip()[:200])
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        msg = json.loads(line)
        resp = msg.get("response") or {}
        if msg.get("type") == "control_response" and resp.get("request_id") == req_id:
            if resp.get("subtype") != "success":
                raise RuntimeError(str(resp.get("error") or "제어 프로토콜 오류"))
            models = (resp.get("response") or {}).get("models") or []
            return [
                _norm(m["value"], m.get("displayName"),
                      (m.get("supportedEffortLevels") or []) if m.get("supportsEffort") else [],
                      "", m.get("isDefault") or m.get("value") == "default")
                for m in models if isinstance(m, dict) and m.get("value")
            ]
    raise RuntimeError("모델 응답을 찾지 못함")


def _query_codex(cli_path):
    proc = subprocess.Popen(
        [cli_path, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True,
        env=_env_for(cli_path),
    )
    q, errs = queue.Queue(), deque(maxlen=100)
    threading.Thread(target=lambda: ([q.put(l) for l in proc.stdout], q.put(None)), daemon=True).start()
    threading.Thread(target=lambda: [errs.append(l) for l in proc.stderr], daemon=True).start()
    deadline = time.monotonic() + 15

    def send(m):
        proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()

    def read(rid):
        while time.monotonic() < deadline:
            try:
                line = q.get(timeout=0.25)
            except queue.Empty:
                if proc.poll() is not None:
                    raise RuntimeError("app-server 조기 종료: " + "".join(errs).strip()[-200:])
                continue
            if line is None:
                raise RuntimeError("app-server 종료: " + "".join(errs).strip()[-200:])
            msg = json.loads(line)
            if msg.get("id") != rid:
                continue
            if msg.get("error"):
                raise RuntimeError(str(msg["error"]))
            return msg.get("result") or {}
        raise TimeoutError("model/list 타임아웃")

    try:
        send({"method": "initialize", "id": 0,
              "params": {"clientInfo": {"name": "madison", "title": "MADISON", "version": "1.0"}}})
        send({"method": "initialized", "params": {}})
        models, cursor, rid = [], None, 1
        while True:
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            send({"method": "model/list", "id": rid, "params": params})
            page = read(rid)
            models.extend(page.get("data") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
            rid += 1
        out = []
        for m in models:
            mid = m.get("id") or m.get("model")
            if not mid or m.get("hidden") is True:
                continue
            efforts = [(e.get("reasoningEffort") if isinstance(e, dict) else e)
                       for e in (m.get("supportedReasoningEfforts") or [])]
            out.append(_norm(mid, m.get("displayName"), efforts,
                             m.get("defaultReasoningEffort"), m.get("isDefault")))
        return out
    finally:
        with suppress(BrokenPipeError, OSError):
            proc.stdin.close()
        if proc.poll() is None:
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=0.5)
        if proc.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired, ProcessLookupError):
                proc.wait(timeout=1)


def get_models(provider: str, cli_path: str, refresh: bool = False) -> dict:
    """{models: [...], error: str} — 1시간 캐시, refresh=True면 재조회."""
    key = f"{provider}:{cli_path}"
    if refresh:
        _CACHE.pop(key, None)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = {"models": [], "error": ""}
    if not cli_path or not (os.path.isfile(cli_path) and os.access(cli_path, os.X_OK)):
        # PATH 탐색 허용 (codex 기본값이 이름만일 수 있음)
        import shutil
        found = shutil.which(cli_path) if cli_path else None
        if not found:
            result["error"] = f"CLI 경로를 찾을 수 없음: {cli_path or '(비어 있음)'}"
            _CACHE[key] = (result, time.time())
            return result
        cli_path = found
    try:
        result["models"] = _query_claude(cli_path) if provider == "claude" else _query_codex(cli_path)
        if not result["models"]:
            result["error"] = "CLI가 모델 목록을 반환하지 않음"
    except Exception as exc:
        result["error"] = str(exc)[:300]
    _CACHE[key] = (result, time.time())
    return result
