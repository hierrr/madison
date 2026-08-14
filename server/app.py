"""MADISON 허브 — 단일 FastAPI 앱이 API와 대시보드를 함께 서빙한다."""
import hashlib
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from . import auth, db, state
from .config import CFG, REPO_ROOT

app = FastAPI(title="MADISON", docs_url=None, redoc_url=None, openapi_url=None)

DASHBOARD_HTML = REPO_ROOT / "dashboard" / "index.html"
ASSETS_DIR = REPO_ROOT / "dashboard" / "assets"
COLLECTOR_DIR = REPO_ROOT / "collector"

# 기계용 호스트(madison-api.*)가 응답하는 경로 (§8.1: /api/*와 설치 파일만)
API_HOST_ALLOWED_PREFIXES = ("/api/", "/collector/")
API_HOST_ALLOWED_EXACT = {"/install.sh", "/install.ps1", "/healthz", "/favicon.svg"}

COLLECTOR_FILES = {
    "report.sh", "flush.sh", "install.sh", "install.ps1", "report.ps1",
    "hooks.template.json", "codex-notify-wrapper.sh", "codex-watch.sh",
    "skills/handoff/SKILL.md", "skills/pickup/SKILL.md",
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
        print(f"[auth] 401 {request.method} {request.url.path} kind={actor['kind']} ip={ip}",
              flush=True)
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
async def get_feed(request: Request, limit: int = 50):
    _require(request, ("device", "admin"))
    with db.tx() as c:
        return state.feed(c, limit)


@app.get("/api/history/events")
async def history_events(request: Request, device: str = "", agent: str = "",
                         session_id: str = "", limit: int = 300):
    """특정 세션의 이벤트 원장 — 자동화 탭의 펼침 보기용."""
    import json as _json
    _require(request, ("device", "admin"))
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
            p = _json.loads(r["payload"] or "{}")
        except _json.JSONDecodeError:
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


@app.get("/api/history/sessions")
async def history_sessions(request: Request, limit: int = 2000, days: int = 0):
    """종료 포함 전체 세션 이력 — 태스크 탭용. days=0이면 전체 기간."""
    _require(request, ("device", "admin"))
    q = ("SELECT s.rowid AS row_id, s.*, d.name AS device"
         " FROM sessions s JOIN devices d ON d.id=s.device_id")
    args: list = []
    if days > 0:
        q += " WHERE s.last_seen_hub >= datetime('now', ?)"
        args.append(f"-{int(days)} days")
    q += " ORDER BY s.last_seen_hub DESC LIMIT ?"
    args.append(min(limit, 5000))
    with db.tx() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


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
        now = state.utcnow()
        cur = c.execute(
            "UPDATE sessions SET state='ended', ended_at=?, end_reason='manual', state_since=?"
            " WHERE device_id=? AND agent=? AND session_id=?",
            (now, now, dev["id"], str(body.get("agent") or ""), str(body.get("session_id") or "")),
        )
        if cur.rowcount != 1:
            raise HTTPException(404, "세션 없음")
    return {"ok": True}


@app.get("/api/devices")
async def list_devices(request: Request):
    _require(request, ("admin",))
    with db.tx() as c:
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
    with db.tx() as c:
        to = c.execute("SELECT id FROM devices WHERE name=? AND revoked=0", (to_name,)).fetchone()
        if not to:
            raise HTTPException(404, f"기기 '{to_name}' 없음")
        cur = c.execute(
            "INSERT INTO handoffs (from_device, to_device, repo, origin, branch, doc_path,"
            " summary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (actor["device"]["id"] if actor["device"] else None, to["id"], repo,
             body.get("origin"), body.get("branch"), body.get("doc_path"),
             str(body.get("summary") or "")[:300], state.utcnow()),
        )
    return {"id": cur.lastrowid, "hf": f"HF-{cur.lastrowid:03d}"}


@app.get("/api/handoffs")
async def list_handoffs(request: Request, mine: str = "", repo: str = "", origin: str = "", limit: int = 50):
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
        else:
            rows = c.execute(
                "SELECT h.*, fd.name AS from_name, td.name AS to_name FROM handoffs h"
                " LEFT JOIN devices fd ON fd.id=h.from_device"
                " LEFT JOIN devices td ON td.id=h.to_device"
                " ORDER BY h.id DESC LIMIT ?", (min(limit, 500),)).fetchall()
        return [
            {**dict(r), "hf": f"HF-{r['id']:03d}"} for r in rows
        ]


@app.patch("/api/handoffs/{handoff_id}")
async def patch_handoff(request: Request, handoff_id: int):
    actor = _require(request, ("device", "admin"), state_change=True)
    body = await request.json()
    status = str(body.get("status") or "")
    if status not in ("delivered", "done", "cancelled"):
        raise HTTPException(400, "status는 delivered|done|cancelled")
    with db.tx() as c:
        sets = "status=?, delivered_at=?" if status == "delivered" else "status=?"
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



