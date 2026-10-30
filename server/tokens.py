"""세션 누적 토큰 → 시간·일 원장 폴드와 조회.

수집기는 턴 종료마다 세션 **누적** 토큰 맵 {model: {in,out,cr,cw,th}}을 이벤트에 싣는다.
허브는 직전 누적(sessions.tokens_cum)과의 차이만 원장에 가산한다 — 재전송·스풀
유실에 멱등. 원칙: 폴드가 어떤 이유로 실패해도 ingest는 계속되어야 한다(호출부 try/except).

계산은 한 번, 조회는 두 단위: 쓰기 경로(add_ledger)가 허브 로컬 '시' 키 하나로 시간 원장
token_hourly와 일 원장 token_daily에 같은 값을 함께 가산한다. 1·7일 화면(window)은 시간 원장,
그 이상(_daily_summary)은 일 원장을 SQL로 읽을 뿐 — 이벤트를 다시 재생하는 코드는 조회에 없다
(일회성 시간 원장 채우기 backfill_hourly만 예외).
"""
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CFG

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
HOUR_FMT = "%Y-%m-%dT%H"
_CODEX_KEY = "_session"   # Codex 카운터는 세션 전역 — 모델 전환 시 이중 계상 방지용 고정 기준점
# 허브 내부 LLM 워커의 전용 cwd 이름(llm-cwd: 현행 LLM_CWD 기본값, summarizer: 과거 워커 디렉터리 — 백필 데이터에 남아 있음).
# 쓰임은 셋뿐이다: (1) 원장 project 개명 — auto로 판정된 행의 이름을 HUB_PROJECT로(ledger_project; 실시간 fold·워커 계상·
# 시간 원장 채우기가 지난다), (2) **세션 기록이 허브에 없는** 전사본 백필(scripts/backfill_tokens.py, collector/
# collect_tokens_local.py 사본)에서 이 이름으로 자동화를 추정(auto_frontend), (3) 기동 시 옛 행 병합(merge_worker_projects).
# 자동화 '판정'(세션 frontend)에는 쓰지 않는다 — state.ingest가 정하는 판정은 payload(claude -p / codex exec 부모 감지)와
# 앱 자동 생성 프롬프트 규칙뿐이다(워커는 MADISON_SUPPRESS=1이라 이벤트를 보내지 않는다 — report.sh·report.ps1 모두 조기 종료).
# 디렉터리 이름만으로 세션을 auto로 만들면 같은 이름의 사용자 프로젝트가 태스크 탭에서 숨고 auto TTL까지 받는다.
# 그래서 이름 개명도 판정이 auto일 때만 하고, 사람 세션이 낸 같은 이름은 그대로 둔다.
AUTO_PROJECTS = ("summarizer", "llm-cwd")

# 허브 워커(태스크 요약·리포트 등 llm.run) 사용량의 원장 project. 워커 cwd 이름(llm-cwd)으로 적으면 사용량 화면에
# 정체 모를 프로젝트가 따로 뜨는데, 이 사용량은 MADISON 자체 자동화이므로 madison 프로젝트의 auto 행으로 보여야 한다.
# cwd는 CLI가 git 컨텍스트를 못 보게 하는 비-git 격리용일 뿐 프로젝트 정체성이 아니다.
HUB_PROJECT = "madison"


def worker_dirs() -> tuple[str, ...]:
    """워커 디렉터리 이름 전부 — 과거(summarizer)·현행 기본값(llm-cwd)·LLM_CWD로 바꾼 이름. 이름 판정의 유일한 출처."""
    return tuple(dict.fromkeys(AUTO_PROJECTS + (CFG.llm_cwd.name,)))


def auto_frontend(project: str, frontend: str) -> str:
    """백필 전용: 허브 세션 기록이 없는 전사본의 워커 프로젝트는 frontend 미상이어도 'auto'로 —
    '자동화 제외' 필터가 걸리게. 실시간 경로의 '자동화인가'는 세션 판정(sessions.frontend)이 단일 기준이다
    (state.ingest가 payload·앱 자동 생성 프롬프트 규칙으로 정해 저장, fold가 ledger_key로 읽는다)."""
    return "auto" if project in worker_dirs() else frontend


