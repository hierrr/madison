"""이벤트 수신(폴드)과 현재 상태 조립 — IMPLEMENTATION.md §4·§5의 규칙 구현."""
import json
from datetime import datetime, timedelta, timezone

from .config import CFG

# 전이 완결 규칙 (§4.1): 모든 이벤트가 상태를 정의한다
STATE_OF = {
    "session_start": "working",
    "prompt": "working",
    "tool_start": "working",
    "heartbeat": "working",
    "turn_done": "awaiting_input",
    "idle": "awaiting_input",
    "permission_request": "needs_approval",
    "session_end": "ended",
}

KNOWN_EVENTS = set(STATE_OF)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def ingest(c, device_id: int, ev: dict) -> str:
    """이벤트 1건 수신. 반환: inserted | duplicate | ignored.
    미지의 event 값도 저장은 하되(§14-3) 상태 폴드는 알려진 것만."""
    event = str(ev.get("event") or "")
    session_id = str(ev.get("session_id") or "unknown")
    agent = str(ev.get("agent") or "claude-code")
    ts_device = str(ev.get("ts") or utcnow())
    ts_hub = utcnow()
    payload = ev.get("detail") or {}
    project = ev.get("project")
    branch = ev.get("branch")
    event_id = ev.get("event_id")

    cur = c.execute(
        "INSERT OR IGNORE INTO events (device_id, agent, session_id, event_id, event,"
        " ts_device, ts_hub, project, branch, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (device_id, agent, session_id, event_id, event, ts_device, ts_hub,
         project, branch, json.dumps(payload, ensure_ascii=False)),
    )
    if cur.rowcount == 0:
        return "duplicate"  # event_id UNIQUE — 재전송 중복 흡수 (§5)

    if event not in KNOWN_EVENTS:
        return "ignored"

    row = c.execute(
        "SELECT * FROM sessions WHERE device_id=? AND agent=? AND session_id=?",
        (device_id, agent, session_id),
    ).fetchone()

    new_state = STATE_OF[event]
    if row is None:
        c.execute(
            "INSERT INTO sessions (device_id, agent, session_id, project, branch, state,"
            " state_ts, state_since, started_at, last_seen_hub) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (device_id, agent, session_id, project, branch, new_state,
             ts_device, ts_hub, ts_hub, ts_hub),
        )
        row = c.execute(
            "SELECT * FROM sessions WHERE device_id=? AND agent=? AND session_id=?",
            (device_id, agent, session_id),
        ).fetchone()

    sets, args = ["last_seen_hub=?"], [ts_hub]

    # 상태 갱신은 ts_device 가드(§5 폴드 정렬) — 스풀 지연 도착이 상태를 역행시키지 않게
    fresh = (row["state_ts"] or "") <= ts_device
    if fresh:
        if new_state != row["state"]:
            sets += ["state=?", "state_since=?"]
            args += [new_state, ts_hub]
        sets += ["state_ts=?"]
        args += [ts_device]
        if project:
            sets += ["project=?"]; args += [project]
        if branch:
            sets += ["branch=?"]; args += [branch]

    # 프런트엔드(cli/app): 실려 오면 반영
    if payload.get("frontend"):
        sets += ["frontend=?"]; args += [str(payload["frontend"])[:20]]
    if payload.get("collection_mode"):
        sets += ["collection_mode=?"]; args += [str(payload["collection_mode"])[:20]]

    # 모델·에포트: 어떤 이벤트든 실려 오면 최신값으로 반영
    if payload.get("model"):
        sets += ["model=?"]; args += [str(payload["model"])[:60]]
    eff = payload.get("effort")
    if isinstance(eff, dict):  # 훅에 따라 {"level": "max"} 객체 형태 (실측)
        eff = eff.get("level")
    if eff:
        sets += ["effort=?"]; args += [str(eff)[:20]]

    # 이벤트별 부가 필드 (카운터·요약은 지연 도착과 무관하게 반영)
    if event == "prompt":
        # 새 지시 → 요약 무효화 (요약 품질을 위해 원문을 넉넉히 보존)
        sets += ["last_prompt=?", "task_summary=NULL"]
        args += [str(payload.get("prompt", ""))[:600]]
    elif event == "turn_done":
        # 턴 수는 에이전트 종류와 무관하게 완료 이벤트 기준으로 통일한다.
        sets += ["turns=turns+1", "last_summary=?", "current_tool=NULL"]
        args += [str(payload.get("summary", ""))[:300]]
    elif event == "idle":
        sets += ["current_tool=NULL"]
    elif event == "tool_start" and fresh:
        sets += ["current_tool=?"]; args += [str(payload.get("tool", ""))[:60]]
    elif event == "permission_request":
        sets += ["approval_msg=?"]; args += [str(payload.get("message", ""))[:300]]
    elif event == "session_start":
        # 부활 규칙 (§4.1): ended 필드 클리어
        sets += ["ended_at=NULL", "end_reason=NULL"]
    elif event == "session_end" and fresh:
        sets += ["ended_at=?", "end_reason=?"]
        args += [ts_hub, str(payload.get("reason", ""))[:60]]

    args += [device_id, agent, session_id]
    c.execute(
        f"UPDATE sessions SET {', '.join(sets)} WHERE device_id=? AND agent=? AND session_id=?",
        args,
    )
    c.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (ts_hub, device_id))
    return "inserted"


