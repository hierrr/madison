"""일일/주간/월간 업무 리포트 + 사용 메트릭.

일일: events(prompt/turn_done)를 프로젝트 → 세션 → 턴(지시·응답 짝)으로 묶어 업무일지 마크다운으로 요약한다.
주간·월간: 저장된 **일일 업무일지**를 재료로 종합한다 — 원본 로그를 다시 읽지 않는다. 기간이 길수록
로그가 상한을 넘겨 뒷부분이 통째로 잘리던 문제가 없어지고, 일일에서 정한 서비스·주제 이름이 그대로 이어진다.
에이전트 사용 메트릭(세션·턴·활동시간·시간대·잔디)도 여기서 집계한다.
LLM 호출은 app에서 주입한다 — 이 모듈은 순수 데이터/문자열만 다룬다.
"""
import datetime
import json
import re

from .config import CFG

EMPTY_MD = "이 기간에 기록된 작업이 없습니다."

# 실제 작업이 아닌 프롬프트(에이전트 알림·시스템 주입 등)는 리포트에서 제외
NOISE = ("<task-notification", "<system-reminder", "<command", "<local-command")

# 내용 없는 응답 — 알림만 받고 할 일이 없던 턴의 정형 문구. 걸러내지 않으면 자리만 차지한다
# (2026-08-27 일일: 45턴 중 13턴).
_EMPTY_RESPONSES = ("no response requested",)

# task-notification은 버리되 <summary>의 제목만 배경 맥락으로 살린다 — 그 시각 끝난 백그라운드 작업의
# 제목이라, 이어지는 응답의 대상('무슨 데이터'·'어느 기능')이 그날의 지시·응답에서 빠져 있을 때
# 이름을 되찾아 준다(2026-08-23 일일 리포트에 대상 없는 '데이터 재수집'만 남은 사례).
_NOTE_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)
_NOTE_LABEL = re.compile(r'"([^"]{4,})"')


def _note(text):
    """알림 원문 → 배경 한 줄. 제목이 따옴표로 묶여 있으면 그 제목만 취해
    같은 작업의 시작·종료 알림이 한 줄로 합쳐지게 한다."""
    m = _NOTE_SUMMARY.search(text)
    if not m:
        return ""
    s = " ".join(m.group(1).split())
    q = _NOTE_LABEL.search(s)
    return (q.group(1) if q else s).strip()


def _real_response(text):
    t = text.strip().lower().rstrip(".!")
    return bool(t) and not any(t.startswith(x) for x in _EMPTY_RESPONSES)


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


_INDENT = re.compile(r"^( *)[-*+] ")


def topics(md, max_indent=4):
    """업무일지에서 주제 수준(들여쓰기 ≤ max_indent) 불릿만 — 직전 일지를 맥락으로 넣을 때 쓴다.
    세부(8칸 이상)는 넣지 않는다: 어제 한 일이 오늘 한 일로 되살아나는 것을 막는다."""
    out = []
    for line in md.splitlines():
        m = _INDENT.match(line)
        if m and len(m.group(1)) <= max_indent:
            out.append(line.rstrip())
    return "\n".join(out)


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


def period_days(range_, day, today):
    """기간에 속하는 로컬 날짜 목록(오늘까지). week=월요일 기준 7일, month=1일 기준 그 달."""
    d = datetime.date.fromisoformat(day)
    if range_ == "week":
        end = d + datetime.timedelta(days=6)
    elif range_ == "month":
        end = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    else:
        end = d
    end = min(end, datetime.date.fromisoformat(today))
    return [(d + datetime.timedelta(days=i)).isoformat() for i in range((end - d).days + 1)]


def _weekday(day):
    return "월화수목금토일"[datetime.date.fromisoformat(day).weekday()]


# ── 일일 원재료: 프로젝트 → 세션 → 턴 ─────────────────

