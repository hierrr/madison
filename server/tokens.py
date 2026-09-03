"""세션 누적 토큰 → 일별 집계 폴드와 조회.

수집기는 턴 종료마다 세션 **누적** 토큰 맵 {model: {in,out,cr,cw,th}}을 이벤트에 싣는다.
허브는 직전 누적(sessions.tokens_cum)과의 차이만 token_daily에 가산한다 — 재전송·스풀
유실에 멱등. 원칙: 폴드가 어떤 이유로 실패해도 ingest는 계속되어야 한다(호출부 try/except).
"""
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("madison.tokens")

DEVICE_ENV = Path.home() / ".claude" / "madison" / "env"
_hub_dev = {"id": None, "at": 0.0}   # LLM 호출마다 env 파일·devices 조회를 반복하지 않게 캐시
HUB_DEV_TTL = 300


def hub_device_id(c) -> int | None:
    """이 허브 기기의 devices.id — 수집기 env의 MADISON_DEVICE로 식별. 미등록이면 None."""
    now = time.time()
    if _hub_dev["id"] is not None and now - _hub_dev["at"] < HUB_DEV_TTL:
        return _hub_dev["id"]
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
    if row:
        _hub_dev.update(id=row["id"], at=now)
    return row["id"] if row else None


def claude_counts(u: dict) -> dict:
    """Claude API/전사본/봉투의 snake_case usage → {in,out,cr,cw,th}.
    분류 규칙의 정본 — llm.py(봉투)·backfill이 공유하고, collector/collect_tokens_local.py와
    report.sh(jq)는 배포 형태상 사본을 가진다(수정 시 함께 맞출 것)."""
    det = u.get("output_tokens_details")
    return {"in": int(u.get("input_tokens") or 0), "out": int(u.get("output_tokens") or 0),
            "cr": int(u.get("cache_read_input_tokens") or 0),
            "cw": int(u.get("cache_creation_input_tokens") or 0),
            "th": int(det.get("thinking_tokens") or 0) if isinstance(det, dict) else 0}


def codex_counts(u: dict) -> dict:
    """Codex usage → {in,out,cr,cw,th}. cached는 input의 부분집합 → 분리(Claude와 의미 통일)."""
    raw_in = int(u.get("input_tokens") or 0)
    cached = int(u.get("cached_input_tokens") or 0)
    return {"in": max(0, raw_in - cached), "out": int(u.get("output_tokens") or 0),
            "cr": cached, "cw": int(u.get("cache_write_input_tokens") or 0),
            "th": int(u.get("reasoning_output_tokens") or 0)}

KINDS = ("in", "out", "cr", "cw", "th")
COLS = {"in": "input", "out": "output", "cr": "cache_read", "cw": "cache_write", "th": "thinking"}
_CODEX_KEY = "_session"   # Codex 카운터는 세션 전역 — 모델 전환 시 이중 계상 방지용 고정 기준점
# 허브 내부 LLM 워커의 전용 cwd 이름 — 이 프로젝트의 사용량은 항상 자동화로 분류한다
# (llm-cwd: 현행 LLM_CWD 기본값, summarizer: 과거 워커 디렉터리 — 백필 데이터에 남아 있음)
AUTO_PROJECTS = ("summarizer", "llm-cwd")


def auto_frontend(project: str, frontend: str) -> str:
    """허브 워커 프로젝트는 frontend 미상이어도 'auto'로 — '자동화 제외' 필터가 걸리게."""
    return "auto" if project in AUTO_PROJECTS else frontend


def _day_local(ts_device: str, ts_hub: str) -> str:
    """UTC ISO → 허브 로컬 날짜. ts_device 우선(스풀 지연 도착 대비), 파싱 실패 시 ts_hub."""
    for ts in (ts_device, ts_hub):
        if not ts:
            continue
        try:
            parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d")
    return time.strftime("%Y-%m-%d")