def _age_min(now: datetime, ts: str | None) -> int | None:
    dt = _parse_ts(ts) if ts else None
    return int((now - dt).total_seconds() // 60) if dt else None


def assemble(c) -> dict:
    """GET /api/state 응답 — 세션·기기·주의필요·핸드오프·KPI·스파크."""
    now = datetime.now(timezone.utc)
    ttl = CFG.ttl_stale_min

    devices = {}
    for d in c.execute("SELECT * FROM devices WHERE revoked=0 ORDER BY name"):
        age = _age_min(now, d["last_seen_at"])
        devices[d["id"]] = {
            "id": d["id"], "name": d["name"],
            "last_seen_min": age,
            "online": age is not None and age <= CFG.device_online_min,
            "week_turns": 0, "spark": [],
        }

    # 기기별 최근 7일 turn_done 스파크
    for d in devices.values():
        rows = c.execute(
            "SELECT date(ts_hub, 'localtime') AS day, COUNT(*) AS n FROM events e"
            " WHERE device_id=? AND event='turn_done'"
            "   AND ts_hub >= datetime('now', '-7 days')"
            " AND NOT EXISTS (SELECT 1 FROM sessions x WHERE x.device_id=e.device_id AND x.agent=e.agent AND x.session_id=e.session_id AND x.frontend='auto')"
            " GROUP BY day", (d["id"],),
        ).fetchall()
        by_day = {r["day"]: r["n"] for r in rows}
        days = [(now.astimezone().date() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        d["spark"] = [{"day": day, "n": by_day.get(day, 0)} for day in days]
        d["week_turns"] = sum(by_day.values())

    hide_before = (now - timedelta(hours=CFG.ended_hide_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sessions = []
    for s in c.execute(
        "SELECT rowid AS row_id, * FROM sessions WHERE NOT (state='ended' AND ended_at < ?)"
        " ORDER BY last_seen_hub DESC", (hide_before,),
    ):
        dev = devices.get(s["device_id"])
        if dev is None:
            continue
        seen_min = _age_min(now, s["last_seen_hub"]) or 0
        # unconfirmed 오버레이 (§4.1): working만 TTL 판정, 대기 상태는 기기 오프라인일 때만
        unconfirmed = False
        if s["state"] == "working" and seen_min > ttl:
            unconfirmed = True
        elif s["state"] in ("awaiting_input", "needs_approval") and not dev["online"]:
            unconfirmed = True
        sessions.append({
            "id": s["row_id"],
            "device": dev["name"], "agent": s["agent"], "session_id": s["session_id"],
            "project": s["project"], "branch": s["branch"],
            "state": s["state"], "unconfirmed": unconfirmed,
            "state_min": _age_min(now, s["state_since"]) or 0,
            "seen_min": seen_min,
            "last_prompt": s["last_prompt"], "last_summary": s["last_summary"],
            "task_summary": s["task_summary"],
            "model": s["model"], "effort": s["effort"], "frontend": s["frontend"],
            "collection_mode": s["collection_mode"],
            "approval_msg": s["approval_msg"], "current_tool": s["current_tool"],
            "turns": s["turns"], "end_reason": s["end_reason"],
            "partial": s["agent"] != "claude-code" and s["collection_mode"] != "hooks",
        })

    order = {"needs_approval": 0, "awaiting_input": 1}
    attention = sorted(
        (s for s in sessions if s["state"] in order and not s["unconfirmed"]),
        key=lambda s: (order[s["state"]], -s["state_min"]),
    )

    # KPI: 오늘 완료 턴 + 어제 같은 시각까지와 비교
    today = c.execute(
        "SELECT COUNT(*) AS n FROM events e WHERE event='turn_done'"
        " AND date(ts_hub,'localtime') = date('now','localtime')"
        " AND NOT EXISTS (SELECT 1 FROM sessions x WHERE x.device_id=e.device_id AND x.agent=e.agent AND x.session_id=e.session_id AND x.frontend='auto')").fetchone()["n"]
    ydelta = c.execute(
        "SELECT COUNT(*) AS n FROM events e WHERE event='turn_done'"
        " AND date(ts_hub,'localtime') = date('now','localtime','-1 day')"
        " AND time(ts_hub,'localtime') <= time('now','localtime')"
        " AND NOT EXISTS (SELECT 1 FROM sessions x WHERE x.device_id=e.device_id AND x.agent=e.agent AND x.session_id=e.session_id AND x.frontend='auto')").fetchone()["n"]
    spark12 = c.execute(
        "SELECT strftime('%Y-%m-%dT%H', ts_hub, 'localtime') AS h, COUNT(*) AS n"
        " FROM events e WHERE event='turn_done' AND ts_hub >= datetime('now','-12 hours')"
        " AND NOT EXISTS (SELECT 1 FROM sessions x WHERE x.device_id=e.device_id AND x.agent=e.agent AND x.session_id=e.session_id AND x.frontend='auto')"
        " GROUP BY h").fetchall()
    by_hour = {r["h"]: r["n"] for r in spark12}
    hours = []
    local_now = now.astimezone()
    for i in range(11, -1, -1):
        h = (local_now - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        hours.append({"hour": h[-2:] + "시", "n": by_hour.get(h, 0)})

    handoffs = []
    for h in c.execute(
        "SELECT h.*, fd.name AS from_name, td.name AS to_name FROM handoffs h"
        " LEFT JOIN devices fd ON fd.id = h.from_device"
        " JOIN devices td ON td.id = h.to_device"
        " WHERE h.status IN ('pending','delivered')"
        " ORDER BY h.id DESC LIMIT 10"):
        handoffs.append({
            "id": h["id"], "hf": f"HF-{h['id']:03d}",
            "from": h["from_name"], "to": h["to_name"],
            "repo": h["repo"], "branch": h["branch"], "doc_path": h["doc_path"],
            "summary": h["summary"], "doc": h["doc"], "status": h["status"],
            "created_at": h["created_at"], "delivered_at": h["delivered_at"],
        })

    active = [s for s in sessions if s["state"] != "ended"]
    return {
        "generated_at": utcnow(),
        "ttl_min": ttl,
        "devices": list(devices.values()),
        "sessions": sessions,
        "attention": attention,
        "handoffs": handoffs,
        "kpi": {
            "attention": len(attention),
            "approval": sum(1 for s in attention if s["state"] == "needs_approval"),
            "awaiting": sum(1 for s in attention if s["state"] == "awaiting_input"),
            "working": sum(1 for s in active if s["state"] == "working" and not s["unconfirmed"]),
            "unconfirmed": sum(1 for s in active if s["unconfirmed"]),
            "devices_online": sum(1 for d in devices.values() if d["online"]),
            "devices_total": len(devices),
            "today_turns": today,
            "yesterday_turns_same_time": ydelta,
            "spark12": hours,
        },
    }


def feed(c, limit: int = 50) -> list[dict]:
    rows = c.execute(
        "SELECT e.id AS eid, e.ts_hub, e.event, e.agent, e.session_id, e.project,"
        " d.name AS device, e.payload, s.task_summary, s.last_prompt, s.frontend,"
        " s.rowid AS srow"
        " FROM events e JOIN devices d ON d.id=e.device_id"
        " LEFT JOIN sessions s ON s.device_id=e.device_id AND s.agent=e.agent"
        "   AND s.session_id=e.session_id"
        " WHERE e.event != 'heartbeat'"
        " ORDER BY e.id DESC LIMIT ?", (limit if limit > 0 else -1,),  # 0 = 무제한
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        # 태스크 = 세션의 작업 요약. 내용 = 이벤트 고유 정보만(원문 출력·프롬프트는 싣지 않음)
        task = r["task_summary"] or r["last_prompt"] or ""
        note = (payload.get("message") or payload.get("tool")
                or payload.get("reason") or payload.get("source") or "")
        out.append({
            "id": r["eid"],
            "ts": r["ts_hub"], "event": r["event"], "device": r["device"],
            "agent": r["agent"], "session_id": r["session_id"],
            "project": r["project"], "frontend": r["frontend"],
            "sid_num": r["srow"],
            "task": str(task)[:120],
            "note": str(note)[:120],
        })
    return out
