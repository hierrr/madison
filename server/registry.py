"""서비스 레지스트리 — 리포트 최상위 이름 공간.

services(확정·제안·거절·병합) + project_map(프로젝트 → 서비스, 강도). DB가 정본이고 `.env`의
REPORT_SERVICE_MAP / REPORT_KNOWN_SERVICES / REPORT_WEAK_PROJECTS는 **최초 시드**로만 읽는다
(레지스트리가 비어 있을 때 한 번). 이후 설정 탭에서 편집하며 허브 재시작이 필요 없다.

강도(strength): strong = 제품 저장소(내용과 무관하게 그 서비스), weak = 잡동사니 디렉터리(기본값일 뿐,
내용이 다른 서비스면 모델이 근거를 들어 번복). 2026-08-28 사용자 결정: 매핑은 제거하지 않고 약한 기본값으로.
"""
import json

from . import state
from .config import CFG

KINDS = ("product", "tool", "ops", "bucket")
STATUSES = ("proposed", "confirmed", "rejected", "merged")


def _cues(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return [str(x) for x in v] if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


class Registry:
    """생성 1회 동안 쓰는 스냅샷 — 리포트 모듈은 DB가 아니라 이것만 본다(순수 함수 유지)."""

    def __init__(self, services=(), proposed=(), project_map=None, exclude=()):
        self.services = list(services)          # [{id, name, kind, description, cues}]
        self.proposed = list(proposed)          # 제안 중 (같은 필드 + evidence)
        self.project_map = dict(project_map or {})   # project → {"service": name, "strength": strong|weak}
        self.exclude = tuple(exclude)

    @classmethod
    def from_env(cls):
        svcs, seen = [], set()
        for proj, name in CFG.report_service_map.items():
            if name not in seen:
                seen.add(name)
                svcs.append({"id": None, "name": name, "kind": "product", "description": "", "cues": []})
        for name, hint in CFG.report_known_services.items():
            if name not in seen:
                seen.add(name)
                svcs.append({"id": None, "name": name, "kind": "product", "description": hint, "cues": []})
        pm = {p: {"service": n, "strength": "weak" if p in CFG.report_weak_projects else "strong"}
              for p, n in CFG.report_service_map.items()}
        return cls(svcs, (), pm, CFG.report_exclude_projects)

    def service(self, project: str) -> str:
        m = self.project_map.get(project)
        return m["service"] if m else project

    def strength(self, project: str) -> str:
        m = self.project_map.get(project)
        return m["strength"] if m else "none"

    def names(self) -> list:
        return [s["name"] for s in self.services]

    def proposed_names(self) -> list:
        return [s["name"] for s in self.proposed]

    def lookup(self, name: str):
        for s in self.services:
            if s["name"] == name:
                return s
        return None


# ── DB ────────────────────────────────────────────────

def seed_from_env(c) -> int:
    """레지스트리가 비어 있으면 .env 값으로 채운다. 채운 서비스 수를 돌려준다(이미 있으면 0)."""
    if c.execute("SELECT COUNT(*) n FROM services").fetchone()["n"]:
        return 0
    now = state.utcnow()
    ids = {}

    def ensure(name, description=""):
        if name in ids:
            return ids[name]
        cur = c.execute(
            "INSERT INTO services (name, kind, status, description, cues, source, created_at, decided_at)"
            " VALUES (?,?,?,?,?,?,?,?)", (name, "product", "confirmed", description, "[]", "env", now, now))
        ids[name] = cur.lastrowid
        return ids[name]

    for proj, name in CFG.report_service_map.items():
        sid = ensure(name)
        c.execute("INSERT OR REPLACE INTO project_map (project, service_id, strength, source) VALUES (?,?,?,?)",
                  (proj, sid, "weak" if proj in CFG.report_weak_projects else "strong", "env"))
    for name, hint in CFG.report_known_services.items():
        ensure(name, hint)
    return len(ids)


def _row(r) -> dict:
    d = dict(r)
    d["cues"] = _cues(d.get("cues"))
    try:
        d["evidence"] = json.loads(d["evidence"]) if d.get("evidence") else []
    except json.JSONDecodeError:
        d["evidence"] = []
    return d


def all_services(c, status=None) -> list:
    q = "SELECT * FROM services"
    args = ()
    if status:
        q += " WHERE status=?"; args = (status,)
    return [_row(r) for r in c.execute(q + " ORDER BY name", args)]


def project_map(c) -> list:
    return [dict(r) for r in c.execute(
        "SELECT pm.project, pm.service_id, pm.strength, pm.source, s.name AS service"
        " FROM project_map pm JOIN services s ON s.id=pm.service_id ORDER BY pm.project")]


def snapshot(c) -> Registry:
    """확정 서비스 + 제안 + 프로젝트 매핑(병합된 서비스는 병합 대상 이름으로)."""
    rows = {r["id"]: r for r in (_row(x) for x in c.execute("SELECT * FROM services"))}

    def resolve(sid):
        seen = set()
        while sid in rows and rows[sid]["status"] == "merged" and rows[sid]["merged_into"] and sid not in seen:
            seen.add(sid)
            sid = rows[sid]["merged_into"]
        return rows.get(sid)

    confirmed = [r for r in rows.values() if r["status"] == "confirmed"]
    proposed = [r for r in rows.values() if r["status"] == "proposed"]
    pm = {}
    for r in c.execute("SELECT project, service_id, strength FROM project_map"):
        s = resolve(r["service_id"])
        if s and s["status"] == "confirmed":
            pm[r["project"]] = {"service": s["name"], "strength": r["strength"]}
    confirmed.sort(key=lambda s: s["name"])
    return Registry(confirmed, proposed, pm, CFG.report_exclude_projects)


def upsert_service(c, name, *, kind="product", description="", cues=(), status="confirmed",
                   source="human", evidence=None) -> int:
    name = name.strip()
    now = state.utcnow()
    row = c.execute("SELECT id, status FROM services WHERE name=?", (name,)).fetchone()
    if row:
        c.execute("UPDATE services SET kind=?, description=?, cues=? WHERE id=?",
                  (kind, description, json.dumps(list(cues), ensure_ascii=False), row["id"]))
        return row["id"]
    cur = c.execute(
        "INSERT INTO services (name, kind, status, description, cues, evidence, source, created_at, proposed_at, decided_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, kind if kind in KINDS else "product", status, description,
         json.dumps(list(cues), ensure_ascii=False), json.dumps(evidence or [], ensure_ascii=False),
         source, now, now if status == "proposed" else None, now if status == "confirmed" else None))
    return cur.lastrowid


