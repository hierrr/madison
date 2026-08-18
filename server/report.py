"""일일/주간 업무 리포트 + 사용 메트릭.

events(prompt/turn_done)를 프로젝트별로 모아 업무일지용 마크다운으로 요약하고,
에이전트 사용 메트릭(세션·턴·활동시간·시간대·잔디)을 집계한다.
LLM 호출은 app에서 주입한다 — 이 모듈은 순수 데이터/문자열만 다룬다.
"""
import json

# 실제 작업이 아닌 프롬프트(에이전트 알림·시스템 주입 등)는 리포트에서 제외
NOISE = ("<task-notification", "<system-reminder", "<command", "<local-command")


def _pred(range_):
    """ts_hub(UTC 저장)를 로컬 날짜로 환산한 윈도우 조건. 파라미터는 _params로.
    week의 day는 항상 그 주 월요일로 정규화되어 들어온다(달력 주 고정, 월~일)."""
    if range_ == "week":
        return "date(ts_hub,'localtime') BETWEEN date(?) AND date(?,'+6 days')"
    return "date(ts_hub,'localtime') = date(?)"


def _params(range_, day):
    return (day, day) if range_ == "week" else (day,)


def gather(c, range_, day):
    """프로젝트별 작업 원재료: {project: {prompts, sums, turns, sessions}}.
    지시 없거나 요약 없는 프로젝트는 제외."""
    pred = _pred(range_)
    rows = c.execute(
        f"SELECT project, event, session_id, device_id, payload FROM events e"
        f" WHERE event IN ('prompt','turn_done') AND COALESCE(project,'') NOT IN ('','summarizer')"
        f"   AND {pred} AND {NOT_AUTO} ORDER BY project, ts_hub", _params(range_, day)).fetchall()
    proj = {}
    for r in rows:
        d = proj.setdefault(r["project"], {"prompts": [], "sums": [], "turns": 0, "sess": set()})
        d["sess"].add((r["device_id"], r["session_id"]))
        pl = json.loads(r["payload"] or "{}")
        if r["event"] == "prompt":
            t = (pl.get("prompt") or "").strip()
            if t and not t.startswith(NOISE):
                d["prompts"].append(t[:240])
        else:
            d["turns"] += 1
            s = (pl.get("summary") or "").strip()
            if s:
                d["sums"].append(s[:240])
    return {p: {"prompts": v["prompts"], "sums": v["sums"], "turns": v["turns"],
                "sessions": len(v["sess"])}
            for p, v in proj.items() if v["prompts"] or v["sums"]}


