"""업무 리포트 생성·저장·스케줄.

일일: events(prompt/turn_done)를 프로젝트→세션→턴으로 모아 허브 LLM으로 업무일지 마크다운 생성
      — **구조화 출력 1회**({markdown, assignments, proposals}) → 검증기 → 위반 시 수정 호출 1회.
주간·월간: 저장된 일일 업무일지를 재료로 종합(빠졌거나 오래된 최근 일일은 그 자리에서 생성).

원칙:
- LLM이 실패하면 **기존 리포트를 덮지 않는다** — 실패 사유만 기록하고 다음 주기에 재시도.
  폴백(원재료 나열)은 저장본이 아예 없을 때만 보여 준다.
- **새 이벤트가 없으면 다시 만들지 않는다** — 같은 사실이 매번 다른 문장으로 바뀌어 노션에 붙인 것과
  어긋나는 것을 막고, 재시작마다 opus 호출 10분이 드는 것을 없앤다.
- 고정(pinned)된 리포트는 자동 재생성에서 제외한다(수동 갱신은 가능). 생성본은 report_versions에 남긴다.
- 최상위 이름 공간은 registry(DB)가 정본 — 모델의 새 서비스 제안은 접수만 하고 사람이 확정한다.
"""
import datetime
import json
import logging
import os
import subprocess
import sys
import threading
import time

from . import cron, db, llm, registry, report, state
from .config import CFG, REPO_ROOT

log = logging.getLogger("madison.reporting")

_gen_lock = threading.RLock()         # LLM 생성 직렬화 (동시 2건 방지) — 주간·월간이 안에서 일일을 만들므로 재진입
_gen_active: set = set()              # 생성 중인 (range, day) — 상태 표시용
_gen_active_lock = threading.Lock()

VERSIONS_KEEP = 5                     # (range, day)당 보관할 생성본 수
PROPOSAL_MAX_PER_DAY = 3              # 하루 생성에서 접수하는 제안 상한 — 남발 방지


def today_local() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def norm_range(r: str) -> str:
    return r if r in ("week", "month") else "day"


def norm_day(range_: str, day: str) -> str:
    """주간은 달력 주(월~일), 월간은 달력 월 고정 — 어떤 날짜로 조회해도 기간 시작일 키로 정규화."""
    if range_ == "day":
        return day
    try:
        d = datetime.date.fromisoformat(day)
    except ValueError:
        return day
    if range_ == "month":
        return d.replace(day=1).isoformat()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


class active:
    """생성 중 표시(프로세스 내부) — 워커 안에서 주간·월간이 일일을 만들 때의 표시용."""
    def __init__(self, key):
        self.key = key

    def __enter__(self):
        with _gen_active_lock:
            _gen_active.add(self.key)

    def __exit__(self, *exc):
        with _gen_active_lock:
            _gen_active.discard(self.key)


JOB_MAX_AGE_SEC = 3600            # 이보다 오래된 작업 표시는 죽은 워커의 잔해로 본다


def _pid_alive(pid) -> bool:
    """pid가 살아 있고 **실제 생성 워커**인가 — 재부팅 뒤 같은 pid를 다른 프로세스가 쓰는 경우를 걸러낸다."""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True                                   # ps를 못 쓰면 보수적으로 살아 있다고 본다
    return "server.genworker" in out


def _job_alive(row) -> bool:
    if not row or not _pid_alive(row["pid"]):
        return False
    try:
        started = datetime.datetime.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() < JOB_MAX_AGE_SEC
    except (TypeError, ValueError):
        return False


def is_generating(key) -> bool:
    """워커 프로세스가 살아 있는 작업이 있는가 — 허브가 재시작돼도 DB의 작업 표시로 안다."""
    with _gen_active_lock:
        if key in _gen_active:
            return True
    with db.tx() as c:
        row = c.execute("SELECT pid, started_at FROM report_jobs WHERE range=? AND day=?", key).fetchone()
        if row and not _job_alive(row):
            c.execute("DELETE FROM report_jobs WHERE range=? AND day=?", key)
            return False
        return bool(row)