def propose(c, name, *, kind="product", description="", cues=(), evidence=None, day="") -> int | None:
    """모델 제안 접수. 이미 있는 이름(확정·제안·거절·병합)은 새로 만들지 않고 근거만 덧붙인다.
    거절된 이름은 다시 제안되지 않는다(사람의 결정 우선)."""
    name = (name or "").strip()
    if not name or len(name) > 60:
        return None
    row = c.execute("SELECT id, status, evidence FROM services WHERE name=?", (name,)).fetchone()
    ev = list(evidence or [])
    if row:
        if row["status"] == "proposed" and ev:
            try:
                old = json.loads(row["evidence"] or "[]")
            except json.JSONDecodeError:
                old = []
            c.execute("UPDATE services SET evidence=? WHERE id=?",
                      (json.dumps((old + ev)[-12:], ensure_ascii=False), row["id"]))
        return row["id"] if row["status"] == "proposed" else None
    return upsert_service(c, name, kind=kind, description=description, cues=cues, status="proposed",
                          source="model", evidence=ev)


def rename_in_reports(c, old: str, new: str) -> int:
    """서비스 개명을 저장된 리포트에 전파 — 최상위 불릿 줄('- 옛이름')만 결정적으로 치환한다(본문 서술은 그대로).
    생성본 보관·세션 배치·교정 기록도 같이 바꾼다. 바뀐 리포트 수를 돌려준다."""
    if not old or not new or old == new:
        return 0
    n = 0
    for table in ("reports", "report_versions"):
        for r in c.execute(f"SELECT rowid AS rid, markdown FROM {table} WHERE markdown LIKE ?", (f"%- {old}%",)):
            lines = (r["markdown"] or "").splitlines()
            changed = False
            for i, line in enumerate(lines):
                if line.rstrip() == f"- {old}":
                    lines[i] = f"- {new}"
                    changed = True
            if changed:
                c.execute(f"UPDATE {table} SET markdown=? WHERE rowid=?", ("\n".join(lines), r["rid"]))
                if table == "reports":
                    n += 1
    c.execute("UPDATE report_assignments SET service=? WHERE service=?", (new, old))
    c.execute("UPDATE corrections SET after_service=? WHERE after_service=?", (new, old))
    c.execute("UPDATE corrections SET before_service=? WHERE before_service=?", (new, old))
    return n


def decide(c, sid: int, status: str, merged_into: int | None = None) -> None:
    if status not in ("confirmed", "rejected", "merged"):
        raise ValueError("status")
    if status == "merged" and not merged_into:
        raise ValueError("merged_into")
    c.execute("UPDATE services SET status=?, merged_into=?, decided_at=? WHERE id=?",
              (status, merged_into if status == "merged" else None, state.utcnow(), sid))


def set_project(c, project: str, service_id: int, strength: str = "strong", source="human") -> None:
    if strength not in ("strong", "weak"):
        raise ValueError("strength")
    c.execute("INSERT INTO project_map (project, service_id, strength, source) VALUES (?,?,?,?)"
              " ON CONFLICT(project) DO UPDATE SET service_id=excluded.service_id, strength=excluded.strength,"
              " source=excluded.source", (project, service_id, strength, source))


def unset_project(c, project: str) -> None:
    c.execute("DELETE FROM project_map WHERE project=?", (project,))


def export_env(c) -> str:
    """현재 레지스트리를 .env 형식으로 — 백업·다른 허브로 옮길 때."""
    reg = snapshot(c)
    pairs = [f"{p}={m['service']}" for p, m in sorted(reg.project_map.items())]
    weak = [p for p, m in sorted(reg.project_map.items()) if m["strength"] == "weak"]
    mapped = {m["service"] for m in reg.project_map.values()}
    known = [f"{s['name']}={s['description']}" for s in reg.services if s["name"] not in mapped]
    return (f"REPORT_SERVICE_MAP={', '.join(pairs)}\n"
            f"REPORT_WEAK_PROJECTS={','.join(weak)}\n"
            f"REPORT_KNOWN_SERVICES={'; '.join(known)}\n")
