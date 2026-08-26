"""일일/주간/월간 업무 리포트 + 사용 메트릭.

events(prompt/turn_done)를 프로젝트별로 모아 업무일지용 마크다운으로 요약하고,
에이전트 사용 메트릭(세션·턴·활동시간·시간대·잔디)을 집계한다.
LLM 호출은 app에서 주입한다 — 이 모듈은 순수 데이터/문자열만 다룬다.
"""
import json
import re

from .config import CFG

# 실제 작업이 아닌 프롬프트(에이전트 알림·시스템 주입 등)는 리포트에서 제외
NOISE = ("<task-notification", "<system-reminder", "<command", "<local-command")

# 제외 프로젝트 이름의 정규형(하이픈·공백·언더스코어 등 구분자 무시) — 언급 줄 필터용
_EXCL_NORM = tuple(n for n in (re.sub(r"[^0-9a-z가-힣]", "", p.lower())
                               for p in CFG.report_exclude_projects) if n)


def _service(project):
    """프로젝트 → 리포트 최상위 서비스명. REPORT_SERVICE_MAP에 없으면 프로젝트명 그대로.
    최상위 묶음을 설정으로 고정해, 이름이 비슷하다는 이유로 별개 서비스가 흡수되는 것을 막는다."""
    return CFG.report_service_map.get(project, project)


def _clip(text, n=240):
    """로그 한 줄 상한. 잘렸으면 잘렸다고 표시 — 표시가 없으면 모델이 '로그가 중간에 잘려 있다'며
    본문 대신 안내문을 쓰거나 남은 프로젝트를 통째로 건너뛴다(2026-08-26 일일 리포트 사고)."""
    return text if len(text) <= n else text[:n].rstrip() + " …(이하 생략)"


_BULLET = re.compile(r"^\s*[-*+] ")


def strip_meta(md):
    """모델이 붙인 머리말·맺음말을 잘라내고 불릿 마크다운만 남긴다.
    프롬프트로 금지해도 로그가 잘려 보이면 '확인 가능한 범위까지만 정리했습니다' 류를 앞에 붙인다.
    불릿이 하나도 없으면(작업 없음 안내 등) 원문 그대로 둔다."""
    lines = md.splitlines()
    at = [i for i, line in enumerate(lines) if _BULLET.match(line)]
    if not at:
        return md.strip()
    return "\n".join(lines[at[0]:at[-1] + 1]).strip()


def _mentions_excluded(text):
    """제외 프로젝트가 언급된 로그 줄인지 — 표기 차이('a-b'/'a b'/'a_b')를 무시하고 비교.
    제외 프로젝트를 다룬 작업(정리·모니터링 등)의 로그가 다른 프로젝트 섹션을 타고
    요약에 이름을 되살리는 것을 막는다."""
    t = re.sub(r"[^0-9a-z가-힣]", "", text.lower())
    return any(n in t for n in _EXCL_NORM)


def _pred(range_):
    """ts_hub(UTC 저장)를 로컬 날짜로 환산한 윈도우 조건. 파라미터는 _params로.
    week의 day는 그 주 월요일, month의 day는 그 달 1일로 정규화되어 들어온다(달력 고정)."""
    if range_ == "week":
        return "date(ts_hub,'localtime') BETWEEN date(?) AND date(?,'+6 days')"
    if range_ == "month":
        return "date(ts_hub,'localtime') BETWEEN date(?) AND date(?,'+1 month','-1 day')"
    return "date(ts_hub,'localtime') = date(?)"


def _params(range_, day):
    return (day, day) if range_ in ("week", "month") else (day,)


def gather(c, range_, day):
    """프로젝트별 작업 원재료: {project: {prompts, sums, turns, sessions}}.
    지시 없거나 요약 없는 프로젝트, REPORT_EXCLUDE_PROJECTS 프로젝트는 제외."""
    pred = _pred(range_)
    skip = ("", "summarizer") + CFG.report_exclude_projects
    rows = c.execute(
        f"SELECT project, event, session_id, device_id, payload FROM events e"
        f" WHERE event IN ('prompt','turn_done')"
        f"   AND COALESCE(project,'') NOT IN ({','.join('?' * len(skip))})"
        f"   AND {pred} AND {NOT_AUTO} ORDER BY project, ts_hub",
        skip + _params(range_, day)).fetchall()
    proj = {}
    for r in rows:
        d = proj.setdefault(r["project"], {"prompts": [], "sums": [], "turns": 0, "sess": set()})
        d["sess"].add((r["device_id"], r["session_id"]))
        pl = json.loads(r["payload"] or "{}")
        if r["event"] == "prompt":
            t = (pl.get("prompt") or "").strip()
            if t and not t.startswith(NOISE) and not _mentions_excluded(t):
                d["prompts"].append(_clip(t))
        else:
            d["turns"] += 1
            s = (pl.get("summary") or "").strip()
            if s and not _mentions_excluded(s):
                d["sums"].append(_clip(s))
    return {p: {"prompts": v["prompts"], "sums": v["sums"], "turns": v["turns"],
                "sessions": len(v["sess"])}
            for p, v in proj.items() if v["prompts"] or v["sums"]}