def any_generating() -> bool:
    with db.tx() as c:
        rows = c.execute("SELECT range, day, pid, started_at FROM report_jobs").fetchall()
        alive = False
        for r in rows:
            if _job_alive(r):
                alive = True
            else:
                c.execute("DELETE FROM report_jobs WHERE range=? AND day=?", (r["range"], r["day"]))
        return alive


def spawn(range_: str, day: str) -> bool:
    """생성 워커를 별도 프로세스로 띄운다(한 번에 하나 — LLM 호출 직렬화). 이미 돌고 있으면 False."""
    with db.tx() as c:
        if any_generating():
            return False
        c.execute("INSERT OR REPLACE INTO report_jobs (range, day, pid, started_at) VALUES (?,?,?,?)",
                  (range_, day, 0, state.utcnow()))
    try:
        proc = subprocess.Popen([sys.executable, "-m", "server.genworker", range_, day],
                                cwd=str(REPO_ROOT), start_new_session=True)
    except OSError:
        log.exception("report worker 시작 실패")
        with db.tx() as c:
            c.execute("DELETE FROM report_jobs WHERE range=? AND day=?", (range_, day))
        return False
    with db.tx() as c:
        c.execute("UPDATE report_jobs SET pid=? WHERE range=? AND day=?", (proc.pid, range_, day))
    log.info("report worker 시작: %s %s (pid %d)", range_, day, proc.pid)
    return True


# ── 저장 ──────────────────────────────────────────────

def get_row(c, range_: str, day: str):
    return c.execute("SELECT * FROM reports WHERE range=? AND day=?", (range_, day)).fetchone()


def recent_corrections(c, projects, days=30) -> list:
    """오늘 작업한 프로젝트들에 대한 최근 사람의 재라벨 기록 — 프롬프트에 사례로 넣는다."""
    projects = [p for p in projects if p]
    if not projects:
        return []
    q = (f"SELECT day, project, before_service, after_service, reason FROM corrections"
         f" WHERE created_at >= datetime('now', ?) AND project IN ({','.join('?' * len(projects))})"
         f" ORDER BY id DESC LIMIT 20")
    return [dict(r) for r in c.execute(q, (f"-{days} days", *projects))]