def _counts(v) -> dict | None:
    """{kind: 정수} 검증·정수화. 불량 필드는 0, 객체가 아니면 버림."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k in KINDS:
        n = v.get(k, 0)
        out[k] = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0 else 0
    return out


def _clean(cum) -> dict:
    """수집기가 보낸 누적 맵 {model: {kind:int}} 정규화. 빈/불량은 {}."""
    if not isinstance(cum, dict):
        return {}
    out = {}
    for model, v in cum.items():
        counts = _counts(v)
        if counts is not None and isinstance(model, str) and model:
            out[model[:60]] = counts
    return out


def _delta(old, new: dict) -> dict:
    """필드별 max(0, new-old). 카운터 역행(리셋)은 0 — 이중 계상보다 소량 누락이 안전."""
    old = _counts(old) or {k: 0 for k in KINDS}
    return {k: max(0, new[k] - old[k]) for k in KINDS}


def fold(c, device_id: int, agent: str, session_id: str, *, project: str, model: str,
         frontend: str, ts_device: str, ts_hub: str, tokens: dict, count_turn: bool):
    """이벤트의 tokens.cum을 델타로 바꿔 token_daily에 가산하고 기준점을 갱신한다."""
    cum = _clean(tokens.get("cum"))
    if not cum:
        return
    row = c.execute("SELECT tokens_cum FROM sessions WHERE device_id=? AND agent=? AND session_id=?",
                    (device_id, agent, session_id)).fetchone()
    try:
        old = json.loads(row["tokens_cum"]) if row and row["tokens_cum"] else {}
    except (ValueError, TypeError):
        old = {}
    if not isinstance(old, dict):
        old = {}

    if agent == "codex-cli":
        # 세션 전역 카운터 — 기준점은 _session 고정 키, 델타는 실려온 모델명으로 귀속
        model_key, new_total = next(iter(cum.items()))
        deltas = {(model or model_key): _delta(old.get(_CODEX_KEY), new_total)}
        stored = {_CODEX_KEY: new_total}
    else:
        # Claude: 모델별 합은 한 전사본 안에서 단조증가 — 모델별 누적 카운터로 유효
        deltas = {m: _delta(old.get(m), v) for m, v in cum.items()}
        stored = cum

    day = _day_local(ts_device, ts_hub)
    for m, d in deltas.items():
        if not any(d.values()) and not count_turn:
            continue
        add_daily(c, day, device_id, agent, project, m, frontend, "events", d,
                  turns=1 if count_turn else 0)
        count_turn = False   # 턴 수는 이벤트당 1회만 (모델 여러 개여도)
    c.execute("UPDATE sessions SET tokens_cum=? WHERE device_id=? AND agent=? AND session_id=?",
              (json.dumps(stored, ensure_ascii=False), device_id, agent, session_id))


def add_daily(c, day, device_id, agent, project, model, frontend, source, counts: dict, turns=0):
    """token_daily에 가산 UPSERT — fold(이벤트)·llm._account_tokens(허브 워커)·백필이 공유."""
    c.execute(
        "INSERT INTO token_daily (day, device_id, agent, project, model, frontend, source,"
        " input, output, cache_read, cache_write, thinking, turns)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(day, device_id, agent, project, model, frontend, source) DO UPDATE SET"
        " input=input+excluded.input, output=output+excluded.output,"
        " cache_read=cache_read+excluded.cache_read, cache_write=cache_write+excluded.cache_write,"
        " thinking=thinking+excluded.thinking, turns=turns+excluded.turns",
        (day, device_id, agent, (project or "")[:120], (model or "")[:60], (frontend or "")[:20],
         source, counts["in"], counts["out"], counts["cr"], counts["cw"], counts["th"], turns))


# ── 조회 ─────────────────────────────────────────────

_SUM = ("SUM(input) input, SUM(output) output, SUM(cache_read) cache_read,"
        " SUM(cache_write) cache_write, SUM(thinking) thinking, SUM(turns) turns")


_HEXDIR = re.compile(r"^[0-9a-f]{16,}$")


def _pretty(project: str) -> str:
    """표시용 프로젝트명 — 태스크·리포트 표기와 같은 눈높이로 한 번 정리:
    해시 이름 임시 디렉터리는 (임시)로 묶는다. 빈 값은 빈 값 그대로 —
    "unknown"으로 바꾸면 실제 그 이름인 프로젝트와 구분이 안 되므로 표시는 클라이언트가
    흐린 이탤릭으로 처리한다."""
    if not project:
        return ""
    if _HEXDIR.match(project):
        return "(임시)"
    return project


def _service_projects(reg, service: str) -> list:
    """서비스 필터 → 그 서비스로 귀속되는 프로젝트 목록(매핑 + 이름 자체)."""
    projs = [p for p, m in reg.project_map.items() if m.get("service") == service]
    return projs + [service]   # 미매핑 프로젝트는 자기 이름이 서비스


def summary(c, reg, *, days: int = 30, agent: str = "", device: str = "", service: str = "",
            model: str = "", human: bool = False) -> dict:
    """token_daily 집계 — 일별 시계열 + 기기/프로젝트/서비스/모델/에이전트/세션별 내역."""
    where, args = ["1=1"], []
    if days and days > 0:
        where.append("t.day >= date('now','localtime', ?)")
        args.append(f"-{int(days) - 1} days")
    if agent:
        where.append("t.agent=?"); args.append(agent)
    if device:
        where.append("t.device_id IN (SELECT id FROM devices WHERE name=?)"); args.append(device)
    if model:
        where.append("t.model=?"); args.append(model)
    if human:
        where.append("t.frontend != 'auto'")
    if service:
        projs = _service_projects(reg, service)
        where.append(f"t.project IN ({','.join('?' * len(projs))})"); args.extend(projs)
    pred = " AND ".join(where)

    def rows(group_sql, key):
        # ORDER BY는 별칭이 아니라 SUM으로 — 바깥 표현식의 bare input/output은 원 컬럼으로 풀린다
        return [dict(r) for r in c.execute(
            f"SELECT {group_sql} {key}, {_SUM} FROM token_daily t WHERE {pred}"
            f" GROUP BY {group_sql} ORDER BY SUM(input)+SUM(output) DESC", args)]

    day_rows = [dict(r) for r in c.execute(
        f"SELECT t.day day, {_SUM} FROM token_daily t WHERE {pred} GROUP BY t.day ORDER BY t.day", args)]
    # 프로젝트·서비스 이름은 태스크·리포트와 같은 처리를 거친다 — 레지스트리 매핑 + 정리 버킷
    per_project: dict = {}
    per_service: dict = {}
    for r in rows("t.project", "project"):
        pname = _pretty(r["project"])
        svc = reg.service(r["project"]) if r["project"] else ""
        if svc == (r["project"] or ""):
            svc = pname   # 미매핑 프로젝트는 정리된 이름 그대로가 서비스
        for bucket, key, name in ((per_project, "project", pname), (per_service, "service", svc)):
            acc = bucket.setdefault(name, {key: name, **{COLS[k]: 0 for k in KINDS}, "turns": 0})
            for k in KINDS:
                acc[COLS[k]] += r[COLS[k]] or 0
            acc["turns"] += r["turns"] or 0
    per_project = sorted(per_project.values(), key=lambda r: -(r["input"] + r["output"]))
    per_device = [dict(r) for r in c.execute(
        f"SELECT d.name device, {_SUM} FROM token_daily t JOIN devices d ON d.id=t.device_id"
        f" WHERE {pred} GROUP BY d.name ORDER BY SUM(input)+SUM(output) DESC", args)]
    total_row = c.execute(f"SELECT {_SUM} FROM token_daily t WHERE {pred}", args).fetchone()
    total = {k: (total_row[k] or 0) for k in ("input", "output", "cache_read", "cache_write", "thinking", "turns")}
    return {
        "days": day_rows,
        "by_device": per_device,
        "by_project": per_project,
        "by_service": sorted(per_service.values(), key=lambda r: -(r["input"] + r["output"])),
        "by_model": rows("t.model", "model"),
        "by_agent": rows("t.agent", "agent"),
        "by_session": _by_session(c, reg, days=days, agent=agent, device=device,
                                  service=service, human=human),
        "total": total,
    }


def _by_session(c, reg, *, days: int, agent: str, device: str, service: str,
                human: bool, limit: int = 20) -> list:
    """세션별 상위 — sessions.tokens_cum(누적 JSON) 합산. 라이브 수집분만(백필엔 세션 정보 없음)."""
    where, args = ["s.tokens_cum IS NOT NULL"], []
    if days and days > 0:
        where.append("s.last_seen_hub >= datetime('now', ?)")
        args.append(f"-{int(days)} days")
    if agent:
        where.append("s.agent=?"); args.append(agent)
    if device:
        where.append("d.name=?"); args.append(device)
    if human:
        where.append("COALESCE(s.frontend,'') != 'auto'")
    if service:
        projs = _service_projects(reg, service)
        where.append(f"s.project IN ({','.join('?' * len(projs))})"); args.extend(projs)
    out = []
    for r in c.execute(
            "SELECT d.name device, s.agent, s.session_id, s.project, s.model, s.tokens_cum,"
            " s.started_at, s.last_seen_hub FROM sessions s JOIN devices d ON d.id=s.device_id"
            f" WHERE {' AND '.join(where)}", args):
        try:
            cum = json.loads(r["tokens_cum"])
        except (ValueError, TypeError):
            continue
        tot = {k: 0 for k in KINDS}
        for v in (cum or {}).values():
            counts = _counts(v)
            if counts:
                for k in KINDS:
                    tot[k] += counts[k]
        if not any(tot.values()):
            continue
        out.append({"device": r["device"], "agent": r["agent"], "session_id": r["session_id"],
                    "project": r["project"] or "", "service": reg.service(r["project"] or ""),
                    "model": r["model"] or "", "started_at": r["started_at"],
                    "last_seen": r["last_seen_hub"], **{COLS[k]: tot[k] for k in KINDS}})
    out.sort(key=lambda s: -(s["input"] + s["output"]))
    return out[:limit]
