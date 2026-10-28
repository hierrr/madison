"""MADISON 허브 — 단일 FastAPI 앱이 API와 대시보드를 함께 서빙한다.
LLM 실행은 llm.py, 요약 워커는 summary.py, 리포트 생성·스케줄은 reporting.py — 여기는 라우트와 인증 접착만."""
import contextlib
import datetime
import json
import logging
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from . import auth, db, llm, llm_meta, registry, report, reporting, state, summary, tokens, usage
from .config import CFG, REPO_ROOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("madison")


@contextlib.asynccontextmanager
async def _lifespan(_app):
    db.conn()
    with db.tx() as c:
        n = registry.seed_from_env(c)
        if n:
            log.info("registry: .env에서 서비스 %d개 시드", n)
    if CFG.retention_days > 0:
        threading.Thread(target=_retention_loop, daemon=True).start()
    if CFG.task_summary_enabled:
        threading.Thread(target=summary.loop, daemon=True).start()
    if CFG.report_enabled:
        threading.Thread(target=reporting.loop, daemon=True).start()
    if CFG.usage_enabled:
        threading.Thread(target=usage.loop, daemon=True).start()
    yield


app = FastAPI(title="MADISON", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

DASHBOARD_HTML = REPO_ROOT / "dashboard" / "index.html"
ASSETS_DIR = REPO_ROOT / "dashboard" / "assets"
COLLECTOR_DIR = REPO_ROOT / "collector"

# 기계용 호스트(madison-api.*)가 응답하는 경로 (§8.1: /api/*와 설치 파일만)
API_HOST_ALLOWED_PREFIXES = ("/api/", "/collector/")
API_HOST_ALLOWED_EXACT = {"/install.sh", "/install.ps1", "/healthz", "/favicon.svg"}

COLLECTOR_FILES = {
    "report.sh", "flush.sh", "install.sh", "install.ps1", "report.ps1",
    "hooks.template.json", "codex-hooks.template.json",
    "skills/handoff/SKILL.md", "skills/pickup/SKILL.md",
    "collect_tokens_local.py",   # 토큰 백필 운반 — 허브 없는 기기에서 집계 JSON 생성
}


@app.middleware("http")
async def host_guard(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0]
    path = request.url.path
    if host == CFG.api_host:
        if not (path.startswith(API_HOST_ALLOWED_PREFIXES) or path in API_HOST_ALLOWED_EXACT):
            return PlainTextResponse("not found", status_code=404)
    return await call_next(request)


def _actor(request: Request) -> dict:
    # 기기 토큰 조회만 DB 락 안에서, JWT 검증(네트워크)은 락 밖에서 — 단일 락·루프 블로킹 방지
    with db.tx() as c:
        device = auth.classify_device(request, c)
    if device:
        return device
    return auth.classify_nodb(request)


def _csrf_guard(request: Request):
    """브라우저發 크로스사이트 요청 차단 (CSRF).
    Sec-Fetch-Site: same-origin/none만 허용 — cross-site/same-site는 거부.
    헤더 부재 = 비브라우저(curl·collector) → 허용. Bearer 토큰 요청은 쿠키 인증이
    아니라 CSRF 대상이 아니므로 면제(대시보드 fetch는 same-origin이라 어차피 통과)."""
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "cross-site 요청 거부 (CSRF 방어)")


def _require(request: Request, kinds: tuple[str, ...], *, state_change: bool = False) -> dict:
    if state_change:
        _csrf_guard(request)
    actor = _actor(request)
    # local은 admin을 포함한다
    if actor["kind"] == "local" and "admin" in kinds:
        return actor
    if actor["kind"] not in kinds:
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
        log.warning("auth 401 %s %s kind=%s ip=%s", request.method, request.url.path, actor["kind"], ip)
        raise HTTPException(401, "인증 실패")
    return actor


# ── 등록 ──────────────────────────────────────────────

