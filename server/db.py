"""SQLite 연결과 스키마. 트래픽이 작으므로 단일 커넥션 + 락으로 직렬화한다."""
import contextlib
import sqlite3
import threading

from .config import CFG

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  token_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  revoked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES devices(id),
  agent TEXT NOT NULL,
  session_id TEXT NOT NULL,
  event_id TEXT UNIQUE,
  event TEXT NOT NULL,
  ts_device TEXT NOT NULL,
  ts_hub TEXT NOT NULL,
  project TEXT, branch TEXT,
  payload TEXT,
  origin TEXT,            -- git remote URL (개명·동명 저장소 구분 키)
  subdir TEXT             -- 저장소 안 상대 경로 (모노리포 하위 서비스 식별)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(device_id, session_id, id);
CREATE INDEX IF NOT EXISTS idx_events_hub_ts ON events(ts_hub);
CREATE TABLE IF NOT EXISTS sessions (
  device_id INTEGER NOT NULL,
  agent TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project TEXT, branch TEXT,
  state TEXT,
  state_ts TEXT,          -- 상태를 정한 이벤트의 ts_device (지연 도착 가드)
  state_since TEXT,       -- 상태가 바뀐 허브 시각 ("n분째" 표시용)
  last_prompt TEXT, last_summary TEXT, task_summary TEXT,
  approval_msg TEXT, current_tool TEXT,
  model TEXT, effort TEXT, frontend TEXT,
  turns INTEGER DEFAULT 0,
  started_at TEXT, last_seen_hub TEXT, ended_at TEXT, end_reason TEXT,
  collection_mode TEXT,
  summary_source TEXT,    -- 'llm' | 'fallback' (실패해 원문 앞부분으로 채운 것 — 재시도 대상)
  summary_tried_at TEXT,  -- 마지막 요약 시도 시각 (재시도 간격용)
  tokens_cum TEXT,        -- 마지막 누적 토큰 JSON {model: {in,out,cr,cw,th}} — 델타 기준점
  PRIMARY KEY (device_id, agent, session_id)
);
CREATE TABLE IF NOT EXISTS handoffs (
  id INTEGER PRIMARY KEY,
  from_device INTEGER, to_device INTEGER NOT NULL,
  repo TEXT NOT NULL, origin TEXT, branch TEXT, doc_path TEXT,
  summary TEXT,
  doc TEXT,               -- 핸드오프 문서 본문 (허브 운반 — 64KB 상한은 API에서)
  patches TEXT,           -- JSON [{repo, base, diff}] (1MB 상한은 API에서)
  status TEXT DEFAULT 'pending',
  created_at TEXT, delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS summary_cache (
  phash TEXT PRIMARY KEY,             -- sha256(템플릿 버전 + 원문) — 동일 템플릿 프롬프트의 요약 재사용
  summary TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,               -- 예: llm.report.model — 대시보드 설정 탭에서 관리, .env 기본값을 덮음
  value TEXT
);
CREATE TABLE IF NOT EXISTS reports (
  range TEXT NOT NULL,          -- 'day' | 'week' | 'month'
  day TEXT NOT NULL,            -- 기준 로컬 날짜 'YYYY-MM-DD' (week=월요일, month=1일)
  markdown TEXT,
  generated_at TEXT,
  model TEXT, effort TEXT,      -- 이 판을 만든 모델
  prompt_version TEXT,          -- 프롬프트 판 — "어느 판으로 만든 리포트인지"
  failed_at TEXT, fail_reason TEXT,   -- 마지막 생성 실패 (성공하면 NULL로)
  stale_at TEXT,                -- 사람의 교정(재라벨) 뒤 재생성 대기 표시
  PRIMARY KEY (range, day)
);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL, session_key TEXT, device TEXT, project TEXT,
  before_service TEXT, after_service TEXT, reason TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS services (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,          -- 리포트 최상위 불릿 표기 그대로
  kind TEXT NOT NULL DEFAULT 'product',      -- product | tool | ops | bucket
  status TEXT NOT NULL DEFAULT 'confirmed',  -- proposed | confirmed | rejected | merged
  merged_into INTEGER,
  description TEXT,                   -- 무엇인지 한 줄
  cues TEXT,                          -- 식별 단서 (JSON 배열)
  evidence TEXT,                      -- 제안 근거 (JSON [{day, session, quote}])
  source TEXT,                        -- env | human | model
  created_at TEXT, proposed_at TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS project_map (
  project TEXT PRIMARY KEY,           -- 수집된 프로젝트(디렉터리) 이름
  service_id INTEGER NOT NULL REFERENCES services(id),
  strength TEXT NOT NULL DEFAULT 'strong',   -- strong: 제품 저장소 | weak: 잡동사니 디렉터리(내용이 다르면 모델이 번복)
  source TEXT
);
CREATE TABLE IF NOT EXISTS report_assignments (
  range TEXT NOT NULL, day TEXT NOT NULL,
  session_key TEXT NOT NULL,          -- 프롬프트의 세션 표기 (S1, S2 …)
  device TEXT, session_id TEXT, project TEXT,
  service TEXT, task TEXT, evidence TEXT,
  created_at TEXT,
  PRIMARY KEY (range, day, session_key)
);
CREATE TABLE IF NOT EXISTS report_versions (
  id INTEGER PRIMARY KEY,
  range TEXT NOT NULL, day TEXT NOT NULL,
  generated_at TEXT, markdown TEXT, model TEXT, prompt_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_report_versions ON report_versions(range, day, id);
CREATE TABLE IF NOT EXISTS report_jobs (
  range TEXT NOT NULL, day TEXT NOT NULL,
  pid INTEGER, started_at TEXT,      -- 생성 워커 프로세스 — 허브 재시작과 무관하게 진행
  PRIMARY KEY (range, day)
);
CREATE TABLE IF NOT EXISTS llm_runs (
  id INTEGER PRIMARY KEY,
  site TEXT, provider TEXT, model TEXT, effort TEXT,
  prompt_sha TEXT, prompt_chars INTEGER, output_chars INTEGER,
  ok INTEGER, returncode INTEGER, duration_s REAL,
  started_at TEXT, error TEXT, ref TEXT, structured INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_runs_started ON llm_runs(started_at);
CREATE TABLE IF NOT EXISTS usage_history (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,               -- 허브 UTC ISO
  provider TEXT NOT NULL,         -- 'claude' | 'codex'
  win TEXT NOT NULL,              -- 창 제목 그대로: '5h', '7d all models', '7d <model>' …
  pct INTEGER NOT NULL,
  resets_at REAL                  -- epoch (없으면 NULL)
);
CREATE INDEX IF NOT EXISTS idx_usage_history ON usage_history(provider, win, ts);
CREATE TABLE IF NOT EXISTS token_daily (
  day TEXT NOT NULL,              -- ts_device 기준 로컬 날짜 'YYYY-MM-DD'
  device_id INTEGER NOT NULL,
  agent TEXT NOT NULL,            -- 'claude-code' | 'codex-cli'
  project TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  frontend TEXT NOT NULL DEFAULT '',   -- ''|cli|app|ide|auto — 자동화도 집계하되 구분(쿼터는 소모)
  source TEXT NOT NULL DEFAULT 'events',   -- 'events' | 'backfill'
  input INTEGER NOT NULL DEFAULT 0,
  output INTEGER NOT NULL DEFAULT 0,       -- thinking 포함
  cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0,
  thinking INTEGER NOT NULL DEFAULT 0,
  turns INTEGER NOT NULL DEFAULT 0,        -- 기여한 turn_done 수
  PRIMARY KEY (day, device_id, agent, project, model, frontend, source)
);
CREATE INDEX IF NOT EXISTS idx_token_daily_day ON token_daily(day);
CREATE TABLE IF NOT EXISTS token_scan_state (
  path TEXT PRIMARY KEY,          -- 전사본 파일 절대경로
  cum TEXT,                       -- 마지막 누적 {model:{in,out,cr,cw,th}} JSON — 델타 기준점
  size INTEGER, mtime REAL,       -- 변화 감지용 (동일하면 파싱 생략)
  updated_at TEXT
);
"""

# 기존 DB에 열 추가·삭제 (이미 반영됐으면 무시)
MIGRATIONS = (
    "ALTER TABLE sessions ADD COLUMN task_summary TEXT",
    "ALTER TABLE sessions ADD COLUMN model TEXT",
    "ALTER TABLE sessions ADD COLUMN effort TEXT",
    "ALTER TABLE sessions ADD COLUMN frontend TEXT",
    "ALTER TABLE sessions ADD COLUMN collection_mode TEXT",
    "ALTER TABLE sessions ADD COLUMN summary_source TEXT",
    "ALTER TABLE sessions ADD COLUMN summary_tried_at TEXT",
    "ALTER TABLE handoffs ADD COLUMN doc TEXT",
    "ALTER TABLE handoffs ADD COLUMN patches TEXT",
    "ALTER TABLE reports ADD COLUMN model TEXT",
    "ALTER TABLE reports ADD COLUMN effort TEXT",
    "ALTER TABLE reports ADD COLUMN prompt_version TEXT",
    "ALTER TABLE reports ADD COLUMN failed_at TEXT",
    "ALTER TABLE reports ADD COLUMN fail_reason TEXT",
    "ALTER TABLE reports ADD COLUMN stale_at TEXT",
    "ALTER TABLE events ADD COLUMN origin TEXT",
    "ALTER TABLE events ADD COLUMN subdir TEXT",
    "ALTER TABLE reports DROP COLUMN pinned",   # 고정 기능 제거 (2026-08-31)
    "ALTER TABLE sessions ADD COLUMN tokens_cum TEXT",
)


def migrate(c: sqlite3.Connection):
    """스키마 생성 + 열 추가 마이그레이션. 테스트의 :memory: 커넥션에도 그대로 쓴다."""
    c.executescript(SCHEMA)
    for ddl in MIGRATIONS:
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    c.commit()


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        CFG.db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(CFG.db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        migrate(_conn)
    return _conn


@contextlib.contextmanager
def tx():
    """with db.tx() as c: ... — 락 잡고 커밋까지. 재진입 가능(RLock) — 같은 스레드의 중첩 tx는 바깥에서 커밋."""
    _lock.acquire()
    try:
        c = conn()
        yield c
        c.commit()
    except BaseException:
        conn().rollback()
        raise
    finally:
        _lock.release()
