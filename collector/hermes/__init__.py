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
from pathlib import Path

_COMMON_PATH = Path(__file__).resolve().parent / "madison_common.py"


def _common():
    mod = sys.modules.get("hermes_madison_common")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("hermes_madison_common", _COMMON_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_madison_common"] = mod
    spec.loader.exec_module(mod)
    return mod


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


def _on_pre_llm_call(session_id="", turn_id="", platform="", model="", **kw):
    try:
        c = _common()
        c.map_turn(turn_id, session_id)
        if turn_id and turn_id not in _seen_turns:
            _seen_turns.add(turn_id)
            if len(_seen_turns) > 1024:
                _seen_turns.clear()
                _seen_turns.add(turn_id)
            # prompt 본문은 worker가 state.db의 마지막 user 메시지로 채운다
            c.emit(session_id, "prompt", {"collection_mode": "hooks"}, enrich="turn")
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
    return (turn_id and c.sid_for_turn(turn_id)) or \
           (session_key and c.sid_for_key(session_key)) or session_key or ""


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