def build_prompt(range_, day, work):
    """프로젝트별 로그를 하나의 마크다운 요청으로 (LLM 1회 호출).
    출력은 업무일지/주간보고용 — 서비스·관리 관점, 중첩 불릿. 주간은 종합(일일 나열 금지)."""
    week = range_ == "week"
    cap = 40 if week else 20   # 주간은 로그가 많다 — 프로젝트당 상한 완화
    blocks = []
    for p, v in work.items():
        b = [f"=== 프로젝트: {p} (턴 {v['turns']}) ==="]
        if v["prompts"]:
            b.append("[지시]\n" + "\n".join("- " + x for x in v["prompts"][:cap]))
        if v["sums"]:
            b.append("[완료요약]\n" + "\n".join("- " + x for x in v["sums"][:cap]))
        blocks.append("\n".join(b))
    label = "한 주(월~일)" if week else "하루"
    head = (
        f"아래는 {label}({day} 기준) 동안 AI 코딩 에이전트에게 준 지시와 완료 로그를 프로젝트별로 모은 것이다.\n"
        + ("이걸 **주간보고용 마크다운**으로 정리하라. 미팅에서 공유하는 자료다 — 개발 구현 디테일이\n"
           "아니라 **서비스·제품 관리 관점**에서 '이번 주에 무엇이 되었는지'를 서술한다.\n\n"
           if week else
           "이걸 **업무일지용 마크다운**으로 정리하라. 개발 구현 디테일이 아니라 "
           "**서비스·제품 관리 관점**에서 '무엇을 했는지'를 서술한다.\n\n")
    )
    weekly_rules = (
        "주간 종합 규칙 (중요 — 일일의 나열이 아니라 한 주의 종합):\n"
        "- 한 주 동안 같은 작업이 만들어졌다 수정·번복·재정리된 경우, 과정을 나열하지 말고\n"
        "  **주말 기준 최종 상태 한 줄**로 정리한다 (예: 색을 3번 바꿨어도 '색상 체계 확정' 하나).\n"
        "- 서비스/기능 단위로 크게 묶고 항목 수를 줄인다 — 기능당 불릿 1~3개, 지엽적 수정은 묶거나 생략.\n"
        "- 12칸 세부 단계는 꼭 필요한 경우에만. 전체가 한 화면에 들어올 분량을 지향한다.\n\n"
    ) if week else (
        "일일 정리 규칙 (세션·턴의 나열이 아니라 기능 단위 정리):\n"
        "- 같은 서비스/기능을 하루에 여러 번 다뤘으면 **하나로 합쳐** 결과 중심으로 정리한다.\n"
        "- 구현 중 방향 전환·보류·취소가 있었으면 과정을 늘어놓지 말고 최종 상태로 표기한다\n"
        "  (예: '~ 구현 → 접근 변경', '~ 시도 → 보류', '~ 추가했다 제거').\n\n"
    )
    return (
        head +
        "구조 — 헤더(#) 없이 전부 불릿, 3단계 중첩:\n"
        "- **최상위 불릿(들여쓰기 0)** = 서비스명. 제품/서비스 단위로 묶는다. 여러 프로젝트가 같은\n"
        "  서비스면 하나로 합친다(서비스명은 작업 내용에 드러나는 제품명, 예: phdkim* 계열 → 김박사넷;\n"
        "  불명확하면 프로젝트명). 서비스명은 이름만 짧게 — 괄호 부연·설명·볼드(**) 금지.\n"
        "- **4칸 들여쓴 불릿** = 기능/영역/주제 (예: 재팬라운지, 모바일앱, 워커 배치).\n"
        "- **8칸 들여쓴 불릿** = 구체적으로 한 일. 더 세부는 12칸.\n\n"
        + weekly_rules +
        "서술 규칙:\n"
        "- 관리자·기획자가 읽는 보고서다. 함수명·변수명·내부 구현 용어 대신 **서비스/사용자 관점** 표현.\n"
        "- 간결한 명사구·완료형. `주제; 세부`, `→ 결과·전환` 표기를 활용해도 좋다.\n"
        "- 잡담·질문·메타 대화·시스템 알림·불완전 지시는 제외. 실제 수행·결정한 것만, 추측 금지.\n"
        "- 머리말·맺음말·총평·인사·헤더(#) 없이 불릿 마크다운만 출력.\n\n"
        "로그:\n" + "\n\n".join(blocks)
    )


def fallback_md(work):
    """LLM 실패/미가용 시 — 원재료 기반 최소 마크다운(요약 없이 나열)."""
    out = []
    for p, v in work.items():
        out.append(f"- {p}")
        out += ["    - " + x[:100] for x in (v["sums"] or v["prompts"])[:6]]
    return "\n".join(out).strip() or "이 기간에 기록된 작업이 없습니다."


# 자동화(frontend='auto') 세션의 이벤트 제외 — 상시 감시 잡(phdkim-regular 등)이 수치를 압도해
# 사람 작업 메트릭이 무의미해지는 것을 방지. 리포트 본문(gather)에서도 제외한다(2026-08-18 사용자 결정
# — 반복 자동화는 업무일지에 쓸 내용이 아님).
NOT_AUTO = (" NOT EXISTS (SELECT 1 FROM sessions s WHERE s.device_id=e.device_id"
            " AND s.agent=e.agent AND s.session_id=e.session_id AND s.frontend='auto')")


def metrics(c, range_, day):
    """활동 메트릭 — 윈도우 집계 + 프로젝트/시간대 분포 + 잔디(최근 112일, 윈도우 무관).
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
    hourly = {r["h"]: r["n"] for r in c.execute(
        f"SELECT strftime('%H',ts_hub,'localtime') h, COUNT(*) n FROM events e"
        f" WHERE event='turn_done' AND {pred} AND {NOT_AUTO} GROUP BY h", pr)}
    streak = {r["d"]: r["n"] for r in c.execute(
        f"SELECT date(ts_hub,'localtime') d, COUNT(*) n FROM events e"
        f" WHERE event='turn_done' AND ts_hub >= datetime('now','-112 days') AND {NOT_AUTO}"
        f" GROUP BY d")}
    return {
        "range": range_, "day": day,
        "turns": turns, "sessions": sessions, "projects": projects,
        "active_hours": round(slots * 0.5, 1),
        "per_project": per_project,
        "hourly": [{"hour": f"{i:02d}", "n": hourly.get(f"{i:02d}", 0)} for i in range(24)],
        "streak": streak,
    }