def known_services(c, days=120, min_events=20):
    """최근 로그에 실제로 나타난 프로젝트를 서비스명으로 환산한 목록(빈도순).
    작업 대상이 로그가 수집된 프로젝트와 다를 때(예: 어느 서비스 저장소를 열어둔 채 허브 결함을 확인),
    모델이 이름을 새로 짓지 않고 이 표기 중 하나를 쓰도록 프롬프트에 함께 넣는다.
    잡다한 임시 디렉터리명이 끼지 않게 최소 이벤트 수로 거른다."""
    rows = c.execute(
        f"SELECT project, COUNT(*) n FROM events e"
        f" WHERE COALESCE(project,'') NOT IN ('','summarizer')"
        f"   AND ts_hub >= datetime('now',?) AND {NOT_AUTO}"
        f" GROUP BY project HAVING n >= ? ORDER BY n DESC", (f"-{days} days", min_events))
    out = []
    for r in rows:
        if r["project"] in CFG.report_exclude_projects:
            continue
        svc = _service(r["project"])
        if svc not in out:
            out.append(svc)
    return out


def build_prompt(range_, day, work, services=()):
    """프로젝트별 로그를 하나의 마크다운 요청으로 (LLM 1회 호출).
    출력은 PM/PO 관점의 보고용 마크다운 — 기간이 길수록 나열이 아니라 더 포괄적인 종합."""
    cap = {"day": 20, "week": 40, "month": 60}[range_]   # 기간이 길수록 로그가 많다 — 상한 완화
    blocks = []
    for p, v in sorted(work.items(), key=lambda kv: (_service(kv[0]), kv[0])):
        b = [f"=== 서비스: {_service(p)} | 프로젝트: {p} (턴 {v['turns']}) ==="]
        if v["prompts"]:
            b.append("[지시]\n" + "\n".join("- " + x for x in v["prompts"][:cap]))
        if v["sums"]:
            b.append("[완료요약]\n" + "\n".join("- " + x for x in v["sums"][:cap]))
        blocks.append("\n".join(b))
    label = {"day": "하루", "week": "한 주(월~일)", "month": "한 달"}[range_]
    kind = {"day": "업무일지", "week": "주간보고", "month": "월간보고"}[range_]
    head = (
        f"아래는 {label}({day} 기준) 동안 AI 코딩 에이전트에게 준 지시와 완료 로그를 프로젝트별로 모은 것이다.\n"
        f"이걸 **{kind}용 마크다운**으로 정리하라. 읽는 사람은 프로덕트/프로젝트 매니저·오너다 —\n"
        "개발 구현 디테일이 아니라 **제품에 무엇이 달라졌고 어디까지 왔는지**를 명확하고 간결하게 서술한다.\n\n"
    )
    period_rules = {
        "day": (
            "일일 정리 규칙 (세션·턴의 나열이 아니라 기능 단위 정리):\n"
            "- 같은 서비스/기능을 하루에 여러 번 다뤘으면 **하나로 합쳐** 결과 중심으로 정리한다.\n"
            "- 구현 중 방향 전환·보류·취소가 있었으면 과정을 늘어놓지 말고 최종 상태로 표기한다\n"
            "  (예: '~ 구현 → 접근 변경', '~ 시도 → 보류', '~ 추가했다 제거').\n\n"
        ),
        "week": (
            "주간 종합 규칙 (중요 — 일일의 나열이 아니라 한 주의 종합·보고):\n"
            "- 한 주 동안 같은 작업이 만들어졌다 수정·번복·재정리된 경우, 과정을 나열하지 말고\n"
            "  **주말 기준 최종 상태 한 줄**로 정리한다 (예: 색을 3번 바꿨어도 '색상 체계 확정' 하나).\n"
            "- 기간 내 여러 프로젝트/기능이 같은 흐름이면 통합해 서술한다 — 기능당 불릿 1~3개,\n"
            "  지엽적 수정은 묶거나 생략. 항목 수는 일일보다 줄어야 정상이다.\n"
            "- 12칸 세부 단계는 꼭 필요한 경우에만. 전체가 한 화면에 들어올 분량을 지향한다.\n\n"
        ),
        "month": (
            "월간 종합 규칙 (중요 — 주간보다 한 단계 더 포괄적인 종합·보고):\n"
            "- 일·주 단위 사건이 아니라 **한 달의 성과와 진척**을 쓴다. 여러 기능/프로젝트가 하나의\n"
            "  방향이면 묶어서 '무엇이 어디까지 왔는지'로 서술한다 (예: '결제 개편 — 설계부터 구현까지 완료').\n"
            "- 서비스당 굵직한 주제 2~4개, 주제당 불릿 1~2개. 지엽적 수정·시행착오·중간 과정은 쓰지 않는다.\n"
            "- 12칸 세부 단계 금지. 분량은 주간과 비슷하거나 짧아야 한다 — 기간이 길다고 길어지면 실패다.\n\n"
        ),
    }[range_]
    return (
        head +
        "구조 — 헤더(#) 없이 전부 불릿, 3단계 중첩:\n"
        "- **최상위 불릿(들여쓰기 0)** = 각 로그 블록 머리에 적힌 **서비스명 그대로**. 이름을 바꾸거나\n"
        "  줄이거나 새로 짓지 않는다. **서비스명이 다른 블록은 절대 합치지 않는다** — 이름이 비슷해도\n"
        "  (예: 서로 다른 서비스인데 접두어만 같은 경우) 각각 별도 최상위 불릿으로 둔다.\n"
        "  같은 서비스명이 붙은 블록이 여러 개일 때만 하나로 합친다.\n"
        "  서비스명은 이름만 짧게 — 괄호 부연·설명·볼드(**) 금지.\n"
        "  단, 보고할 내용이 없는 블록(잡담·중단된 지시뿐)은 **최상위 불릿 자체를 만들지 않는다** —\n"
        "  '기록 없음' 같은 빈 항목을 채워 넣지 말고 통째로 생략한다.\n"
        "- 어느 블록에 담을지는 **작업의 대상** 기준이다. 로그는 그때 열려 있던 저장소에 붙어 수집되므로,\n"
        "  A 저장소에서 일하다 **다른 서비스 B의 결함·개선을 확인·처리**했다면 그 항목은 A가 아니라\n"
        "  B 아래에 둔다 (예: 서비스 저장소에서 작업 중 발견한 협업 도구 자체의 알림 문제 → 그 도구).\n"
        "  이때 B가 그 기간에 블록이 없어도 최상위 불릿을 새로 만들어도 된다. B의 이름은 아래\n"
        "  '알려진 서비스' 표기를 그대로 쓰고, 목록에 없으면 옮기지 말고 원래 블록에 둔다.\n"
        "- **4칸 들여쓴 불릿** = 기능/영역/주제 (예: 결제, 모바일앱, 워커 배치).\n"
        "- **8칸 들여쓴 불릿** = 구체적으로 한 일. 더 세부는 12칸.\n\n"
        + period_rules +
        "서술 규칙:\n"
        "- 프로덕트/프로젝트 매니저·오너가 읽는 보고서다. 함수명·변수명·파일명·내부 구현 용어를 쓰지\n"
        "  말고, **무엇이 달라졌는지 / 어떤 결정이 났는지 / 어디까지 진행됐는지**로 표현한다.\n"
        "- 간결한 명사구·완료형. `주제; 세부`, `→ 결과·전환` 표기를 활용해도 좋다.\n"
        "- 핸드오프·환경 설정·도구 정비처럼 수단·프로세스 성격의 작업은 **무엇에 대한 작업이었는지**\n"
        "  (대상 기능·과제)를 반드시 함께 적는다 — '기기 간 작업 이관'처럼 대상 없이 수단만 적지 않는다.\n"
        "- 잡담·질문·메타 대화·시스템 알림·불완전 지시는 제외. 실제 수행·결정한 것만, 추측 금지.\n"
        "- 로그 항목은 길면 끝에 '…(이하 생략)'이 붙어 있다. 잘린 항목도 드러난 범위까지 반영하되,\n"
        "  **잘림 자체는 언급하지 않는다**. 로그가 부족해 보여도 되묻지 말고 확인되는 것만 정리한다.\n"
        "- 머리말·맺음말·총평·인사·안내문·사과·헤더(#) 없이 **불릿만** 출력한다. 첫 글자는 반드시 '- '.\n"
        "  모든 블록을 빠짐없이 다룬다 — 일부 블록만 정리하고 나머지를 남기지 않는다.\n\n"
        + (("알려진 서비스(다른 서비스 대상 항목을 옮길 때 이 표기를 그대로 쓴다):\n- "
            + ", ".join(services) + "\n\n") if services else "")
        + "로그:\n" + "\n\n".join(blocks)
    )