def ledger_project(project: str, frontend: str) -> str:
    """원장에 적을 project — 자동화(auto)로 판정된 워커 디렉터리 이름은 HUB_PROJECT로. 사람 세션(cli·app 등)이
    낸 것이면 디렉터리 이름이 llm-cwd여도 사람 작업이라 그대로 둔다. 전사본 백필이 쓰고, merge_worker_projects가
    같은 규칙을 SQL로 적용한다."""
    return HUB_PROJECT if frontend == "auto" and project in worker_dirs() else project


_LEDGER_KEY = {"token_daily": "day", "token_hourly": "hour"}


def merge_worker_projects(c) -> int:
    """일·시간 원장의 (워커 디렉터리 이름, frontend='auto') 행을 HUB_PROJECT 행으로 합친다 — 같은 키가 이미 있으면
    열을 더하고 옛 행은 지운다. 멱등(합칠 행이 없으면 변화 없음). 호출부(db.migrate)의 트랜잭션 안에서 부른다.
    frontend가 auto가 아닌 행은 사람 작업이라 건드리지 않는다. 합친(지운) 행 수를 돌려준다."""
    names = tuple(n for n in worker_dirs() if n != HUB_PROJECT)
    if not names:
        return 0
    marks = ",".join("?" * len(names))
    n = 0
    per_table = {}
    for table, key in _LEDGER_KEY.items():
        if not c.execute(f"SELECT 1 FROM {table} WHERE frontend='auto' AND project IN ({marks}) LIMIT 1",
                         names).fetchone():
            continue
        c.execute(
            f"INSERT INTO {table} ({key}, device_id, agent, project, model, frontend, source,"
            f" input, output, cache_read, cache_write, thinking, turns)"
            f" SELECT {key}, device_id, agent, ?, model, frontend, source,"
            f" input, output, cache_read, cache_write, thinking, turns FROM {table}"
            f" WHERE frontend='auto' AND project IN ({marks})"
            f" ON CONFLICT({key}, device_id, agent, project, model, frontend, source) DO UPDATE SET"
            f" input=input+excluded.input, output=output+excluded.output,"
            f" cache_read=cache_read+excluded.cache_read, cache_write=cache_write+excluded.cache_write,"
            f" thinking=thinking+excluded.thinking, turns=turns+excluded.turns",
            (HUB_PROJECT,) + names)
        per_table[table] = c.execute(
            f"DELETE FROM {table} WHERE frontend='auto' AND project IN ({marks})", names).rowcount
        n += per_table[table]
    if n:
        log.info("원장 워커 project 병합: 일 원장 %d행·시간 원장 %d행 → %s", per_table.get("token_daily", 0),
                 per_table.get("token_hourly", 0), HUB_PROJECT)
    return n


def ledger_key(event_project, session) -> tuple[str, str]:
    """토큰 한 덩어리의 (project, frontend) — 실시간 fold와 일회성 채우기(backfill_hourly)가 함께 쓰는 경로.
    frontend는 이벤트 payload가 아니라 세션에 저장된 판정(sessions.frontend), project는 이벤트 값 우선·
    없으면 세션 값. session은 sessions 행(없으면 None). 이름 규칙은 세션 판정이 auto일 때의 개명(ledger_project)뿐 —
    원장 project를 정하는 함수는 ledger_project 하나다(워커 계상·백필·기동 시 병합과 공유)."""
    project = str(event_project or (session["project"] if session else "") or "")
    frontend = str((session["frontend"] if session else "") or "")
    return ledger_project(project, frontend), frontend