def gather(c, range_, day):
    """프로젝트별 작업 원재료 — {project: {sessions, turns, n_sessions}}.
    sessions = [{device, start, end, turns: [{prompts, notes, response}]}] (세션·턴 모두 시간순).
    한 턴 = 지시(들) → 응답. 같은 세션에서 응답이 붙기 전까지의 지시는 한 턴에 모은다(알림이 쌓이거나
    실행 중 지시를 이어 보낸 경우). 지시도 응답도 없는 턴(알림만 받고 빈 응답)은 버린다.
    지시·응답이 하나도 없는 프로젝트, REPORT_EXCLUDE_PROJECTS 프로젝트, 자동화 세션은 제외."""
    pred = _pred(range_)
    skip = ("", "summarizer") + CFG.report_exclude_projects
    rows = c.execute(
        f"SELECT e.project, e.event, e.session_id, e.device_id, e.payload, d.name AS device,"
        f" strftime('%m/%d %H:%M', e.ts_hub, 'localtime') AS t"
        f" FROM events e LEFT JOIN devices d ON d.id=e.device_id"
        f" WHERE e.event IN ('prompt','turn_done')"
        f"   AND COALESCE(e.project,'') NOT IN ({','.join('?' * len(skip))})"
        f"   AND {pred} AND {NOT_AUTO} ORDER BY e.ts_hub, e.id",
        skip + _params(range_, day)).fetchall()
    proj = {}
    for r in rows:
        p = proj.setdefault(r["project"], {"sess": {}, "turns": 0})
        s = p["sess"].setdefault(
            (r["device_id"], r["session_id"]),
            {"device": r["device"] or "?", "start": r["t"], "end": r["t"], "turns": []})
        s["end"] = r["t"]
        pl = json.loads(r["payload"] or "{}")
        cur = s["turns"][-1] if s["turns"] and s["turns"][-1]["response"] is None else None
        if r["event"] == "prompt":
            t = (pl.get("prompt") or "").strip()
            if not t or _mentions_excluded(t):
                continue
            if cur is None:
                cur = {"prompts": [], "notes": [], "response": None}
                s["turns"].append(cur)
            if t.startswith("<task-notification"):
                n = _note(t)
                if n and n not in cur["notes"]:
                    cur["notes"].append(_clip(n, 120))
            elif not t.startswith(NOISE):
                cur["prompts"].append(_clip(t))
        else:
            p["turns"] += 1
            resp = (pl.get("summary") or "").strip()
            if not _real_response(resp) or _mentions_excluded(resp):
                resp = ""
            if cur is None:
                cur = {"prompts": [], "notes": [], "response": None}
                s["turns"].append(cur)
            cur["response"] = _clip(resp)
    out = {}
    for name, p in proj.items():
        sessions = []
        for s in p["sess"].values():
            turns = [t for t in s["turns"] if t["prompts"] or t["response"]]
            for t in turns:
                t["response"] = t["response"] or ""
            if turns:
                sessions.append({**s, "turns": turns})
        if sessions:
            out[name] = {"sessions": sessions, "turns": p["turns"], "n_sessions": len(p["sess"])}
    return out