def fallback_md(work):
    """LLM 실패/미가용 시 — 원재료 기반 최소 마크다운(요약 없이 나열)."""
    out, seen = [], None
    for p, v in sorted(work.items(), key=lambda kv: (_service(kv[0]), kv[0])):
        if _service(p) != seen:                       # 같은 서비스로 매핑된 프로젝트는 한 불릿 아래로
            seen = _service(p)
            out.append(f"- {seen}")
        out += ["    - " + x[:100] for x in (v["sums"] or v["prompts"])[:6]]
    return "\n".join(out).strip() or "이 기간에 기록된 작업이 없습니다."


# 자동화(frontend='auto') 세션의 이벤트 제외 — 분 단위로 도는 상시 감시 잡이 수치를 압도해
# 사람 작업 메트릭이 무의미해지는 것을 방지. 리포트 본문(gather)에서도 제외한다(2026-08-18 사용자 결정
# — 반복 자동화는 업무일지에 쓸 내용이 아님).
NOT_AUTO = (" NOT EXISTS (SELECT 1 FROM sessions s WHERE s.device_id=e.device_id"
            " AND s.agent=e.agent AND s.session_id=e.session_id AND s.frontend='auto')")


def metrics(c, range_, day):
    """활동 메트릭 — 윈도우 집계 + 프로젝트·기기/시간대 분포 + 잔디(최근 364일=52주, 윈도우 무관).
    잔디 기간은 52주 고정 — EVENT_RETENTION_DAYS를 유한하게 두면 그보다 짧게 유지할 것.
    자동화 세션은 전 수치에서 제외."""
    pred = _pred(range_)
    pr = _params(range_, day)
    turns = c.execute(
        f"SELECT COUNT(*) n FROM events e WHERE event='turn_done' AND {pred} AND {NOT_AUTO}",
        pr).fetchone()["n"]
    sessions = c.execute(
        f"SELECT COUNT(*) n FROM (SELECT DISTINCT device_id, session_id FROM events e"
        f" WHERE {pred} AND {NOT_AUTO})", pr).fetchone()["n"]
    projects = c.execute(
        f"SELECT COUNT(DISTINCT project) n FROM events e"
        f" WHERE COALESCE(project,'') NOT IN ('','summarizer') AND {pred} AND {NOT_AUTO}",
        pr).fetchone()["n"]
    # 활동 시간 ≈ 이벤트가 있는 30분 슬롯 수 × 0.5h (연속 몰입시간 근사)
    slots = c.execute(
        f"SELECT COUNT(*) n FROM (SELECT DISTINCT strftime('%Y%m%d%H',ts_hub,'localtime'),"
        f" CAST(strftime('%M',ts_hub,'localtime') AS INTEGER)/30 FROM events e"
        f" WHERE {pred} AND {NOT_AUTO})", pr).fetchone()["n"]
    per_project = [dict(r) for r in c.execute(
        f"SELECT project, COUNT(*) turns FROM events e WHERE event='turn_done'"
        f" AND COALESCE(project,'') NOT IN ('','summarizer') AND {pred} AND {NOT_AUTO}"
        f" GROUP BY project ORDER BY turns DESC", pr)]
    per_device = [dict(r) for r in c.execute(
        f"SELECT d.name AS device, COUNT(*) turns FROM events e"
        f" JOIN devices d ON d.id=e.device_id"
        f" WHERE event='turn_done' AND {pred} AND {NOT_AUTO}"
        f" GROUP BY d.name ORDER BY turns DESC", pr)]
    hourly = {r["h"]: r["n"] for r in c.execute(
        f"SELECT strftime('%H',ts_hub,'localtime') h, COUNT(*) n FROM events e"
        f" WHERE event='turn_done' AND {pred} AND {NOT_AUTO} GROUP BY h", pr)}
    streak = {r["d"]: r["n"] for r in c.execute(
        f"SELECT date(ts_hub,'localtime') d, COUNT(*) n FROM events e"
        f" WHERE event='turn_done' AND ts_hub >= datetime('now','-365 days') AND {NOT_AUTO}"
        f" GROUP BY d")}
    return {
        "range": range_, "day": day,
        "turns": turns, "sessions": sessions, "projects": projects,
        "active_hours": round(slots * 0.5, 1),
        "per_project": per_project,
        "per_device": per_device,
        "hourly": [{"hour": f"{i:02d}", "n": hourly.get(f"{i:02d}", 0)} for i in range(24)],
        "streak": streak,
    }