# 토큰 '규모' = input + cache_read + cache_write + output (thinking은 output의 부분집합이라 더하지 않는다).
# 우측 막대(에이전트·모델·서비스·프로젝트·기기별)의 정렬 기준 — Python 키와 SQL 식이 같은 정의.
SCALE_SQL = "SUM(input)+SUM(cache_read)+SUM(cache_write)+SUM(output)"


def scale_key(r) -> int:
    """sorted(key=...)용 — 규모 내림차순."""
    return -(r["input"] + r["cache_read"] + r["cache_write"] + r["output"])


def local_hour(ts_device, ts_hub=None) -> str:
    """UTC ISO → 허브 로컬 시각 키 'YYYY-MM-DDTHH' (일 키는 앞 10자). ts_device 우선(스풀 지연 도착 대비),
    파싱 실패 시 ts_hub, 둘 다 못 쓰면 지금. 원장 시간 키의 유일한 생성 규칙 — fold·워커·백필·조회가 공유."""
    for ts in (ts_device, ts_hub):
        if not ts:
            continue
        try:
            parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime(HOUR_FMT)
    return time.strftime(HOUR_FMT)


MAX_COUNT = 10 ** 15   # 한 카운터의 현실적 상한(1000조) — 넘으면 불량 필드. SQLite INTEGER는 2^63-1이라 그 너머는 OverflowError