# ── 정적 서빙 ─────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "madison"}


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(ASSETS_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/assets/{name}")
async def assets(name: str):
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


# ── 태스크 한 줄 요약 워커 ────────────────────────────
# 훅이 아니라 허브가 중앙에서 요약한다: 프롬프트당 haiku 1회, 세션·기기 지연 0.
# 요약 실행 자체가 대시보드에 잡히지 않도록 MADISON_SUPPRESS로 차단
# (훅은 claude의 자식 프로세스라 env를 상속 → report.sh가 즉시 exit 0.
#  --bare는 로그인 컨텍스트까지 건너뛰어 사용 불가 — 2026-08-12 실측).

SUMMARIZER_DIR = REPO_ROOT / "data" / "summarizer"


def _summarize_cached(prompt: str) -> str:
    """동일 프롬프트(자동화 템플릿 등)는 캐시 재사용 — haiku 호출 최소화 + 즉시 요약."""
    phash = hashlib.sha256(prompt.encode()).hexdigest()
    with db.tx() as c:
        row = c.execute("SELECT summary FROM summary_cache WHERE phash=?", (phash,)).fetchone()
    if row:
        return row["summary"]
    summary = _summarize_one(prompt)
    if summary:
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO summary_cache (phash, summary, created_at) VALUES (?,?,?)",
                      (phash, summary, state.utcnow()))
    return summary


def _summarize_one(prompt: str) -> str:
    try:
        SUMMARIZER_DIR.mkdir(parents=True, exist_ok=True)
        # 전용 cwd(비-git → 새어도 project='summarizer'로 자명) + 독립 프로세스 그룹
        # (허브 kickstart 재시작이 진행 중인 요약 프로세스를 죽여 잔해를 남기지 않도록)
        out = subprocess.run(
            [CFG.task_summary_bin, "-p", "--model", CFG.task_summary_model,
             "아래는 AI 코딩 에이전트에게 준 지시문이다(길면 중간에 잘려 있을 수 있다)."
             " 무슨 작업인지 한국어 한 문장(50자 이내)으로 요약하라."
             " 잘림·불완전함에 대한 언급, 인사, 부연 설명 전부 금지 — 오직 요약 한 문장만 출력:"
             f"\n\n{prompt}"],
            capture_output=True, text=True, timeout=90,
            cwd=str(SUMMARIZER_DIR), start_new_session=True,
            env={**os.environ, "MADISON_SUPPRESS": "1"},
        )
        if out.returncode != 0:
            return ""
        return " ".join((out.stdout or "").split())[:90]
    except Exception:
        return ""


def _summary_loop():
    while True:
        time.sleep(12)
        try:
            with db.tx() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT device_id, agent, session_id, last_prompt, last_summary FROM sessions"
                    " WHERE (state != 'ended' OR ended_at >= datetime('now','-1 day'))"
                    "   AND task_summary IS NULL"
                    "   AND (COALESCE(last_prompt,'') != '' OR COALESCE(last_summary,'') != '')"
                    " ORDER BY CASE WHEN state != 'ended' THEN 0 ELSE 1 END, last_seen_hub DESC"
                    " LIMIT 6")]

            def _input_of(r):
                # 지시가 없으면(코덱스 앱 등) 마지막 응답으로부터 작업을 추정 요약
                if r["last_prompt"]:
                    return r["last_prompt"]
                return ("다음은 AI 에이전트의 마지막 응답이다. 어떤 작업/대화였는지 한 문장으로"
                        f" 추정 요약하라: {r['last_summary']}")

            # 활성 세션 우선 + 3-병렬 (haiku CLI 호출이 건당 수십 초라 직렬로는 백로그가 밀림)
            if rows:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    results = list(ex.map(
                        lambda r: (r, _summarize_cached(_input_of(r))), rows))
                for r, summary in results:
                    fallback = (r["last_prompt"] or r["last_summary"] or "")[:90]
                    with db.tx() as c:
                        # 실패 시 원문 앞부분으로 채워 무한 재시도 방지. 그 사이 프롬프트가 바뀌었으면 skip.
                        c.execute(
                            "UPDATE sessions SET task_summary=? WHERE device_id=? AND agent=?"
                            " AND session_id=? AND COALESCE(last_prompt,'')=?",
                            (summary or fallback, r["device_id"], r["agent"],
                             r["session_id"], r["last_prompt"] or ""))
        except Exception:
            pass


# ── 보존 정리 스레드 ──────────────────────────────────

def _retention_loop():
    while True:
        time.sleep(6 * 3600)
        try:
            with db.tx() as c:
                c.execute(
                    "DELETE FROM events WHERE ts_hub < datetime('now', ?)",
                    (f"-{CFG.retention_days} days",))
        except Exception:
            pass


@app.on_event("startup")
async def startup():
    db.conn()
    threading.Thread(target=_retention_loop, daemon=True).start()
    if CFG.task_summary_enabled:
        threading.Thread(target=_summary_loop, daemon=True).start()
