"""MADISON reporter — plugin-bus edition.

Runs in every hermes process that loads plugins (gateway, CLI, TUI, API
server), so local `hermes` sessions are covered the same as Telegram/Discord.

Mapping onto madison's event vocabulary:
  on_session_start      → session_start
  first pre_llm_call    → prompt        (turn begins; working)
  pre_tool_call         → tool_start    (current tool badge)
  post_tool_call        → heartbeat
  on_session_end        → turn_done     (per run_conversation call, incl. interrupted)
  on_session_finalize   → session_end
  pre_approval_request  → permission_request (red approval queue)
  post_approval_response → heartbeat    (clears it; the turn keeps running)

All handlers are fire-and-forget and must never raise into hermes.
"""

import importlib.util
import sys
import uuid
from pathlib import Path

_COMMON_PATH = Path(__file__).resolve().parent / "madison_common.py"
_mod = None  # 이 __init__ 인스턴스에 묶인 공용 모듈 — sys.modules 공유 캐시를 쓰지 않는다


def _common():
    """공용 모듈을 이 플러그인 인스턴스마다 새로 적재한다. 게이트웨이 핫리로드는 __init__.py만
    다시 실행하므로, sys.modules에 남은 구 버전을 재사용하면 새 핸들러가 구 함수를 호출하게 된다."""
    global _mod
    if _mod is None:
        name = "hermes_madison_common_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, _COMMON_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        _mod = mod
    return _mod


def _on_unload():
    global _mod
    mod, _mod = _mod, None
    if mod is not None:
        try:
            mod.shutdown()
        except Exception:
            pass


_seen_turns = set()  # turn_id별로 prompt는 한 번만


def _on_session_start(session_id="", model="", platform="", **kw):
    try:
        c = _common()
        d = {"collection_mode": "hooks"}
        if platform:
            d["frontend"] = platform
        if model:
            d["model"] = model
        c.emit(session_id, "session_start", d, enrich="session")
    except Exception:
        pass


def _on_pre_llm_call(session_id="", turn_id="", platform="", model="", user_message="", **kw):
    try:
        c = _common()
        c.map_turn(turn_id, session_id)
        if turn_id and turn_id not in _seen_turns:
            _seen_turns.add(turn_id)
            if len(_seen_turns) > 1024:
                _seen_turns.clear()
                _seen_turns.add(turn_id)
            # 지시문은 훅 인자가 진실 — state.db 기록은 비동기라 worker가 읽을 때 아직 없을 수 있다
            d = {"collection_mode": "hooks"}
            text = c.prompt_text(user_message)
            if text:
                d["prompt"] = text[:600]
            c.emit(session_id, "prompt", d, enrich="turn")
    except Exception:
        pass


def _on_pre_tool_call(session_id="", turn_id="", tool_name="", **kw):
    try:
        c = _common()
        c.map_turn(turn_id, session_id)
        c.emit(session_id, "tool_start",
               {"collection_mode": "hooks", "tool": str(tool_name)[:60]})
    except Exception:
        pass


def _on_post_tool_call(session_id="", turn_id="", tool_name="", **kw):
    try:
        _common().emit(session_id, "heartbeat", {"collection_mode": "hooks"})
    except Exception:
        pass


def _on_session_end(session_id="", task_id="", turn_id="", completed=None,
                    interrupted=None, model="", platform="", **kw):
    # 이름과 달리 run_conversation 1회(=턴)마다 발화한다 — madison의 turn_done.
    try:
        d = {"collection_mode": "hooks"}
        if interrupted:
            d["interrupted"] = True
        _common().emit(session_id, "turn_done", d, enrich="turn")
    except Exception:
        pass


def _on_session_finalize(session_id="", platform="", reason="", **kw):
    try:
        c = _common()
        c.emit(session_id, "session_end",
               {"collection_mode": "hooks", "reason": str(reason or "finalize")})
        c.flush(2.0)  # 프로세스 종료 직전일 수 있으므로 전송을 보장
    except Exception:
        pass


def _on_subagent_start(parent_session_id="", parent_turn_id="", child_role="",
                       child_goal="", **kw):
    try:
        c = _common()
        c.map_turn(parent_turn_id, parent_session_id)
        c.emit(parent_session_id, "tool_start",
               {"collection_mode": "hooks", "tool": ("delegate:" + str(child_role))[:60],
                "bg": "start", "bg_kind": "agent"})
    except Exception:
        pass


def _on_subagent_stop(parent_session_id="", parent_turn_id="", child_role="",
                      child_status="", **kw):
    try:
        _common().emit(parent_session_id, "subagent_stop",
                       {"collection_mode": "hooks", "bg": "stop", "bg_kind": "agent"})
    except Exception:
        pass


def _resolve_sid(c, session_key, turn_id):
    # session_key를 세션 id로 쓰면 허브에 유령 세션이 생긴다 — 못 풀면 빈 값(emit이 생략)
    return (turn_id and c.sid_for_turn(turn_id)) or \
           (session_key and c.sid_for_key_db(session_key)) or ""


def _on_pre_approval(command="", description="", pattern_key="", pattern_keys=None,
                     session_key="", surface="", turn_id="", tool_call_id="", **kw):
    try:
        if surface == "smart":  # 보조 LLM이 수 초 내 자동 결정 — 큐에 띄우지 않는다
            return
        c = _common()
        sid = _resolve_sid(c, session_key, turn_id)
        msg = (description or command or "approval requested")[:300]
        c.emit(sid, "permission_request", {"collection_mode": "hooks", "message": msg})
    except Exception:
        pass


def _on_post_approval(command="", description="", pattern_key="", pattern_keys=None,
                      session_key="", surface="", choice="", decided_by="",
                      turn_id="", tool_call_id="", **kw):
    try:
        if surface == "smart":
            return
        c = _common()
        sid = _resolve_sid(c, session_key, turn_id)
        c.emit(sid, "heartbeat",
               {"collection_mode": "hooks", "approval_choice": str(choice)})
    except Exception:
        pass


def register(ctx):
    try:
        ctx.on_unload(_on_unload)  # 리로드 시 구 worker 정지
    except Exception:
        pass
    try:
        # worker(전송 + 60초 스윕)는 첫 emit에서만 시작됐는데, 그러면 이벤트가 없는 동안(유휴 24h 종료 판정이
        # 필요한 바로 그때) 스윕이 돌지 않는다 — 플러그인 적재 시점에 띄운다
        _common()._ensure_worker()
    except Exception:
        pass
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
    ctx.register_hook("pre_approval_request", _on_pre_approval)
    ctx.register_hook("post_approval_response", _on_post_approval)