def relabel(range_: str, day: str, session_key: str, service: str, reason: str = "") -> dict:
    """사람의 재라벨 — 배치를 고치고 교정 기록을 남기며 그 날을 재생성 대기로 표시한다.
    본문은 다음 생성에서 바뀐다(즉시 문장을 고치지 않는다 — 문장은 모델이, 배치 결정은 사람이)."""
    with db.tx() as c:
        a = c.execute("SELECT * FROM report_assignments WHERE range=? AND day=? AND session_key=?",
                      (range_, day, session_key)).fetchone()
        if not a:
            raise KeyError("assignment")
        if not c.execute("SELECT 1 FROM services WHERE name=? AND status IN ('confirmed','proposed')", (service,)).fetchone():
            raise ValueError("service")
        now = state.utcnow()
        c.execute("INSERT INTO corrections (day, session_key, device, project, before_service, after_service, reason, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (day, session_key, a["device"], a["project"], a["service"], service, reason[:200], now))
        c.execute("UPDATE report_assignments SET service=?, evidence=? WHERE range=? AND day=? AND session_key=?",
                  (service, f"사람이 옮김: {reason}"[:300] if reason else "사람이 옮김", range_, day, session_key))
        c.execute("UPDATE reports SET stale_at=?, pinned=0 WHERE range=? AND day=?", (now, range_, day))
    return {"day": day, "session_key": session_key, "service": service}


def store(range_: str, day: str, md: str, *, started_at: str, res: llm.Result | None) -> str:
    """성공 저장 + 생성본 보관. generated_at은 **재료를 모은 시각**(started_at) — 생성 중 도착한 이벤트가
    '이미 반영됨'으로 오인되지 않게."""
    model = res.model if res else ""
    with db.tx() as c:
        c.execute(
            "INSERT INTO reports (range, day, markdown, generated_at, model, effort, prompt_version, failed_at, fail_reason)"
            " VALUES (?,?,?,?,?,?,?,NULL,NULL)"
            " ON CONFLICT(range, day) DO UPDATE SET markdown=excluded.markdown, generated_at=excluded.generated_at,"
            " model=excluded.model, effort=excluded.effort, prompt_version=excluded.prompt_version,"
            " failed_at=NULL, fail_reason=NULL, stale_at=NULL",
            (range_, day, md, started_at, model, res.effort if res else "", report.PROMPT_VERSION))
        if md != report.EMPTY_MD:
            c.execute("INSERT INTO report_versions (range, day, generated_at, markdown, model, prompt_version)"
                      " VALUES (?,?,?,?,?,?)", (range_, day, started_at, md, model, report.PROMPT_VERSION))
            c.execute("DELETE FROM report_versions WHERE range=? AND day=? AND id NOT IN"
                      " (SELECT id FROM report_versions WHERE range=? AND day=? ORDER BY id DESC LIMIT ?)",
                      (range_, day, range_, day, VERSIONS_KEEP))
    return started_at


def mark_failed(range_: str, day: str, reason: str, fallback: str | None) -> None:
    """실패 기록. 저장본이 없을 때만 폴백을 markdown에 넣는다(있으면 기존 본문 유지)."""
    now = state.utcnow()
    with db.tx() as c:
        row = get_row(c, range_, day)
        if row is None:
            c.execute("INSERT INTO reports (range, day, markdown, generated_at, failed_at, fail_reason)"
                      " VALUES (?,?,?,?,?,?)", (range_, day, fallback, None, now, reason[:300]))
        elif not row["markdown"] and fallback:
            c.execute("UPDATE reports SET markdown=?, failed_at=?, fail_reason=? WHERE range=? AND day=?",
                      (fallback, now, reason[:300], range_, day))
        else:
            c.execute("UPDATE reports SET failed_at=?, fail_reason=? WHERE range=? AND day=?",
                      (now, reason[:300], range_, day))


def store_assignments(c, range_, day, assignments, work):
    """모델의 세션별 배치를 저장 — 리포트 탭의 '왜 여기' 표시와 다음 단계(교정 기록)의 재료."""
    keymap = {}
    for p, v in work.items():
        for s in v["sessions"]:
            keymap[s["key"]] = (s["device"], s["session_id"], p)
    c.execute("DELETE FROM report_assignments WHERE range=? AND day=?", (range_, day))
    now = state.utcnow()
    for a in assignments or []:
        key = str(a.get("session") or "").strip()
        if key not in keymap:
            continue
        device, sid, project = keymap[key]
        c.execute("INSERT OR REPLACE INTO report_assignments"
                  " (range, day, session_key, device, session_id, project, service, task, evidence, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (range_, day, key, device, sid, project, str(a.get("service") or "")[:80],
                   str(a.get("task") or "")[:120], str(a.get("evidence") or "")[:300], now))


def accept_proposals(c, proposals, day, reg) -> list:
    """모델이 새로 이름 지은 서비스를 레지스트리에 **바로 등록**한다(사람 확정 단계 없음 — 2026-08-28 사용자 결정:
    이름 판단은 에이전트가 하고, 고칠 일이 있으면 허브에서 에이전트에게 시킨다). 가드만 코드가 건다:
    저장소·디렉터리 이름은 서비스가 아니므로 거부(그건 project_map의 일), 제외 이름·기존 이름·이미 거절된 이름 거부,
    하루 상한. 등록된 이름 목록을 돌려준다."""
    accepted = []
    known = set(reg.names()) | set(reg.proposed_names())
    projects = {p.lower() for p in report.active_projects(c, days=365, min_events=1)} | {p.lower() for p in reg.project_map}
    for p in (proposals or [])[:PROPOSAL_MAX_PER_DAY]:
        name = str(p.get("name") or "").strip()
        if not name or len(name) > 60 or name in known or name in CFG.report_exclude_projects or name.lower() in projects:
            if name:
                log.info("report day %s 새 서비스 무시(저장소명/기존 이름): %s", day, name)
            continue
        prev = c.execute("SELECT status FROM services WHERE name=?", (name,)).fetchone()
        if prev and prev["status"] in ("rejected", "merged"):
            continue                                                  # 사람의 결정이 우선
        registry.upsert_service(c, name, kind=str(p.get("kind") or "product"),
                                description=str(p.get("description") or "")[:200],
                                cues=[str(x)[:60] for x in (p.get("cues") or [])][:8],
                                status="confirmed", source="model",
                                evidence=[{"day": day, "quote": str(p.get("evidence") or "")[:300]}])
        accepted.append(name)
    return accepted