@app.post("/api/enroll")
async def enroll(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    secret = str(body.get("secret") or "")
    if not CFG.enroll_secret:
        raise HTTPException(403, "등록이 비활성화되어 있습니다 (ENROLL_SECRET 미설정)")
    import secrets as pysecrets
    if not pysecrets.compare_digest(secret, CFG.enroll_secret):
        raise HTTPException(403, "등록 암호가 틀렸습니다")
    if not name or len(name) > 32 or not all(ch.isalnum() or ch in "-_" for ch in name):
        raise HTTPException(400, "기기 이름은 영숫자/-/_ 32자 이내")
    token = auth.new_token()
    with db.tx() as c:
        dup = c.execute("SELECT id FROM devices WHERE name=?", (name,)).fetchone()
        if dup:
            raise HTTPException(409, f"'{name}'은 이미 등록됨 — 재발급은 대시보드에서 기존 기기 폐기 후")
        cur = c.execute(
            "INSERT INTO devices (name, token_hash, created_at) VALUES (?,?,?)",
            (name, auth.token_hash(token), state.utcnow()),
        )
        device_id = cur.lastrowid
    return {"device_id": device_id, "name": name, "token": token}


# ── 이벤트 ────────────────────────────────────────────

@app.post("/api/events")
async def post_events(request: Request):
    actor = _require(request, ("device",))
    body = await request.json()
    events = body if isinstance(body, list) else [body]
    if len(events) > 500:
        raise HTTPException(413, "배치는 500건 이하")
    result = {"inserted": 0, "duplicate": 0, "ignored": 0}
    with db.tx() as c:
        for ev in events:
            if not isinstance(ev, dict):
                continue
            result[state.ingest(c, actor["device"]["id"], ev)] += 1
    return JSONResponse(result, status_code=202)


# ── 조회 ──────────────────────────────────────────────

@app.get("/api/state")
async def get_state(request: Request):
    _require(request, ("device", "admin"))
    with db.tx() as c:
        return state.assemble(c)


@app.get("/api/feed")
async def get_feed(request: Request, limit: int = 50, page: int = 0, device: str = "",
                   project: str = "", days: int = 0, noauto: int = 0):
    """page=0(기본)은 기존 리스트 응답(메인 피드·구형 클라이언트).
    page>=1이면 이벤트 탭용 {rows,total,page,pages,facets} — 필터·페이지 단위로만 싣는다."""
    _require(request, ("device", "admin"))
    with db.tx() as c:
        if page < 1:
            return state.feed(c, limit)
        per = limit if 1 <= limit <= 200 else 20
        return state.feed_page(c, page, per, device, project, days, bool(noauto))


@app.get("/api/history/events")
async def history_events(request: Request, device: str = "", agent: str = "",
                         session_id: str = "", limit: int = 300):
    """특정 세션의 이벤트 원장 — 자동화 탭의 펼침 보기용."""
    _require(request, ("admin",))
    with db.tx() as c:
        dev = c.execute("SELECT id FROM devices WHERE name=?", (device,)).fetchone()
        if not dev:
            raise HTTPException(404, "기기 없음")
        sess = c.execute(
            "SELECT task_summary FROM sessions WHERE device_id=? AND agent=? AND session_id=?",
            (dev["id"], agent, session_id)).fetchone()
        rows = c.execute(
            "SELECT id, event, ts_hub, payload FROM events"
            " WHERE device_id=? AND agent=? AND session_id=?"
            " ORDER BY id LIMIT ?",
            (dev["id"], agent, session_id, min(limit, 1000))).fetchall()
    task_summary = sess["task_summary"] if sess else None
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            p = {}
        # 원문(프롬프트·응답) 대신 요약/이벤트 고유 정보만
        if r["event"] in ("prompt", "turn_done"):
            note = task_summary or ""
        else:
            note = (p.get("message") or p.get("tool") or p.get("reason")
                    or p.get("source") or "")
        out.append({"id": r["id"], "event": r["event"], "ts": r["ts_hub"],
                    "note": str(note)[:120]})
    return out


def _is_unconfirmed(st: str, last_seen_hub, device_seen_at, now=None, frontend=None) -> bool:
    """'신호 없음' 판정 — /api/state(state.assemble §4.1)와 같은 규칙(state.is_unconfirmed): working은 TTL 초과,
    대기 상태는 기기 오프라인, 헤드리스(frontend=auto)는 종료 전까지 상태 무관 TTL 초과."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seen = state._age_min(now, last_seen_hub) or 0
    dev_age = state._age_min(now, device_seen_at)
    dev_online = dev_age is not None and dev_age <= CFG.device_online_min
    return state.is_unconfirmed(st, frontend, seen, dev_online, CFG.ttl_stale_min)


def _overlay_unconfirmed(rows: list[dict]):
    """unconfirmed 오버레이 — 행마다 _is_unconfirmed."""
    now = datetime.datetime.now(datetime.timezone.utc)
    for r in rows:
        r["unconfirmed"] = _is_unconfirmed(r["state"], r["last_seen_hub"], r.pop("device_seen_at", None), now,
                                           frontend=r.get("frontend"))
    return rows


def end_reason_for(c, device_id: int, agent: str, session_id: str) -> str | None:
    """수동 종료 처리의 사유 — 신호가 끊긴 세션이면 'lost'(신호 끊김), 살아 있는 세션을 사람이 닫으면 'manual'.
    나중에 봐도 왜 종료됐는지 알 수 있게 허브가 판정해 기록한다(2026-10-01 사용자 요청). 세션 없으면 None."""
    row = c.execute(
        "SELECT s.state, s.frontend, s.last_seen_hub, d.last_seen_at AS device_seen_at FROM sessions s"
        " JOIN devices d ON d.id=s.device_id WHERE s.device_id=? AND s.agent=? AND s.session_id=?",
        (device_id, agent, session_id)).fetchone()
    if row is None:
        return None
    return "lost" if _is_unconfirmed(row["state"], row["last_seen_hub"], row["device_seen_at"],
                                     frontend=row["frontend"]) else "manual"


# 태스크·자동화 탭 페이지 행 — s.* 대신 표시 컬럼만 (last_summary·tokens_cum 등 큰 필드 제외)
_SESSION_PAGE_COLS = (
    "s.rowid AS row_id, d.name AS device, s.agent, s.session_id, s.project, s.branch,"
    " s.state, s.end_reason, s.turns, s.model, s.effort, s.frontend, s.started_at,"
    " s.last_seen_hub, s.task_summary, s.session_name,"
    " substr(COALESCE(s.last_prompt,''), 1, 200) AS last_prompt,"
    " d.last_seen_at AS device_seen_at"
)


# 실질 세션 = 프롬프트를 한 번이라도 받았거나(=last_prompt 있음) 턴을 돈 것.
# 프롬프트·턴 없이 뜬 세션(모델 목록 프로브 등 헤드리스 호출, 입력 없이 닫은 창)은
# 보여줄 작업 내용이 없으므로 태스크·자동화 탭에서 감춘다 — 실행 방식과 무관한 판정이라
# 프로젝트가 늘어도 그대로 적용된다. 원장(이벤트 히스토리)에는 그대로 남는다.
_SUBSTANTIVE_SQL = "(s.turns > 0 OR COALESCE(s.last_prompt, '') != '')"

# 검색 — 세션 행은 마지막 프롬프트·요약만 들고 있어, 세션 중간에 한 작업은 이벤트 원장의 지시(prompt)·응답 요약(summary)까지
# 봐야 찾힌다(2026-10-28). payload 전체가 아니라 두 필드만 — 'cli'·'model' 같은 키·값 오탐 방지.
# hermes는 prompt·turn_done 양쪽에 두 필드를 다 싣기도 해서 이벤트 종류와 필드를 교차로 본다.
_SEARCH_SESSION_COLS = ("s.task_summary", "s.last_prompt", "s.session_name")
_SEARCH_EVENT_SQL = (
    "EXISTS (SELECT 1 FROM events e WHERE e.device_id = s.device_id AND e.session_id = s.session_id"
    " AND e.agent = s.agent AND e.event IN ('prompt', 'turn_done')"
    " AND (json_extract(e.payload, '$.prompt') LIKE ? ESCAPE '\\'"
    "      OR json_extract(e.payload, '$.summary') LIKE ? ESCAPE '\\'))")


def _search_terms(q: str) -> list[str]:
    """검색어 → 공백으로 나눈 단어(최대 5개). 단어마다 AND — 같은 세션 안이면 서로 다른 턴에 있어도 맞는다."""
    return q.split()[:5]


def _like_pat(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _session_conds(device: str = "", agent: str = "", fe: str = "",
                   project: str = "", days: int = 0, auto: int = -1,
                   substantive: bool = True, state: str = "", q: str = "") -> tuple[list[str], list]:
    """세션 이력 공용 WHERE 조각. fe '-'는 프런트엔드 미기록, project '(unknown)'은 빈 값 매칭.
    substantive=True면 프롬프트·턴 없는 no-op 세션을 제외한다.
    state는 세션 상태값 그대로, 'stale' = 현황의 '신호 없음' 오버레이(_overlay_unconfirmed)와 같은 판정(working은 TTL 초과,
    대기 상태는 기기 오프라인 — 종료 훅을 못 받고 남은 좀비 세션용, 헤드리스(auto)는 종료 전까지 상태 무관 TTL 초과 — 2026-10-14),
    또는 'ended:<사유>' = 종료 사유별('-'는 사유 없음, 2026-10-01).
    q는 검색어 — 단어마다 세션 요약·마지막 프롬프트·세션 이름 또는 이벤트 원장의 지시·응답 요약 중 한 곳에 있으면 매칭(대소문자 무시는 ASCII만)."""
    conds, args = [], []
    for term in _search_terms(q):
        pat = _like_pat(term)
        conds.append("(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in _SEARCH_SESSION_COLS)
                     + " OR " + _SEARCH_EVENT_SQL + ")")
        args += [pat] * (len(_SEARCH_SESSION_COLS) + 2)
    if state == "stale":
        # 저장 형식이 'YYYY-MM-DDTHH:MM:SSZ'라 datetime()의 공백 구분 문자열과는 같은 날짜에서 비교가 어긋난다 — strftime으로 맞춘다
        fmt = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
        conds.append(
            f"(((s.state = 'working' OR (s.frontend = 'auto' AND s.state != 'ended')) AND s.last_seen_hub < {fmt})"
            " OR (s.state IN ('awaiting_input', 'needs_approval')"
            f"     AND (d.last_seen_at IS NULL OR d.last_seen_at < {fmt})))")
        args += [f"-{int(CFG.ttl_stale_min)} minutes", f"-{int(CFG.device_online_min)} minutes"]
    elif state.startswith("ended:"):
        reason = state[len("ended:"):]
        conds.append("s.state = 'ended'")
        if reason == "-":
            conds.append("COALESCE(s.end_reason, '') = ''")
        else:
            conds.append("s.end_reason = ?"); args.append(reason)
    elif state:
        conds.append("s.state = ?"); args.append(state)
    if substantive:
        conds.append(_SUBSTANTIVE_SQL)
    if auto == 1:
        conds.append("s.frontend = 'auto'")
    elif auto == 0:
        conds.append("(s.frontend IS NULL OR s.frontend != 'auto')")
    if device:
        conds.append("d.name = ?"); args.append(device)
    if agent:
        conds.append("s.agent = ?"); args.append(agent)
    if fe == "-":
        conds.append("(s.frontend IS NULL OR s.frontend = '')")
    elif fe:
        conds.append("s.frontend = ?"); args.append(fe)
    if project == "(unknown)":
        conds.append("(s.project IS NULL OR s.project = '')")
    elif project:
        conds.append("s.project = ?"); args.append(project)
    if days > 0:
        conds.append("s.last_seen_hub >= datetime('now', ?)"); args.append(f"-{int(days)} days")
    return conds, args


def _excerpt(text: str, term: str, before: int = 40, width: int = 140) -> str:
    """text에서 term 주변 발췌 — 줄바꿈은 공백으로, 앞뒤가 잘리면 말줄임표."""
    text = " ".join(text.split())
    start = max(text.lower().find(term.lower()) - before, 0)
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


def _match_snippet(c, device: str, agent: str, session_id: str, terms: list[str]) -> dict | None:
    """검색 결과 행의 근거 — 그 세션에서 검색어가 처음 나온 지시(prompt) 또는 응답 요약(summary) 발췌.
    모든 단어가 함께 든 턴을 먼저 찾고, 없으면(단어가 여러 턴에 흩어짐) 첫 단어가 든 턴.
    세션 행 컬럼(요약·세션 이름)으로만 맞았으면 None — 작업 칸에 이미 보인다."""
    text_sql = ("COALESCE(json_extract(e.payload, '$.prompt'), '') || char(10)"
                " || COALESCE(json_extract(e.payload, '$.summary'), '')")
    for group in (terms, terms[:1]):
        r = c.execute(
            "SELECT e.ts_hub, json_extract(e.payload, '$.prompt') AS prompt, json_extract(e.payload, '$.summary') AS summary"
            " FROM events e JOIN devices d ON d.id = e.device_id"
            " WHERE d.name = ? AND e.agent = ? AND e.session_id = ? AND e.event IN ('prompt', 'turn_done')"
            + "".join(f" AND {text_sql} LIKE ? ESCAPE '\\'" for _ in group) +
            " ORDER BY e.id LIMIT 1",
            [device, agent, session_id] + [_like_pat(t) for t in group]).fetchone()
        if r:
            field = "prompt" if terms[0].lower() in str(r["prompt"] or "").lower() else "summary"
            return {"field": field, "ts": r["ts_hub"], "text": _excerpt(str(r[field] or ""), terms[0])}
    return None


@app.get("/api/history/sessions")
async def history_sessions(request: Request, limit: int = 2000, days: int = 0,
                           page: int = 0, device: str = "", agent: str = "",
                           fe: str = "", project: str = "", auto: int = -1, state: str = "",
                           q: str = ""):
    """종료 포함 전체 세션 이력 — 태스크·자동화 탭용. 관리자 전용(기기 쪽 소비자 없음).
    page=0(기본)은 기존 전체 리스트 응답. page>=1이면 필터·페이지 단위 {rows,total,page,pages,facets}.
    state: working|awaiting_input|needs_approval|ended|stale(신호 없음)|ended:<사유> — 자동화·태스크 탭 상태 필터(2026-10-01).
    q: 검색어(_session_conds) — 있으면 행마다 맞은 지시·응답 발췌(match)를 붙인다(2026-10-28)."""
    _require(request, ("admin",))
    base_from = " FROM sessions s JOIN devices d ON d.id=s.device_id"
    if page < 1:
        q = "SELECT s.rowid AS row_id, s.*, d.name AS device, d.last_seen_at AS device_seen_at" + base_from
        args: list = []
        if days > 0:
            q += " WHERE s.last_seen_hub >= datetime('now', ?)"
            args.append(f"-{int(days)} days")
        q += " ORDER BY s.last_seen_hub DESC LIMIT ?"
        args.append(limit if limit > 0 else -1)  # 0 = 무제한
        with db.tx() as c:
            rows = [dict(r) for r in c.execute(q, args).fetchall()]
        return _overlay_unconfirmed(rows)

    per = limit if 1 <= limit <= 200 else 20
    conds, cargs = _session_conds(device, agent, fe, project, days, auto, state=state, q=q)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with db.tx() as c:
        total = c.execute("SELECT COUNT(*)" + base_from + where, cargs).fetchone()[0]
        pages = max(1, -(-total // per))
        page = min(max(page, 1), pages)
        rows = [dict(r) for r in c.execute(
            "SELECT " + _SESSION_PAGE_COLS + base_from + where +
            " ORDER BY s.last_seen_hub DESC LIMIT ? OFFSET ?",
            cargs + [per, (page - 1) * per]).fetchall()]
        terms = _search_terms(q)
        if terms:
            for r in rows:
                r["match"] = _match_snippet(c, r["device"], r["agent"], r["session_id"], terms)
        # 같은 필터에서 감춘 no-op 세션 수 — 조용히 버리지 않고 탭에 "N건 제외"로 알린다
        nconds, nargs = _session_conds(device, agent, fe, project, days, auto, substantive=False, state=state, q=q)
        nconds.append("NOT " + _SUBSTANTIVE_SQL)
        hidden = c.execute("SELECT COUNT(*)" + base_from +
                           " WHERE " + " AND ".join(nconds), nargs).fetchone()[0]
        # facets(드롭다운 옵션)는 탭 범위(auto 여부)만 반영 — 선택 필터·기간과 무관, no-op 제외
        fconds, fargs = _session_conds(auto=auto)
        fwhere = " WHERE " + " AND ".join(fconds)
        devices = [r[0] for r in c.execute(
            "SELECT DISTINCT d.name" + base_from + fwhere + " ORDER BY d.name", fargs)]
        agents = [{"agent": r[0], "frontend": r[1]} for r in c.execute(
            "SELECT DISTINCT s.agent, COALESCE(s.frontend,'')" + base_from + fwhere +
            " ORDER BY 1, 2", fargs)]
        projects = sorted({r[0] or "(unknown)" for r in c.execute(
            "SELECT DISTINCT COALESCE(s.project,'')" + base_from + fwhere, fargs)})
        # 종료 사유 선택지 — 탭 범위에 실제로 있는 사유만, 많은 순. ''(사유 없음)은 '-'로
        reasons = [r[0] or "-" for r in c.execute(
            "SELECT COALESCE(s.end_reason,'') AS r, COUNT(*) AS n" + base_from + fwhere +
            " AND s.state = 'ended' GROUP BY r ORDER BY n DESC, r", fargs)]
    return {"rows": _overlay_unconfirmed(rows), "total": total, "hidden": hidden,
            "page": page, "pages": pages,
            "facets": {"devices": devices, "agents": agents, "projects": projects, "reasons": reasons}}


@app.post("/api/sessions/end")
async def end_session(request: Request):
    """표시상 종료 처리(실제 프로세스 무관) — 종료 신호가 없는 세션 정리용. 관리자 전용."""
    _require(request, ("admin",), state_change=True)
    body = await request.json()
    name = str(body.get("device") or "")
    with db.tx() as c:
        dev = c.execute("SELECT id FROM devices WHERE name=?", (name,)).fetchone()
        if not dev:
            raise HTTPException(404, f"기기 '{name}' 없음")
        agent = str(body.get("agent") or "")
        sid = str(body.get("session_id") or "")
        reason = end_reason_for(c, dev["id"], agent, sid)
        if reason is None:
            raise HTTPException(404, "세션 없음")
        now = state.utcnow()
        c.execute(
            "UPDATE sessions SET state='ended', ended_at=?, end_reason=?, state_since=?"
            " WHERE device_id=? AND agent=? AND session_id=?",
            (now, reason, now, dev["id"], agent, sid),
        )
    return {"ok": True, "end_reason": reason}


@app.get("/api/devices")
async def list_devices(request: Request):
    actor = _require(request, ("device", "admin"))
    with db.tx() as c:
        if actor["kind"] == "device":
            # 기기 토큰: 핸드오프 대상 선택용 최소 정보 — 자기 자신 제외, 이름·온라인 여부만
            now = datetime.datetime.now(datetime.timezone.utc)
            return [
                {"name": r["name"],
                 "online": (age := state._age_min(now, r["last_seen_at"])) is not None
                           and age <= CFG.device_online_min}
                for r in c.execute(
                    "SELECT name, last_seen_at FROM devices WHERE revoked=0 AND id != ?"
                    " ORDER BY name", (actor["device"]["id"],))
            ]
        return [dict(r) for r in c.execute(
            "SELECT id, name, created_at, last_seen_at, revoked FROM devices ORDER BY name")]


@app.post("/api/devices/{device_id}/revoke")
async def revoke_device(request: Request, device_id: int):
    _require(request, ("admin",), state_change=True)
    with db.tx() as c:
        c.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,))
    return {"ok": True}


# ── 핸드오프 ──────────────────────────────────────────

@app.post("/api/handoffs")
async def create_handoff(request: Request):
    actor = _require(request, ("device", "admin"), state_change=True)
    body = await request.json()
    to_name = str(body.get("to") or "").strip()
    repo = str(body.get("repo") or "").strip()
    if not to_name or not repo:
        raise HTTPException(400, "to(기기명)와 repo는 필수")
    # 허브 운반 페이로드 — 문서 본문 + 리포별 patch. 상한 초과는 git 경로(wip 브랜치 push)로 유도.
    doc = str(body.get("doc") or "")
    if len(doc.encode()) > 65536:
        raise HTTPException(400, "doc이 64KB 상한 초과 — git 경로(wip 브랜치 push + 문서 축약)로 넘기세요")
    patches = body.get("patches")
    patches_json = None
    if patches:
        if not isinstance(patches, list) or not all(
            isinstance(p, dict) and isinstance(p.get("repo"), str)
            and isinstance(p.get("base"), str) and isinstance(p.get("diff"), str)
            for p in patches
        ):
            raise HTTPException(400, "patches는 [{repo, base, diff}] 배열이어야 함")
        patches_json = json.dumps(patches, ensure_ascii=False)
        if len(patches_json.encode()) > 1_000_000:
            raise HTTPException(400, "patches가 1MB 상한 초과 — git 경로(wip 브랜치 push)로 넘기세요")
    with db.tx() as c:
        to = c.execute("SELECT id FROM devices WHERE name=? AND revoked=0", (to_name,)).fetchone()
        if not to:
            raise HTTPException(404, f"기기 '{to_name}' 없음")
        cur = c.execute(
            "INSERT INTO handoffs (from_device, to_device, repo, origin, branch, doc_path,"
            " summary, doc, patches, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (actor["device"]["id"] if actor["device"] else None, to["id"], repo,
             body.get("origin"), body.get("branch"), body.get("doc_path"),
             str(body.get("summary") or "")[:300], doc or None, patches_json, state.utcnow()),
        )
    return {"id": cur.lastrowid, "hf": f"HF-{cur.lastrowid:03d}"}


# 핸드오프 탭 검색 — 요약·문서 본문·브랜치에서, 단어마다 AND(태스크·자동화 탭과 같은 규칙, 2026-10-28).
# patches(diff)는 코드라 단어마다 잡음이 많아 제외. 문서는 목록에 안 실리므로 요약에 안 보이는 단어는 발췌로 보여 준다.
_SEARCH_HANDOFF_COLS = ("h.summary", "h.doc", "h.branch")


def _handoff_search_conds(terms: list[str]) -> list[str]:
    """단어마다 조건 하나 — 인자는 단어당 _SEARCH_HANDOFF_COLS 개수만큼 _like_pat(단어)."""
    return ["(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in _SEARCH_HANDOFF_COLS) + ")" for _ in terms]


def _handoff_match(c, handoff_id: int, summary: str | None, terms: list[str]) -> dict | None:
    """요약에 안 보이는 첫 단어가 문서에 있으면 그 주변 발췌. 모두 요약에 보이면(또는 브랜치로만 맞았으면) None."""
    shown = (summary or "").lower()
    term = next((t for t in terms if t.lower() not in shown), None)
    if term is None:
        return None
    doc = (c.execute("SELECT doc FROM handoffs WHERE id=?", (handoff_id,)).fetchone() or {"doc": None})["doc"] or ""
    if term.lower() not in doc.lower():
        return None
    return {"field": "doc", "text": _excerpt(doc, term)}


@app.get("/api/handoffs")
async def list_handoffs(request: Request, mine: str = "", repo: str = "", origin: str = "", limit: int = 50,
                        page: int = 0, src: str = "", dst: str = "", status: str = "", days: int = 0, q: str = ""):
    actor = _require(request, ("device", "admin"))
    with db.tx() as c:
        if mine and actor["device"]:
            q = "SELECT h.*, fd.name AS from_name FROM handoffs h" \
                " LEFT JOIN devices fd ON fd.id=h.from_device" \
                " WHERE h.to_device=? AND h.status='pending'"
            args: list = [actor["device"]["id"]]
            # 빈 문자열 repo/origin은 매칭 키가 아니다 — 빈 값끼리 '='로 오배달되는 것 방지.
            # repo·origin이 둘 다 비면 필터를 걸지 않고(호출측이 조회 자체를 생략해야 함) 전체를 준다.
            conds, cargs = [], []
            if repo:
                conds.append("h.repo=?"); cargs.append(repo)
            if origin:
                conds.append("(h.origin IS NOT NULL AND h.origin!='' AND h.origin=?)"); cargs.append(origin)
            if conds:
                q += " AND (" + " OR ".join(conds) + ")"
                args += cargs
            rows = c.execute(q + " ORDER BY h.id", args).fetchall()
        elif page >= 1:
            # 핸드오프 탭 페이지 응답 — doc/patches(수백 KB)는 싣지 않고 펼칠 때 단건 조회
            hf_from = (" FROM handoffs h"
                       " LEFT JOIN devices fd ON fd.id=h.from_device"
                       " LEFT JOIN devices td ON td.id=h.to_device")
            conds, cargs = [], []
            if src == "?":                      # 보낸 기기 미상(장치 기록 없음)
                conds.append("fd.name IS NULL")
            elif src:
                conds.append("fd.name = ?"); cargs.append(src)
            if dst == "?":
                conds.append("td.name IS NULL")
            elif dst:
                conds.append("td.name = ?"); cargs.append(dst)
            if repo:
                conds.append("h.repo = ?"); cargs.append(repo)
            if status:
                conds.append("h.status = ?"); cargs.append(status)
            if days > 0:
                conds.append("h.created_at >= datetime('now', ?)"); cargs.append(f"-{int(days)} days")
            terms = _search_terms(q)
            conds += _handoff_search_conds(terms)
            cargs += [_like_pat(t) for t in terms for _ in _SEARCH_HANDOFF_COLS]
            where = (" WHERE " + " AND ".join(conds)) if conds else ""
            per = limit if 1 <= limit <= 200 else 20
            total = c.execute("SELECT COUNT(*)" + hf_from + where, cargs).fetchone()[0]
            pages = max(1, -(-total // per))
            page = min(max(page, 1), pages)
            rows = [dict(r) for r in c.execute(
                "SELECT h.id, h.repo, h.branch, h.summary, h.status, h.created_at, h.delivered_at,"
                " fd.name AS from_name, td.name AS to_name,"
                " (h.doc IS NOT NULL AND h.doc != '') AS has_doc"
                + hf_from + where + " ORDER BY h.id DESC LIMIT ? OFFSET ?",
                cargs + [per, (page - 1) * per]).fetchall()]
            if terms:
                for r in rows:
                    r["match"] = _handoff_match(c, r["id"], r["summary"], terms)
            facets = {
                "froms": [r[0] for r in c.execute(
                    "SELECT DISTINCT COALESCE(fd.name,'?')" + hf_from + " ORDER BY 1")],
                "tos": [r[0] for r in c.execute(
                    "SELECT DISTINCT COALESCE(td.name,'?')" + hf_from + " ORDER BY 1")],
                "projects": [r[0] for r in c.execute(
                    "SELECT DISTINCT h.repo FROM handoffs h ORDER BY 1")],
            }
            return {"rows": [{**r, "hf": f"HF-{r['id']:03d}"} for r in rows],
                    "total": total, "page": page, "pages": pages, "facets": facets}
        else:
            rows = c.execute(
                "SELECT h.*, fd.name AS from_name, td.name AS to_name FROM handoffs h"
                " LEFT JOIN devices fd ON fd.id=h.from_device"
                " LEFT JOIN devices td ON td.id=h.to_device"
                " ORDER BY h.id DESC LIMIT ?", (limit if limit > 0 else -1,)).fetchall()
        return [
            {**dict(r), "hf": f"HF-{r['id']:03d}"} for r in rows
        ]


@app.get("/api/handoffs/{handoff_id}")
async def get_handoff(request: Request, handoff_id: int):
    """핸드오프 단건(문서 포함) — 핸드오프 탭 '문서 보기' 펼침용.
    기기 토큰은 자기가 받았거나 보낸 건만 조회 가능(admin은 전체) — PATCH와 같은 범위 규칙."""
    actor = _require(request, ("device", "admin"))
    where = "h.id=?"
    args: list = [handoff_id]
    if actor["kind"] == "device":
        where += " AND (h.to_device=? OR h.from_device=?)"
        args += [actor["device"]["id"], actor["device"]["id"]]
    with db.tx() as c:
        r = c.execute(
            "SELECT h.*, fd.name AS from_name, td.name AS to_name FROM handoffs h"
            " LEFT JOIN devices fd ON fd.id=h.from_device"
            " LEFT JOIN devices td ON td.id=h.to_device"
            f" WHERE {where}", args).fetchone()
    if not r:
        raise HTTPException(404, "핸드오프 없음 또는 권한 없음")
    return {**dict(r), "hf": f"HF-{r['id']:03d}"}


@app.patch("/api/handoffs/{handoff_id}")
async def patch_handoff(request: Request, handoff_id: int):
    actor = _require(request, ("device", "admin"), state_change=True)
    body = await request.json()
    status = str(body.get("status") or "")
    if status not in ("delivered", "in_progress", "done", "cancelled"):
        raise HTTPException(400, "status는 delivered|in_progress|done|cancelled")
    with db.tx() as c:
        sets = "status=?, delivered_at=COALESCE(delivered_at,?)" if status == "delivered" else "status=?"
        args = [status, state.utcnow()] if status == "delivered" else [status]
        # 기기 토큰은 자기 앞으로 온 핸드오프만 전이 가능(admin은 전체)
        where = "id=?"
        wargs = [handoff_id]
        if actor["kind"] == "device":
            where += " AND to_device=?"
            wargs.append(actor["device"]["id"])
        cur = c.execute(f"UPDATE handoffs SET {sets} WHERE {where}", args + wargs)
        if cur.rowcount != 1:
            raise HTTPException(404, "핸드오프 없음 또는 권한 없음")
    return {"ok": True}


# ── 설정 (LLM 사용처별 provider/model/effort) ─────────
# 대시보드 설정 탭에서 관리. settings 테이블 값이 .env 기본값을 덮는다.
# provider는 이 프로젝트 전제(에이전트 CLI가 있는 기기)에 맞춰 claude/codex만.

@app.get("/api/settings")
async def get_settings(request: Request):
    _require(request, ("admin",))
    with db.tx() as c:
        stored = {k: v for k, v in llm.settings_all(c).items() if k.startswith("llm.")}   # 내부 보존 키 제외
    return {"stored": stored, "effective": {s: llm.conf(s) for s in llm.SITES},
            "defaults": {s: llm.defaults(s) for s in llm.SITES}, "sites": list(llm.SITES)}


@app.get("/api/llm-models")
async def llm_models(request: Request, provider: str = "claude", refresh: str = ""):
    """설정 탭 모델 드롭다운 — CLI에서 계정별 모델 목록 조회 (1시간 캐시, refresh=1로 갱신)."""
    _require(request, ("admin",))
    if provider not in ("claude", "codex"):
        raise HTTPException(400, "provider는 claude 또는 codex")
    conf = llm.conf("summary")  # bin 경로는 사이트 공통
    bin_path = conf["claude_bin"] if provider == "claude" else conf["codex_bin"]
    return llm_meta.get_models(provider, bin_path, refresh=refresh == "1")


@app.post("/api/settings")
async def save_settings(request: Request):
    _require(request, ("admin",), state_change=True)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "객체 필요")
    allowed = {f"llm.{s}.{f}" for s in llm.SITES for f in llm.FIELDS}
    allowed |= {"llm.claude_bin", "llm.codex_bin"}
    with db.tx() as c:
        for k, v in body.items():
            if k not in allowed:
                raise HTTPException(400, f"알 수 없는 설정 키: {k}")
            v = str(v or "").strip()
            if k.endswith(".provider") and v and v not in ("claude", "codex"):
                raise HTTPException(400, "provider는 claude 또는 codex")
            if v:
                c.execute("INSERT INTO settings (key, value) VALUES (?,?)"
                          " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
            else:  # 빈 값 = 기본값으로 복귀
                c.execute("DELETE FROM settings WHERE key=?", (k,))
    return {"ok": True}


@app.get("/api/llm-runs")
async def llm_runs(request: Request, limit: int = 50, site: str = ""):
    """최근 LLM 호출 기록 — 실패 원인 확인용."""
    _require(request, ("admin",))
    q = "SELECT * FROM llm_runs"
    args: list = []
    if site:
        q += " WHERE site=?"; args.append(site)
    q += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(limit, 500)))
    with db.tx() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


# ── 정적 서빙 ─────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "madison"}


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(ASSETS_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/assets/{name:path}")
async def assets(name: str):
    """대시보드 정적 자산 — 하위 폴더(assets/lucide/ 아이콘 원본)까지. 경로 이탈은 parents 검사로 차단."""
    path = (ASSETS_DIR / name).resolve()
    if not path.is_file() or ASSETS_DIR.resolve() not in path.parents:
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/install.sh")
async def install_sh():
    return FileResponse(COLLECTOR_DIR / "install.sh", media_type="text/x-shellscript")


@app.get("/install.ps1")
async def install_ps1():
    return FileResponse(COLLECTOR_DIR / "install.ps1", media_type="text/plain")


@app.get("/collector/{path:path}")
async def collector_file(path: str):
    if path not in COLLECTOR_FILES:
        raise HTTPException(404)
    return FileResponse(COLLECTOR_DIR / path, media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    actor = _actor(request)
    if actor["kind"] not in ("local", "admin"):
        if not (CFG.cf_team_domain and CFG.cf_aud):
            return HTMLResponse(
                "<h3>MADISON</h3><p>대시보드 인증이 아직 구성되지 않았습니다 — "
                "Cloudflare Access 앱을 만들고 .env의 CF_ACCESS_TEAM_DOMAIN/CF_ACCESS_AUD를 "
                "채운 뒤 허브를 재시작하세요. (허브 기기에서는 http://127.0.0.1:8787 로 접근 가능)</p>",
                status_code=403)
        return HTMLResponse("<h3>MADISON</h3><p>인증 실패 — Access 로그인 필요</p>", status_code=403)
    resp = FileResponse(DASHBOARD_HTML, media_type="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── 업무 리포트 + 사용 메트릭 ──────────────────────────

@app.get("/api/report")
async def get_report(request: Request, range: str = "day", date: str = ""):
    _require(request, ("admin",))
    range_ = reporting.norm_range(range)
    day = reporting.norm_day(range_, date or reporting.today_local())
    with db.tx() as c:
        row = reporting.get_row(c, range_, day)
        assignments = [dict(r) for r in c.execute(
            "SELECT session_key, device, project, service, task, evidence FROM report_assignments"
            " WHERE range=? AND day=? ORDER BY session_key", (range_, day))] if range_ == "day" else []
        proposed = [r["name"] for r in c.execute("SELECT name FROM services WHERE status='proposed' ORDER BY name")]
        versions = c.execute("SELECT COUNT(*) n FROM report_versions WHERE range=? AND day=?",
                             (range_, day)).fetchone()["n"]
    out = {"range": range_, "day": day, "generating": reporting.is_generating((range_, day)),
           "markdown": None, "generated_at": None, "model": None,
           "failed_at": None, "fail_reason": None, "assignments": assignments,
           "proposed": proposed, "versions": versions}
    if row:
        out.update({"markdown": row["markdown"], "generated_at": row["generated_at"],
                    "model": row["model"],
                    "failed_at": row["failed_at"], "fail_reason": row["fail_reason"]})
    return out


@app.get("/api/report/versions")
async def report_versions(request: Request, range: str = "day", date: str = ""):
    _require(request, ("admin",))
    range_ = reporting.norm_range(range)
    day = reporting.norm_day(range_, date or reporting.today_local())
    with db.tx() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, generated_at, model, prompt_version, length(markdown) AS chars FROM report_versions"
            " WHERE range=? AND day=? ORDER BY id DESC", (range_, day))]


@app.post("/api/report/restore")
async def report_restore(request: Request):
    """보관된 생성본으로 되돌린다(현재 본문도 보관에 남는다). 이후 재료가 바뀌면 자동 재생성이 덮을 수 있다."""
    _require(request, ("admin",), state_change=True)
    body = await request.json()
    vid = int(body.get("id") or 0)
    with db.tx() as c:
        v = c.execute("SELECT * FROM report_versions WHERE id=?", (vid,)).fetchone()
        if not v:
            raise HTTPException(404, "생성본 없음")
        c.execute("UPDATE reports SET markdown=?, model=?, prompt_version=?, failed_at=NULL, fail_reason=NULL"
                  " WHERE range=? AND day=?", (v["markdown"], v["model"], v["prompt_version"], v["range"], v["day"]))
    return {"ok": True, "range": v["range"], "day": v["day"]}


@app.post("/api/report/refresh")
async def refresh_report(request: Request, range: str = "day", date: str = ""):
    _require(request, ("admin",), state_change=True)   # 관리자 전용 (LLM 비용 유발)
    range_ = reporting.norm_range(range)
    day = reporting.norm_day(range_, date or reporting.today_local())
    if reporting.is_generating((range_, day)):
        return {"status": "generating"}
    return {"status": "started" if reporting.spawn(range_, day) else "busy"}


@app.post("/api/report/relabel")
async def relabel_report(request: Request):
    """세션 배치를 사람이 바로잡는다 — 교정 기록으로 남아 다음 생성의 사례가 되고, 그 날은 재생성 대기가 된다."""
    _require(request, ("admin",), state_change=True)
    body = await request.json()
    day = str(body.get("date") or "")
    try:
        return reporting.relabel("day", day, str(body.get("session_key") or ""),
                                 str(body.get("service") or "").strip(), str(body.get("reason") or ""))
    except KeyError:
        raise HTTPException(404, "배치 기록 없음")
    except ValueError:
        raise HTTPException(400, "서비스 목록에 없는 이름")


@app.get("/api/metrics")
async def get_metrics(request: Request, range: str = "day", date: str = ""):
    _require(request, ("admin",))
    with db.tx() as c:
        r2 = reporting.norm_range(range)
        return report.metrics(c, r2, reporting.norm_day(r2, date or reporting.today_local()), registry.snapshot(c))


@app.get("/api/usage/history")
async def get_usage_history(request: Request, days: int = 30):
    """구독 한도 % 변화 이력 — 변화 시점만 저장, 계단선으로 그린다. 조기 리셋·플랜 변경도 표시."""
    _require(request, ("admin",))
    with db.tx() as c:
        h = usage.history(c, days)
    snap = usage.snapshot()   # 패널 제목용 플랜명 (현황 타일과 동일 표기)
    for p in usage.PROVIDERS:
        h[p]["plan"] = (snap.get(p) or {}).get("plan") or ""
    return h


@app.get("/api/usage/tokens")
async def get_usage_tokens(request: Request, days: int = 30, agent: str = "", device: str = "",
                           service: str = "", model: str = "", human: int = 0):
    """토큰 사용량 집계 — 일별 시계열 + 기기/서비스/프로젝트/모델/세션별 내역."""
    _require(request, ("admin",))
    with db.tx() as c:
        return tokens.summary(c, registry.snapshot(c), days=days, agent=agent, device=device,
                              service=service, model=model, human=bool(human))


# ── 서비스 레지스트리 (리포트 최상위 이름 공간) ─────────

@app.get("/api/services")
async def list_services(request: Request):
    _require(request, ("admin",))
    with db.tx() as c:
        registry.seed_from_env(c)
        return {"services": registry.all_services(c), "project_map": registry.project_map(c),
                "projects": report.active_projects(c), "kinds": list(registry.KINDS)}


@app.post("/api/services")
async def create_service(request: Request):
    _require(request, ("admin",), state_change=True)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "이름은 1~60자")
    with db.tx() as c:
        sid = registry.upsert_service(c, name, kind=str(body.get("kind") or "product"),
                                      description=str(body.get("description") or "")[:200],
                                      cues=[str(x).strip() for x in (body.get("cues") or []) if str(x).strip()][:12])
    return {"ok": True, "id": sid}


@app.patch("/api/services/{sid}")
async def update_service(request: Request, sid: int):
    _require(request, ("admin",), state_change=True)
    body = await request.json()
    with db.tx() as c:
        row = c.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "서비스 없음")
        name = str(body.get("name") or row["name"]).strip()
        if not name or len(name) > 60:
            raise HTTPException(400, "이름은 1~60자")
        dup = c.execute("SELECT id FROM services WHERE name=? AND id!=?", (name, sid)).fetchone()
        if dup:
            raise HTTPException(409, "같은 이름의 서비스가 있음 — 병합을 쓰세요")
        kind = str(body.get("kind") or row["kind"])
        if kind not in registry.KINDS:
            raise HTTPException(400, "kind")
        cues = body.get("cues")
        cues_json = json.dumps([str(x).strip() for x in cues if str(x).strip()][:12], ensure_ascii=False) \
            if isinstance(cues, list) else row["cues"]
        c.execute("UPDATE services SET name=?, kind=?, description=?, cues=? WHERE id=?",
                  (name, kind, str(body.get("description") if body.get("description") is not None
                                   else row["description"] or "")[:200], cues_json, sid))
        renamed = registry.rename_in_reports(c, row["name"], name) if name != row["name"] else 0
    return {"ok": True, "renamed_reports": renamed}


@app.post("/api/services/{sid}/decide")
async def decide_service(request: Request, sid: int):
    """제안 확정/거절/병합 — 사람만 한다."""
    _require(request, ("admin",), state_change=True)
    body = await request.json()
    status = str(body.get("status") or "")
    merged_into = body.get("merged_into")
    with db.tx() as c:
        if not c.execute("SELECT id FROM services WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "서비스 없음")
        try:
            registry.decide(c, sid, status, int(merged_into) if merged_into else None)
        except ValueError as e:
            raise HTTPException(400, f"잘못된 요청: {e}")
    return {"ok": True}


@app.put("/api/project-map/{project}")
async def put_project_map(request: Request, project: str):
    _require(request, ("admin",), state_change=True)
    body = await request.json()
    with db.tx() as c:
        sid = body.get("service_id")
        if not sid and body.get("service"):
            r = c.execute("SELECT id FROM services WHERE name=?", (str(body["service"]),)).fetchone()
            sid = r["id"] if r else None
        if not sid:
            raise HTTPException(400, "service_id 또는 service(이름) 필요")
        try:
            registry.set_project(c, project, int(sid), str(body.get("strength") or "strong"))
        except ValueError:
            raise HTTPException(400, "strength는 strong|weak")
    return {"ok": True}


@app.delete("/api/project-map/{project}")
async def delete_project_map(request: Request, project: str):
    _require(request, ("admin",), state_change=True)
    with db.tx() as c:
        registry.unset_project(c, project)
    return {"ok": True}


@app.get("/api/services/export")
async def export_services(request: Request):
    _require(request, ("admin",))
    with db.tx() as c:
        return PlainTextResponse(registry.export_env(c))


# ── 보존 정리 스레드 ──────────────────────────────────

def _retention_loop():
    """EVENT_RETENTION_DAYS > 0일 때만 기동 — 0이면 이벤트를 무기한 보존."""
    import time
    while True:
        time.sleep(6 * 3600)
        try:
            with db.tx() as c:
                c.execute(
                    "DELETE FROM events WHERE ts_hub < datetime('now', ?)",
                    (f"-{CFG.retention_days} days",))
        except Exception:
            log.exception("보존 정리 오류")