def _counts(v) -> dict | None:
    """{kind: 정수} 검증·정수화. 불량 필드(음수·비수치·MAX_COUNT 초과·inf/NaN)는 0, 객체가 아니면 버림."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k in KINDS:
        n = v.get(k, 0)
        ok = isinstance(n, (int, float)) and not isinstance(n, bool) and 0 <= n <= MAX_COUNT
        out[k] = int(n) if ok else 0
    return out


@contextlib.contextmanager
def savepoint(c, name: str = "madison_sp"):
    """원자 구간 — 안에서 예외가 나면 이 구간의 쓰기만 롤백·해제하고 예외를 그대로 다시 던진다.
    바깥 트랜잭션(호출부의 db.tx, migrate의 IMMEDIATE)은 건드리지 않고, 정상이면 RELEASE로 바깥에 합류한다.
    (열린 트랜잭션이 없으면 SAVEPOINT가 트랜잭션을 시작하고 RELEASE가 커밋한다 — 호출부가 곧 커밋하는 경로뿐이다.)"""
    c.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        try:
            c.execute(f"ROLLBACK TO {name}")
            c.execute(f"RELEASE {name}")
        except Exception:
            log.exception("savepoint %s 롤백 실패", name)   # 원래 예외를 가리지 않는다
        raise
    else:
        c.execute(f"RELEASE {name}")


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


def _step(agent: str, old: dict, cum: dict, model: str) -> tuple[dict, dict]:
    """직전 기준점 old와 새 누적 cum → ({모델: 델타}, 새 기준점). fold와 일회성 채우기가 함께 쓰는 규칙."""
    if agent == "codex-cli":
        # 세션 전역 카운터 — 기준점은 _session 고정 키, 델타는 실려온 모델명으로 귀속
        model_key, new_total = next(iter(cum.items()))
        return {(model or model_key): _delta(old.get(_CODEX_KEY), new_total)}, {_CODEX_KEY: new_total}
    # Claude: 모델별 합은 한 전사본 안에서 단조증가 — 모델별 누적 카운터로 유효
    return {m: _delta(old.get(m), v) for m, v in cum.items()}, cum


def _spread(deltas: dict, count_turn: bool):
    """(모델, 델타, 턴수) 나열 — 델타가 전부 0이고 턴도 없으면 건너뛰고, 턴은 이벤트당 1회 첫 모델 행에만."""
    for m, d in deltas.items():
        if not any(d.values()) and not count_turn:
            continue
        yield m, d, 1 if count_turn else 0
        count_turn = False


def fold(c, device_id: int, agent: str, session_id: str, *, project: str, model: str,
         frontend: str, ts_device: str, ts_hub: str, tokens: dict, count_turn: bool):
    """이벤트의 tokens.cum을 델타로 바꿔 시간·일 원장에 가산하고 기준점을 갱신한다.
    project·frontend는 호출부가 ledger_key로 구해 넘긴다 — frontend는 이번 갱신 이후 세션에 저장된 판정.
    원장 가산 전부와 기준점(tokens_cum) 갱신은 한 SAVEPOINT다 — 중간에 실패하면 모두 되돌리고 예외를 올린다
    (state.ingest가 잡아 로그). 일부만 남으면 기준점이 그대로라 다음 턴에 이미 들어간 델타가 또 가산된다."""
    cum = _clean(tokens.get("cum"))
    if not cum:
        return
    with savepoint(c, "madison_fold"):
        row = c.execute("SELECT tokens_cum FROM sessions WHERE device_id=? AND agent=? AND session_id=?",
                        (device_id, agent, session_id)).fetchone()
        try:
            old = json.loads(row["tokens_cum"]) if row and row["tokens_cum"] else {}
        except (ValueError, TypeError):
            old = {}
        if not isinstance(old, dict):
            old = {}

        deltas, stored = _step(agent, old, cum, model)
        hour = local_hour(ts_device, ts_hub)
        for m, d, turns in _spread(deltas, count_turn):
            add_ledger(c, hour, device_id, agent, project, m, frontend, "events", d, turns=turns)
        c.execute("UPDATE sessions SET tokens_cum=? WHERE device_id=? AND agent=? AND session_id=?",
                  (json.dumps(stored, ensure_ascii=False), device_id, agent, session_id))


_LEDGER_UPSERT = (
    "INSERT INTO {table} ({key}, device_id, agent, project, model, frontend, source,"
    " input, output, cache_read, cache_write, thinking, turns)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT({key}, device_id, agent, project, model, frontend, source) DO UPDATE SET"
    " input=input+excluded.input, output=output+excluded.output,"
    " cache_read=cache_read+excluded.cache_read, cache_write=cache_write+excluded.cache_write,"
    " thinking=thinking+excluded.thinking, turns=turns+excluded.turns")


def _upsert(c, table: str, key: str, stamp, device_id, agent, project, model, frontend, source,
            counts: dict, turns: int):
    c.execute(_LEDGER_UPSERT.format(table=table, key=key),
              (stamp, device_id, agent, (project or "")[:120], (model or "")[:60], (frontend or "")[:20],
               source, counts["in"], counts["out"], counts["cr"], counts["cw"], counts["th"], turns))


def add_ledger(c, hour: str, device_id, agent, project, model, frontend, source, counts: dict, turns=0):
    """원장 쓰기 경로의 전부 — 로컬 시 키 'YYYY-MM-DDTHH' 하나를 받아 token_hourly(그 시)와
    token_daily(날 = hour[:10])에 같은 값을 가산 UPSERT한다. fold(이벤트)·llm._account_tokens(허브 워커)가 쓴다.
    (전사본 백필은 일 원장만 쓴다 — scripts/backfill_tokens.py 주석 참고.)"""
    _upsert(c, "token_daily", "day", hour[:10], device_id, agent, project, model, frontend, source, counts, turns)
    _upsert(c, "token_hourly", "hour", hour, device_id, agent, project, model, frontend, source, counts, turns)


def backfill_hourly(c):
    """일회성 시간 원장 채우기 — db.migrate가 token_hourly를 **새로 만들 때만** 부른다(같은 IMMEDIATE 트랜잭션).
    기존 일 원장은 이미 있으므로 시간 원장만 쓴다. 이 함수가 이벤트를 재생하는 유일한 코드다.
    전제: migrate는 db.conn()에서 ingest보다 먼저 돌고 BEGIN IMMEDIATE로 다른 migrate를 막으므로
    실시간 fold·다른 프로세스의 채우기와 이중 계상되지 않는다.
    재료 둘:
      1) 토큰이 실린 turn_done/session_end 이벤트를 세션별 id 순으로 재생 — 규칙은 fold와 같다(_step·_spread).
         frontend는 세션의 현재 판정(sessions.frontend), project는 이벤트 project 또는 세션 project(ledger_key).
         과거 이벤트 시점의 판정과 갈릴 수 있는 경계 사례는 일 원장과의 날짜별 합 비교로 확인한다.
      2) llm_runs.usage — 허브 워커(훅 억제라 이벤트 없음): started_at 시(실시간 _account_tokens와 같은 기준),
         frontend 'auto', project는 ledger_project(워커 cwd 이름, 'auto') = HUB_PROJECT, 기기는 허브 기기. 기기를 못 찾으면 건너뛴다.
    격리: 세션 하나(재료 1)·llm_runs 한 행(재료 2)의 처리는 각각 SAVEPOINT다. 손상된 한 단위가 실패하면 그 단위만
    롤백하고 건너뛴 뒤 건수를 경고로 남긴다 — 기동을 막지 않는다. 전체 트랜잭션 실패(락·디스크)는 그대로 올린다.
    경계: EVENT_RETENTION_DAYS>0으로 오래된 이벤트가 지워졌다면 세션의 남은 첫 이벤트가 전체 누적을 첫 델타로
    계상해 부풀 수 있다(기본값 0=무기한 보존에서는 해당 없음). llm_runs는 30일만 보존되므로(llm.py) 그 이전
    워커 사용량은 일 원장에만 있다 — 시간 원장을 읽는 화면은 최근 7일뿐이라 영향 없음."""
    from . import state   # 지연 import — state가 tokens를 import한다
    n_ev = n_run = 0
    skipped_sessions = skipped_runs = 0
    first_error = None
    sessions = {(r["device_id"], r["agent"], r["session_id"]): r
                for r in c.execute("SELECT device_id, agent, session_id, project, frontend FROM sessions")}
    # 세션별로 이벤트를 읽는다 — 읽기 커서가 열린 채 ROLLBACK TO 하지 않으려고 키 목록을 먼저 확정한다.
    keys = [tuple(r) for r in c.execute(
        "SELECT DISTINCT device_id, agent, session_id FROM events"
        " WHERE event IN ('turn_done','session_end') AND payload LIKE '%\"tokens\"%'"
        " ORDER BY device_id, agent, session_id")]
    for key in keys:
        rows = c.execute(
            "SELECT event, ts_device, ts_hub, project, payload FROM events"
            " WHERE device_id=? AND agent=? AND session_id=? AND event IN ('turn_done','session_end')"
            " AND payload LIKE '%\"tokens\"%' ORDER BY id", key).fetchall()
        try:
            with savepoint(c, "madison_fill_session"):
                n_ev += _fill_session(c, key, rows, sessions.get(key), state)
        except Exception as e:
            skipped_sessions += 1
            first_error = first_error or f"세션 {key}: {type(e).__name__}: {e}"

    runs = c.execute("SELECT provider, started_at, usage FROM llm_runs WHERE usage IS NOT NULL").fetchall()
    hub = hub_device_id(c) if runs else None
    if runs and hub is None:
        log.warning("시간 원장 채우기: 허브 기기를 못 찾아 워커 호출(llm_runs %d건) 건너뜀", len(runs))
    for r in (runs if hub is not None else ()):
        try:
            um = _clean(json.loads(r["usage"]))
        except ValueError:
            continue   # usage가 JSON이 아님 — 손상이 아니라 '없음' 취급(예전과 같다)
        ag = "codex-cli" if r["provider"] == "codex" else "claude-code"
        try:
            with savepoint(c, "madison_fill_run"):
                hour = local_hour(r["started_at"])
                rows = [(m, d) for m, d in um.items() if any(d.values())]
                for m, d in rows:
                    _upsert(c, "token_hourly", "hour", hour, hub, ag, ledger_project(CFG.llm_cwd.name, "auto"),
                            m, "auto", "events", d, 0)
            n_run += len(rows)
        except Exception as e:
            skipped_runs += 1
            first_error = first_error or f"워커 호출 {r['started_at']}: {type(e).__name__}: {e}"
    if skipped_sessions or skipped_runs:
        log.warning("시간 원장 채우기: 손상된 단위 건너뜀 — 세션 %d건·워커 호출 %d건 (첫 오류: %s)",
                    skipped_sessions, skipped_runs, first_error)
    log.info("시간 원장 채우기: 이벤트 %d행·워커 %d행 가산", n_ev, n_run)


def _fill_session(c, key, rows, session, state) -> int:
    """세션 하나의 이벤트(id 순)를 재생해 시간 원장에 가산 — 가산한 행 수."""
    n = 0
    old: dict = {}
    last_model = ""
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("tokens"), dict):
            continue
        cum = _clean(payload["tokens"].get("cum"))
        if not cum:
            continue
        model = state._real_model(payload) or last_model
        last_model = model
        deltas, old = _step(key[1], old, cum, model)
        proj, fe = ledger_key(r["project"], session)
        hour = local_hour(r["ts_device"], r["ts_hub"])
        for m, d, turns in _spread(deltas, r["event"] == "turn_done"):
            _upsert(c, "token_hourly", "hour", hour, key[0], key[1], proj, m, fe, "events", d, turns)
            n += 1
    return n


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


def _project_service_rows(reg, raw_rows) -> tuple[list, list]:
    """원 프로젝트명 집계 행({project, input, …, turns}) → (프로젝트별, 서비스별).
    이름은 태스크·리포트와 같은 처리를 거친다 — 레지스트리 매핑 + 정리 버킷(_pretty)."""
    per_project: dict = {}
    per_service: dict = {}
    for r in raw_rows:
        pname = _pretty(r["project"])
        svc = reg.service(r["project"]) if r["project"] else ""
        if svc == (r["project"] or ""):
            svc = pname   # 미매핑 프로젝트는 정리된 이름 그대로가 서비스
        for bucket, key, name in ((per_project, "project", pname), (per_service, "service", svc)):
            acc = bucket.setdefault(name, {key: name, **{COLS[k]: 0 for k in KINDS}, "turns": 0})
            for k in KINDS:
                acc[COLS[k]] += r[COLS[k]] or 0
            acc["turns"] += r["turns"] or 0
    return sorted(per_project.values(), key=scale_key), sorted(per_service.values(), key=scale_key)


def summary(c, reg, *, days: int = 30, agent: str = "", device: str = "", service: str = "",
            model: str = "", human: bool = False) -> dict:
    """토큰 사용량 집계 — 시계열 + 기기/프로젝트/서비스/모델/에이전트/세션별 내역.
    짧은 범위(≤7일)는 일 원장을 달력 날짜로 자르면 '최근 24시간'이 사실상 '오늘'이 되므로,
    시계열만이 아니라 내역·합계까지 시간 원장(token_hourly)의 롤링 창(window)으로 준다 —
    차트와 표가 항상 같은 창을 본다. 긴 범위만 일 원장(token_daily)을 쓴다. 두 원장은 같은 fold가 쓴다."""
    if 0 < days <= 7:
        parts = window(c, reg, hours_back=24 * int(days), agent=agent, device=device,
                       service=service, model=model, human=human)
    else:
        parts = _daily_summary(c, reg, days=days, agent=agent, device=device,
                               service=service, model=model, human=human)
    return {**parts,
            "by_session": _by_session(c, reg, days=days, agent=agent, device=device,
                                      service=service, human=human)}


def _where(reg, alias: str, *, agent: str, device: str, service: str, model: str, human: bool,
           period: str = "", period_args=()) -> tuple[str, list]:
    """원장 조회 WHERE의 유일한 빌더 — 일 원장(_daily_summary)과 시간 원장(window)이 함께 쓴다.
    둘의 차이는 테이블 별칭(alias)과 기간 조건(period + period_args)뿐이다. 필터: agent, 쉼표 다중 device,
    model, human(= 자동화 제외, frontend != 'auto'), service."""
    where, args = ["1=1"], []
    if period:
        where.append(period); args.extend(period_args)
    t = alias
    if agent:
        where.append(f"{t}.agent=?"); args.append(agent)
    if device:
        names = [d for d in device.split(",") if d]   # 쉼표 구분 다중선택
        where.append(f"{t}.device_id IN (SELECT id FROM devices WHERE name IN ({','.join('?' * len(names))}))")
        args.extend(names)
    if model:
        where.append(f"{t}.model=?"); args.append(model)
    if human:
        where.append(f"{t}.frontend != 'auto'")
    if service:
        projs = _service_projects(reg, service)
        where.append(f"{t}.project IN ({','.join('?' * len(projs))})"); args.extend(projs)
    return " AND ".join(where), args


def _breakdowns(c, reg, table: str, pred: str, args: list) -> dict:
    """같은 조건(pred, 테이블 별칭 t)의 내역·합계 — by_device/project/service/model/agent/total. 규모(SCALE_SQL) 내림차순."""
    def rows(group_sql, key):
        # ORDER BY는 별칭이 아니라 SUM으로 — 바깥 표현식의 bare input/output은 원 컬럼으로 풀린다
        return [dict(r) for r in c.execute(
            f"SELECT {group_sql} {key}, {_SUM} FROM {table} t WHERE {pred}"
            f" GROUP BY {group_sql} ORDER BY {SCALE_SQL} DESC", args)]

    per_project, per_service = _project_service_rows(reg, rows("t.project", "project"))
    per_device = [dict(r) for r in c.execute(
        f"SELECT d.name device, {_SUM} FROM {table} t JOIN devices d ON d.id=t.device_id"
        f" WHERE {pred} GROUP BY d.name ORDER BY {SCALE_SQL} DESC", args)]
    total_row = c.execute(f"SELECT {_SUM} FROM {table} t WHERE {pred}", args).fetchone()
    return {
        "by_device": per_device,
        "by_project": per_project,
        "by_service": per_service,
        "by_model": rows("t.model", "model"),
        "by_agent": rows("t.agent", "agent"),
        "total": {k: (total_row[k] or 0) for k in (*COLS.values(), "turns")},
    }


def _daily_summary(c, reg, *, days: int, agent: str, device: str, service: str,
                   model: str, human: bool) -> dict:
    """긴 범위 — token_daily(일 원장) 집계. 백필분도 여기에만 잡힌다."""
    period, pargs = "", []
    if days and days > 0:
        period, pargs = "t.day >= date('now','localtime', ?)", [f"-{int(days) - 1} days"]
    pred, args = _where(reg, "t", agent=agent, device=device, service=service, model=model, human=human,
                        period=period, period_args=pargs)
    day_rows = [dict(r) for r in c.execute(
        f"SELECT t.day day, {_SUM} FROM token_daily t WHERE {pred} GROUP BY t.day ORDER BY t.day", args)]
    series_by_agent: dict = {}   # 에이전트별 일 시계열 — 같은 필터, 합하면 day_rows와 같다
    for r in c.execute(
            f"SELECT t.agent agent, t.day day, {_SUM} FROM token_daily t WHERE {pred}"
            f" GROUP BY t.agent, t.day ORDER BY t.day", args):
        r = dict(r)
        series_by_agent.setdefault(r.pop("agent"), {"days": [], "hours": None})["days"].append(r)
    return {"days": day_rows, "hours": None, "series_by_agent": series_by_agent,
            **_breakdowns(c, reg, "token_daily", pred, args)}


def window(c, reg, *, hours_back: int = 24, agent: str = "", device: str = "",
           service: str = "", model: str = "", human: bool = False) -> dict:
    """최근 N시간 롤링 창 — token_hourly(시간 원장)를 SQL로 읽는다. 조회만 한다: 이벤트·llm_runs를
    다시 재생하지 않는다(워커 호출도 llm._account_tokens가 쓰기 시점에 이 원장에 넣는다).
    필터는 일 원장 조회와 같은 _where 빌더. 자동화 여부(human)는 원장 행의 frontend —
    쓰기 시점에 세션 판정(ledger_key)으로 정해져 일 원장과 같은 값이다.
    반환: summary와 같은 모양의 {days, hours, by_*, total} — hours는 시간 오름차순
    [{ts:'YYYY-MM-DDTHH', input,…, turns}] 빈 시간대 0 채움, days는 그 버킷의 날짜별 합.
    series_by_agent={agent:{days,hours}}는 같은 시계열을 에이전트별로 나눈 것(합하면 days/hours).
    전사본 백필분은 일 원장 전용이라 여기엔 안 잡힌다."""
    now = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    order = [(now - timedelta(hours=h)).strftime(HOUR_FMT) for h in range(hours_back - 1, -1, -1)]
    zero = {COLS[k]: 0 for k in KINDS}
    pred, args = _where(reg, "t", agent=agent, device=device, service=service, model=model, human=human,
                        period="t.hour BETWEEN ? AND ?", period_args=(order[0], order[-1]))

    def series(rows) -> dict:
        """시간 행 → 창 전체를 0으로 채운 {hour: 버킷}."""
        buckets = {k: {"ts": k, **zero, "turns": 0} for k in order}
        for r in rows:
            b = buckets[r["hour"]]
            for col in (*zero, "turns"):
                b[col] = r[col] or 0
        return buckets

    def to_days(bks) -> list:
        rows: dict = {}
        for k in order:
            acc = rows.setdefault(k[:10], {"day": k[:10], **zero, "turns": 0})
            for col in (*zero, "turns"):
                acc[col] += bks[k][col]
        return list(rows.values())

    buckets = series(c.execute(
        f"SELECT t.hour hour, {_SUM} FROM token_hourly t WHERE {pred} GROUP BY t.hour", args))
    per_agent: dict = {}
    for r in c.execute(f"SELECT t.agent agent, t.hour hour, {_SUM} FROM token_hourly t WHERE {pred}"
                       " GROUP BY t.agent, t.hour ORDER BY t.agent", args):
        per_agent.setdefault(r["agent"], []).append(r)
    agent_buckets = {ag: series(rows) for ag, rows in per_agent.items()}
    return {
        "days": to_days(buckets),
        "hours": [buckets[k] for k in order],
        "series_by_agent": {ag: {"days": to_days(ab), "hours": [ab[k] for k in order]}
                            for ag, ab in agent_buckets.items()},
        **_breakdowns(c, reg, "token_hourly", pred, args),
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
        names = [d for d in device.split(",") if d]
        where.append(f"d.name IN ({','.join('?' * len(names))})"); args.extend(names)
    if human:
        where.append("COALESCE(s.frontend,'') != 'auto'")
    if service:
        projs = _service_projects(reg, service)
        where.append(f"s.project IN ({','.join('?' * len(projs))})"); args.extend(projs)
    out = []
    for r in c.execute(
            "SELECT d.name device, s.agent, s.session_id, s.project, s.model, s.tokens_cum,"
            " s.started_at, s.last_seen_hub, s.frontend FROM sessions s JOIN devices d ON d.id=s.device_id"
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
        # 원장(by_project)과 같은 이름 — 세션 판정이 auto인 워커 디렉터리 이름은 HUB_PROJECT로
        proj = ledger_project(r["project"] or "", r["frontend"] or "")
        out.append({"device": r["device"], "agent": r["agent"], "session_id": r["session_id"],
                    "project": proj, "service": reg.service(proj),
                    "model": r["model"] or "", "started_at": r["started_at"],
                    "last_seen": r["last_seen_hub"], **{COLS[k]: tot[k] for k in KINDS}})
    out.sort(key=scale_key)
    return out[:limit]