def learn_mappings(c, assignments, work, reg) -> list:
    """매핑이 없는 프로젝트의 배치가 한 서비스로 모이면 project_map에 **약한** 매핑으로 학습한다.
    다음 생성부터 그 서비스가 기본값이 되고(약함이라 내용이 다르면 여전히 옮김), 사람이 .env나 UI를 만질 일이 없다."""
    by_project: dict = {}
    keymap = {s["key"]: p for p, v in work.items() for s in v["sessions"]}
    for a in assignments or []:
        p = keymap.get(str(a.get("session") or "").strip())
        svc = str(a.get("service") or "").strip()
        if p and svc:
            by_project.setdefault(p, set()).add(svc)
    learned = []
    names = {s["name"]: s["id"] for s in registry.all_services(c, "confirmed")}
    for p, svcs in by_project.items():
        if reg.strength(p) != "none" or len(svcs) != 1:
            continue
        svc = next(iter(svcs))
        if svc == p or svc not in names:
            continue                                                  # 프로젝트명 그대로 쓴 경우는 배우지 않음
        registry.set_project(c, p, names[svc], "weak", source="model")
        learned.append((p, svc))
    return learned


# ── 생성 ──────────────────────────────────────────────

def _digest(day: str):
    def call(prompt):
        r = llm.run("digest", prompt, ref=f"digest:{day}")
        return llm.strip_fences(r.text) if r.ok else ""
    return call


def _prev_daily(c, day: str, back: int = 3):
    """직전 업무일지 (날짜, md) — 최근 back일 안에서 내용 있는 가장 가까운 것. 주말 뒤 월요일도 금요일을 잇는다."""
    d = datetime.date.fromisoformat(day)
    for i in range(1, back + 1):
        pd = (d - datetime.timedelta(days=i)).isoformat()
        row = c.execute("SELECT markdown FROM reports WHERE range='day' AND day=?", (pd,)).fetchone()
        if row and row["markdown"] and row["markdown"] != report.EMPTY_MD:
            return pd, row["markdown"]
    return None


def _validated(md: str, allowed, block_services, ref: str):
    """검증 → 위반이 있으면 같은 모델로 수정 호출 1회 → 남은 위반은 기록(needs_review)."""
    problems = report.validate(md, allowed, block_services)
    if not problems:
        return md, []
    log.info("report %s 검증 위반 %d건 — 수정 호출: %s", ref, len(problems), "; ".join(problems[:4]))
    fix = llm.run("report", report.repair_prompt(md, problems), ref=f"repair:{ref}")
    if fix.ok:
        fixed = report.strip_meta(llm.strip_fences(fix.text))
        left = report.validate(fixed, allowed, block_services)
        if len(left) < len(problems):
            return fixed, left
    return md, problems


def gen_day(day: str):
    """→ (markdown | None, Result | None, fallback, extras). None = LLM 실패(저장하지 말 것)."""
    with db.tx() as c:
        registry.seed_from_env(c)
        reg = registry.snapshot(c)
        work = report.gather(c, "day", day)
        prev = _prev_daily(c, day) if work else None
        corrections = recent_corrections(c, list(work)) if work else []
    extras = {"assignments": [], "proposals": [], "problems": [], "reg": reg, "work": work}
    if not work:
        return report.EMPTY_MD, None, None, extras
    # 예산을 넘는 프로젝트는 긴 세션부터 미리 압축(세션당 LLM 1회) — 잘라내지 않는다
    report.compress(work, _digest(day), reg=reg)
    res = llm.run("report", report.build_day_prompt(day, work, reg, prev, corrections),
                  schema=report.DOC_SCHEMA, ref=f"day:{day}")
    fallback = report.fallback_md(work, reg)
    if not res.ok:
        return None, res, fallback, extras
    data = res.data or {}
    md = report.strip_meta(llm.strip_fences(str(data.get("markdown") or "")))
    if not md:
        res.ok, res.error = False, "빈 리포트"
        return None, res, fallback, extras
    extras["assignments"] = data.get("assignments") or []
    extras["proposals"] = data.get("proposals") or []
    proposed_now = [str(p.get("name") or "").strip() for p in extras["proposals"] if p.get("name")]
    allowed = reg.names() + reg.proposed_names() + proposed_now
    block_services = [reg.service(p) for p in work]
    md, problems = _validated(md, allowed, block_services, f"day:{day}")
    extras["problems"] = problems
    return md, res, fallback, extras