def known_services(c, days=120, min_events=20):
    """최근 로그에 실제로 나타난 프로젝트를 서비스명으로 환산한 목록(빈도순) + REPORT_KNOWN_SERVICES.
    작업 대상이 로그가 수집된 프로젝트와 다를 때(예: 어느 서비스 저장소를 열어둔 채 허브 결함을 확인,
    모노리포 안에서 다른 서비스를 작업), 모델이 이름을 새로 짓지 않고 이 표기 중 하나를 쓰도록 프롬프트에 넣는다.
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
    for name in CFG.report_known_services:          # 프로젝트가 없는 서비스도 항상 후보에
        if name not in out:
            out.append(name)
    return out


# ── 렌더링·압축 ───────────────────────────────────────

def _render_turn(t):
    if t["prompts"]:
        head = "- 지시: " + " / ".join(t["prompts"])
        if t["notes"]:
            head += "  (알림: " + "; ".join(t["notes"]) + ")"
    elif t["notes"]:
        head = "- 알림: " + "; ".join(t["notes"])
    else:
        head = "- (지시 기록 없음)"
    return head + ("\n  응답: " + t["response"] if t["response"] else "")


def _render_session(s):
    head = f"[세션 · {s['device']} · {s['start']}~{s['end']} · 턴 {len(s['turns'])}]"
    if s.get("digest"):
        return head + " (압축 요약)\n" + s["digest"]
    return "\n".join([head] + [_render_turn(t) for t in s["turns"]])


def _render_block(p, v):
    head = f"=== 서비스: {_service(p)} | 프로젝트: {p} (세션 {v['n_sessions']}, 턴 {v['turns']}) ==="
    return "\n\n".join([head] + [_render_session(s) for s in v["sessions"]])


BLOCK_BUDGET = 40_000   # 프로젝트 블록당 문자 예산 — 평소 하루는 여유 있게 들어가고, 넘치면 압축


def digest_prompt(project, s):
    n = max(8, min(30, len(s["turns"]) // 3))
    return (
        f"아래는 프로젝트 '{project}'의 한 세션에서 AI 코딩 에이전트에게 준 지시와 응답의 시간순 기록이다.\n"
        f"나중에 업무일지로 정리할 재료로 쓰이도록 **시간순 불릿 {n}개 이내**로 압축하라.\n"
        "- 무엇을 했고(대상 데이터·화면·기능 이름 포함) 어떤 결정·결과·수치가 나왔는지, 최종 상태가 무엇인지 보존한다.\n"
        "- 대기·확인·잡담·되묻기 응답은 버린다. 방향이 바뀐 작업은 과정 대신 최종 상태로 적는다.\n"
        "- 머리말·맺음말 없이 불릿만 출력한다. 첫 글자는 반드시 '- '.\n\n"
        + _render_session(s))


def compress(work, llm, budget=BLOCK_BUDGET):
    """블록이 예산을 넘는 프로젝트는 긴 세션부터 llm(prompt)→str 압축 요약(digest)으로 바꿔 예산 안에 넣는다.
    잘라내지 않는다 — 상한으로 뒷부분을 버리면 그날 오후 작업이 통째로 사라진다(2026-08-27 사고).
    llm이 빈 문자열을 돌려주면 그 세션은 원문을 유지하고 다음 세션으로 넘어간다."""
    for p, v in work.items():
        for s in sorted(v["sessions"], key=lambda x: -len(_render_session(x))):
            if len(_render_block(p, v)) <= budget:
                break
            if s.get("digest") or len(s["turns"]) < 3:
                continue
            d = strip_meta(llm(digest_prompt(p, s)))
            if d:
                s["digest"] = d


# ── 프롬프트 ──────────────────────────────────────────

_AUDIENCE = (
    "읽는 사람은 프로덕트/프로젝트 매니저·오너다 —\n"
    "개발 구현 디테일이 아니라 **제품에 무엇이 달라졌고 어디까지 왔는지**를 명확하고 간결하게 서술한다.\n\n"
)

_STRUCTURE_HEAD = (
    "구조 — 헤더(#) 없이 전부 불릿, 3단계 중첩:\n"
    "- **최상위 불릿(들여쓰기 0)** = 각 로그 블록 머리에 적힌 **서비스명 그대로**. 이름을 바꾸거나\n"
    "  줄이거나 새로 짓지 않는다. **서비스명이 다른 블록은 절대 합치지 않는다** — 이름이 비슷해도\n"
    "  (예: 서로 다른 서비스인데 접두어만 같은 경우) 각각 별도 최상위 불릿으로 둔다.\n"
    "  같은 서비스명이 붙은 블록이 여러 개일 때만 하나로 합친다.\n"
    "  서비스명은 이름만 짧게 — 괄호 부연·설명·볼드(**) 금지.\n"
    "  단, 보고할 내용이 없는 블록(잡담·중단된 지시뿐)은 **최상위 불릿 자체를 만들지 않는다** —\n"
    "  '기록 없음' 같은 빈 항목을 채워 넣지 말고 통째로 생략한다.\n"
)

_STRUCTURE_TAIL = (
    "- **4칸 들여쓴 불릿** = 과제/기능/영역 (예: 결제, 모바일앱, 워커 배치).\n"
    "- **8칸 들여쓴 불릿** = 구체적으로 한 일. 더 세부는 12칸.\n\n"
)

_STYLE = (
    "서술 규칙:\n"
    "- 프로덕트/프로젝트 매니저·오너가 읽는 보고서다. 함수명·변수명·파일명·내부 구현 용어를 쓰지\n"
    "  말고, **무엇이 달라졌는지 / 어떤 결정이 났는지 / 어디까지 진행됐는지**로 표현한다.\n"
    "- 간결한 명사구·완료형. `주제; 세부`, `→ 결과·전환` 표기를 활용해도 좋다.\n"
    "- 핸드오프·환경 설정·도구 정비처럼 수단·프로세스 성격의 작업은 **무엇에 대한 작업이었는지**\n"
    "  (대상 기능·과제)를 반드시 함께 적는다 — '기기 간 작업 이관'처럼 대상 없이 수단만 적지 않는다.\n"
    "- 각 항목은 그 리포트만 읽고도 무엇에 대한 작업인지 알 수 있어야 한다 — '데이터 수집',\n"
    "  '전량 분석', '오류 수정'처럼 **대상이 빠진 표기 금지**. 무슨 데이터·어느 화면·어느 기능인지를\n"
    "  항목이나 그 상위 불릿에 드러낸다.\n"
    "- 잡담·질문·메타 대화·시스템 알림·불완전 지시는 제외. 실제 수행·결정한 것만, 추측 금지.\n"
    "- 상태 표현을 보존한다 — 검토·권장·예정·진행 중인 것을 결정·완료로 격상하지 않는다.\n"
    "- 머리말·맺음말·총평·인사·안내문·사과·헤더(#) 없이 **불릿만** 출력한다. 첫 글자는 반드시 '- '.\n"
    "  모든 블록을 빠짐없이 다룬다 — 일부 블록만 정리하고 나머지를 남기지 않는다.\n\n"
)


def _services_note(services):
    if not services:
        return ""
    lines = []
    for s in services:
        hint = CFG.report_known_services.get(s, "")
        lines.append(f"- {s}" + (f" — {hint}" if hint else ""))
    return ("알려진 서비스 — 항목의 **작업 대상**이 이 중 하나면 로그 블록의 서비스명 대신 이 이름으로 최상위\n"
            "불릿을 세운다(표기 그대로). 단서가 적힌 서비스는 그 단서로 식별하고, 같은 세션에서 앞 지시가 밝힌\n"
            "대상·단서가 뒤 작업에도 이어지는지 흐름으로 판단한다:\n" + "\n".join(lines) + "\n\n")


def build_day_prompt(day, work, services=(), prev=None):
    """하루치 원재료(gather → compress) → 업무일지 요청 (LLM 1회 호출).
    prev=(날짜, 직전 업무일지 md): 주제 목록만 넣어 이어지는 작업의 이름·묶음을 잇게 한다."""
    blocks = [_render_block(p, v)
              for p, v in sorted(work.items(), key=lambda kv: (_service(kv[0]), kv[0]))]
    head = (
        f"아래는 하루({day}) 동안 AI 코딩 에이전트에게 준 지시와 그 응답을 프로젝트 → 세션 → 시간순으로\n"
        "모은 것이다. 이걸 **업무일지용 마크다운**으로 정리하라. " + _AUDIENCE
    )
    context = ""
    if prev:
        context = (
            f"직전 업무일지({prev[0]})의 주제 목록 — 오늘 작업의 맥락이다:\n{topics(prev[1])}\n"
            "오늘 항목이 이 주제의 연장이면 **같은 서비스·기능 이름을 이어 쓰고** 하나의 흐름으로 묶는다.\n"
            "이 목록의 일을 오늘 한 일로 다시 쓰지는 않는다 — 오늘 로그에 있는 것만 쓴다.\n\n"
        )
    structure = (
        _STRUCTURE_HEAD +
        "- 어느 블록에 담을지는 **작업의 대상** 기준이다. 로그는 그때 열려 있던 저장소에 붙어 수집되므로,\n"
        "  A 저장소에서 일하다 **다른 서비스 B의 결함·개선을 확인·처리**했다면 그 항목은 A가 아니라\n"
        "  B 아래에 둔다 (예: 서비스 저장소에서 작업 중 발견한 협업 도구 자체의 알림 문제 → 그 도구).\n"
        "  이때 B가 그 기간에 블록이 없어도 최상위 불릿을 새로 만들어도 된다. B의 이름은 아래\n"
        "  '알려진 서비스' 표기를 그대로 쓰고, 목록에 없으면 옮기지 말고 원래 블록에 둔다.\n"
        + _STRUCTURE_TAIL
    )
    rules = (
        "일일 정리 규칙 (세션·턴의 나열이 아니라 과제 단위 정리):\n"
        "- 한 세션의 지시·응답은 **하나의 이어지는 작업 흐름**이다. 턴마다 항목을 만들지 말고, 그 흐름이\n"
        "  무엇을 위한 작업이었는지(과제·대상)를 4칸 주제로 잡고 세부를 8칸·12칸에 둔다.\n"
        "- 4칸 주제는 **과제 단위**다. 한 과제의 검증·수정·되돌림·후속 검토·운영 방식 검토는 그 과제\n"
        "  하나 아래에 둔다 — 단계나 작업 성격(검토/운영/데이터)마다 주제를 쪼개지 않는다.\n"
        "  하루 종일 한 과제를 다뤘으면 4칸 주제가 한둘인 것이 정상이다.\n"
        "- 같은 서비스/과제를 여러 세션에서 다뤘으면 **하나로 합쳐** 결과 중심으로 정리한다.\n"
        "- 구현 중 방향 전환·보류·취소가 있었으면 과정을 늘어놓지 말고 최종 상태로 표기한다\n"
        "  (예: '~ 구현 → 접근 변경', '~ 시도 → 보류', '~ 추가했다 제거').\n\n"
    )
    log_notes = (
        "로그 읽는 법:\n"
        "- '지시:'는 사람이 준 지시, '응답:'은 그 턴의 에이전트 마지막 응답이다. 둘은 한 짝이다.\n"
        "- '알림:'으로 시작하는 턴은 그 시각 끝난 백그라운드 작업의 제목이고 뒤의 응답이 그 결과 처리다.\n"
        "  알림은 응답의 **대상·맥락을 식별하는 데만** 쓰고, 지시·응답에 없는 일을 알림만 보고 항목으로\n"
        "  만들지 않는다.\n"
        "- '(압축 요약)' 세션은 긴 세션을 미리 불릿으로 줄인 것이다 — 원문 턴과 같은 무게로 다룬다.\n"
        "- 항목이 길면 끝에 '…(이하 생략)'이 붙어 있다. 잘린 항목도 드러난 범위까지 반영하되,\n"
        "  **잘림 자체는 언급하지 않는다**. 로그가 부족해 보여도 되묻지 말고 확인되는 것만 정리한다.\n\n"
    )
    return (head + context + structure + rules + _STYLE + log_notes + _services_note(services)
            + "로그:\n" + "\n\n".join(blocks))


def build_period_prompt(range_, day, dailies, services=()):
    """주간·월간: 일일 업무일지 모음 → 종합 보고 요청 (LLM 1회 호출).
    dailies = [(날짜, 업무일지 md)] 날짜순. 원본 로그가 아니라 일일 결과를 재료로 쓴다."""
    label = {"week": "한 주(월~일)", "month": "한 달"}[range_]
    kind = {"week": "주간보고", "month": "월간보고"}[range_]
    head = (
        f"아래는 {label}({day} 시작) 동안의 **일일 업무일지**를 날짜순으로 모은 것이다. 각 업무일지는\n"
        "서비스 > 과제 > 세부의 3단계 불릿이다. 이걸 **" + kind + "용 마크다운**으로 종합하라. " + _AUDIENCE
    )
    structure = (
        _STRUCTURE_HEAD.replace("각 로그 블록 머리에 적힌", "업무일지의 최상위 불릿에 적힌")
        .replace("같은 서비스명이 붙은 블록이 여러 개일 때만 하나로 합친다.",
                 "날짜가 달라도 같은 서비스명이면 하나로 합친다.")
        .replace("보고할 내용이 없는 블록(잡담·중단된 지시뿐)", "보고할 내용이 없는 서비스")
        + _STRUCTURE_TAIL
    )
    rules = {
        "week": (
            "주간 종합 규칙 (중요 — 일일의 나열이 아니라 한 주의 종합·보고):\n"
            "- 한 주 동안 같은 작업이 만들어졌다 수정·번복·재정리된 경우, 과정을 나열하지 말고\n"
            "  **주말 기준 최종 상태 한 줄**로 정리한다 (예: 색을 3번 바꿨어도 '색상 체계 확정' 하나).\n"
            "- 여러 날에 걸친 같은 과제는 일일의 주제 이름을 이어 받아 하나로 묶는다 — 과제당 불릿 1~3개,\n"
            "  지엽적 수정은 묶거나 생략. 항목 수는 일일 합계보다 훨씬 줄어야 정상이다.\n"
            "- 12칸 세부 단계는 꼭 필요한 경우에만. 전체가 한 화면에 들어올 분량을 지향한다.\n\n"
        ),
        "month": (
            "월간 종합 규칙 (중요 — 주간보다 한 단계 더 포괄적인 종합·보고):\n"
            "- 일·주 단위 사건이 아니라 **한 달의 성과와 진척**을 쓴다. 여러 과제/프로젝트가 하나의\n"
            "  방향이면 묶어서 '무엇이 어디까지 왔는지'로 서술한다 (예: '결제 개편 — 설계부터 구현까지 완료').\n"
            "- 서비스당 굵직한 주제 2~4개, 주제당 불릿 1~2개. 지엽적 수정·시행착오·중간 과정은 쓰지 않는다.\n"
            "- 12칸 세부 단계 금지. 분량은 주간과 비슷하거나 짧아야 한다 — 기간이 길다고 길어지면 실패다.\n\n"
        ),
    }[range_]
    body = "\n\n".join(f"=== {d} ({_weekday(d)}) ===\n{md}" for d, md in dailies)
    return (head + structure + rules + _STYLE + _services_note(services) + "업무일지:\n" + body)


def fallback_md(work):
    """일일 LLM 실패/미가용 시 — 원재료 기반 최소 마크다운(요약 없이 나열)."""
    out, seen = [], None
    for p, v in sorted(work.items(), key=lambda kv: (_service(kv[0]), kv[0])):
        if _service(p) != seen:                       # 같은 서비스로 매핑된 프로젝트는 한 불릿 아래로
            seen = _service(p)
            out.append(f"- {seen}")
        lines = [t["response"] or " / ".join(t["prompts"])
                 for s in v["sessions"] for t in s["turns"]]
        out += ["    - " + x[:100] for x in lines[:6]]
    return "\n".join(out).strip() or EMPTY_MD


def fallback_period_md(dailies):
    """주간·월간 LLM 실패 시 — 일일 업무일지를 날짜 아래 그대로 나열."""
    out = []
    for d, md in dailies:
        out.append(f"- {d} ({_weekday(d)})")
        out += ["    " + line for line in md.splitlines() if line.strip()]
    return "\n".join(out).strip() or EMPTY_MD


# 자동화(frontend='auto') 세션의 이벤트 제외 — 분 단위로 도는 상시 감시 잡이 수치를 압도해
# 사람 작업 메트릭이 무의미해지는 것을 방지. 리포트 본문(gather)에서도 제외한다(2026-08-18 사용자 결정
# — 반복 자동화는 업무일지에 쓸 내용이 아님).
NOT_AUTO = (" NOT EXISTS (SELECT 1 FROM sessions s WHERE s.device_id=e.device_id"
            " AND s.agent=e.agent AND s.session_id=e.session_id AND s.frontend='auto')")


def last_event_at(c, day):
    """그날(로컬) 사람 세션의 마지막 지시·응답 시각(UTC ISO) — 저장된 일일 리포트가 그보다 오래됐으면 재생성 대상."""
    return c.execute(
        f"SELECT MAX(ts_hub) m FROM events e WHERE event IN ('prompt','turn_done')"
        f" AND date(ts_hub,'localtime') = date(?) AND {NOT_AUTO}", (day,)).fetchone()["m"]


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