def _age_sec(iso_utc: str) -> float:
    try:
        t = datetime.datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def _yesterday(today: str) -> str:
    return (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()


def _finish_day(day: str, started: str, md, res, fallback, extras) -> str | None:
    """gen_day 결과를 저장(성공) 또는 실패 기록. 저장된 본문을 돌려준다."""
    if md is None:
        mark_failed("day", day, res.error if res else "unknown", fallback)
        return None
    store("day", day, md, started_at=started, res=res)
    with db.tx() as c:
        store_assignments(c, "day", day, extras["assignments"], extras["work"])
        accepted = accept_proposals(c, extras["proposals"], day, extras["reg"])
        learned = learn_mappings(c, extras["assignments"], extras["work"], extras["reg"])
        if extras["problems"]:
            c.execute("UPDATE reports SET fail_reason=? WHERE range='day' AND day=?",
                      ("검토 필요: " + "; ".join(extras["problems"])[:280], day))
    if accepted:
        log.info("report day %s 새 서비스 등록: %s", day, ", ".join(accepted))
    if learned:
        log.info("report day %s 매핑 학습: %s", day, ", ".join(f"{p}→{s}" for p, s in learned))
    return md


def daily_for(day: str, today: str) -> str:
    """주간·월간 재료용 일일 업무일지. 없으면 생성하고, **어제** 것은 마지막 지시·응답보다 오래됐으면 재생성한다
    (일일 자동 갱신은 '오늘'만 돌아서, 마지막 시간별 갱신과 자정 사이의 작업이나 허브가 꺼져 있던 저녁의 작업은
    빠진 채 굳는다). 오늘 것은 루프가 갱신하므로 저장본을 쓰고, 더 오래된 날도 저장본 그대로.
    고정된 날은 건드리지 않는다."""
    with db.tx() as c:
        row = get_row(c, "day", day)
        stale = False
        if row and row["markdown"] and not row["pinned"] and day == _yesterday(today):
            last = report.last_event_at(c, day)
            stale = bool(last and last > (row["generated_at"] or ""))
    if row and row["markdown"] and not stale:
        return row["markdown"] or ""
    with active(("day", day)):
        started = state.utcnow()
        md = _finish_day(day, started, *gen_day(day))
        if md is None:
            return (row["markdown"] if row and row["markdown"] else "") or ""
        return md


def gen_period(range_: str, day: str):
    today = today_local()
    dailies = [(d, md) for d in report.period_days(range_, day, today)
               for md in [daily_for(d, today)] if md and md != report.EMPTY_MD]
    if not dailies:
        return report.EMPTY_MD, None, None
    with db.tx() as c:
        reg = registry.snapshot(c)
    res = llm.run("report", report.build_period_prompt(range_, day, dailies, reg), ref=f"{range_}:{day}")
    fallback = report.fallback_period_md(dailies)
    if not res.ok:
        return None, res, fallback
    md = report.strip_meta(llm.strip_fences(res.text))
    if not md:
        res.ok, res.error = False, "빈 리포트"
        return None, res, fallback
    allowed = reg.names() + reg.proposed_names()
    block_services = [n for _, d_md in dailies for n in report.top_level_names(d_md)]
    md, problems = _validated(md, allowed, block_services, f"{range_}:{day}")
    if problems:
        log.info("report %s %s 검증 잔여 %d건", range_, day, len(problems))
    return md, res, fallback


def generate(range_: str, day: str) -> dict:
    """허브에서 부르는 진입점 — 워커 프로세스를 띄운다. (동기 생성은 generate_inline — 워커가 쓴다.)"""
    return {"range": range_, "day": day, "started": spawn(range_, day)}


def generate_inline(range_: str, day: str) -> dict:
    """기간 리포트 생성·저장(동기). 워커 프로세스 안에서 실행된다. LLM 직렬화(_gen_lock)."""
    key = (range_, day)
    with active(key), _gen_lock:
        started = state.utcnow()
        if range_ == "day":
            md, res, fallback, extras = gen_day(day)
            if md is None:
                reason = res.error if res else "unknown"
                mark_failed("day", day, reason, fallback)
                log.warning("report day %s 실패 — 기존 본문 유지: %s", day, reason)
                return {"range": range_, "day": day, "failed": reason}
            _finish_day(day, started, md, res, fallback, extras)
            gen_at = started
        else:
            md, res, fallback = gen_period(range_, day)
            if md is None:
                reason = res.error if res else "unknown"
                mark_failed(range_, day, reason, fallback)
                log.warning("report %s %s 실패 — 기존 본문 유지: %s", range_, day, reason)
                return {"range": range_, "day": day, "failed": reason}
            gen_at = store(range_, day, md, started_at=started, res=res)
        log.info("report %s %s 생성 (%s)", range_, day, res.model if res else "no-llm")
    return {"range": range_, "day": day, "markdown": md, "generated_at": gen_at}


# ── 재생성 판단 ──────────────────────────────────────

def stale_reason(c, range_: str, day: str) -> str | None:
    """자동 재생성이 필요한 이유 — 없으면 None. 고정(pinned)은 항상 None."""
    row = get_row(c, range_, day)
    if row is None or not row["markdown"]:
        return "no-report"
    if row["pinned"]:
        return None
    gen = row["generated_at"] or ""
    if row["stale_at"] and row["stale_at"] > gen:
        return "relabeled"
    if row["failed_at"] and row["failed_at"] > gen:
        return "retry-failed"
    last = report.last_event_in(c, range_, day)
    if last and last > gen:
        return "new-events"
    if range_ != "day":
        days = report.period_days(range_, day, today_local())
        newest = c.execute(
            "SELECT MAX(generated_at) m FROM reports WHERE range='day' AND day BETWEEN ? AND ?",
            (days[0], days[-1])).fetchone()["m"]
        if newest and newest > gen:
            return "daily-updated"
    return None


def due(range_: str, reason, now: datetime.datetime, last_slot: dict) -> bool:
    """이 분에 생성해야 하는가. no-report는 즉시, 그 외 변경은 크론 시각에만(분당 1회)."""
    if not reason:
        return False
    if reason == "no-report":
        return True
    expr = {"day": CFG.report_daily_cron, "week": CFG.report_weekly_cron, "month": CFG.report_monthly_cron}[range_]
    slot = now.strftime("%Y-%m-%d %H:%M")
    if last_slot.get(range_) == slot or not cron.matches(expr, now):
        return False
    last_slot[range_] = slot
    return True


def loop():
    """일일·주간·월간 자동 생성 — 매분 확인. 크론 시각(REPORT_*_CRON)에 바뀐 것이 있으면 워커를 띄운다.
    허브 재시작은 생성을 유발하지 않고, 어제 일지는 일일 크론 시각에 같이 점검한다(늦게 도착한 작업 반영)."""
    for name, expr in (("REPORT_DAILY_CRON", CFG.report_daily_cron), ("REPORT_WEEKLY_CRON", CFG.report_weekly_cron),
                       ("REPORT_MONTHLY_CRON", CFG.report_monthly_cron)):
        if not cron.valid(expr):
            log.error("%s='%s' 형식 오류 — 자동 갱신 중단(수동 갱신만 가능)", name, expr)
            return
    last_slot: dict = {}
    while True:
        try:
            now = datetime.datetime.now()
            today = now.strftime("%Y-%m-%d")
            for r in ("day", "week", "month"):
                day = norm_day(r, today)
                with db.tx() as c:
                    reason = stale_reason(c, r, day)
                if due(r, reason, now, last_slot) and spawn(r, day):
                    log.info("report loop: %s %s 생성 (%s)", r, day, reason)
                if r == "day" and cron.matches(CFG.report_daily_cron, now):
                    y = _yesterday(today)
                    with db.tx() as c:
                        yreason = stale_reason(c, "day", y)
                    if yreason and yreason != "no-report" and spawn("day", y):
                        log.info("report loop: day %s 생성 (%s)", y, yreason)
        except Exception:
            log.exception("report loop 오류")
        time.sleep(60 - datetime.datetime.now().second)     # 분 경계에 맞춰 깨어난다
