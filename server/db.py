"""SQLite 연결과 스키마. 트래픽이 작으므로 단일 커넥션 + 락으로 직렬화한다."""
import sqlite3
import threading

from .config import CFG

_lock = threading.Lock()
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
  payload TEXT
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
  last_prompt TEXT, last_summary TEXT, approval_msg TEXT, current_tool TEXT,
  turns INTEGER DEFAULT 0,
  started_at TEXT, last_seen_hub TEXT, ended_at TEXT, end_reason TEXT,
  PRIMARY KEY (device_id, agent, session_id)
);
CREATE TABLE IF NOT EXISTS handoffs (
  id INTEGER PRIMARY KEY,
  from_device INTEGER, to_device INTEGER NOT NULL,
  repo TEXT NOT NULL, origin TEXT, branch TEXT, doc_path TEXT,
  summary TEXT,
  status TEXT DEFAULT 'pending',
  created_at TEXT, delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS summary_cache (
  phash TEXT PRIMARY KEY,             -- sha256(원문) — 동일 템플릿 프롬프트의 요약 재사용
  summary TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,               -- 예: llm.report.model — 대시보드 설정 탭에서 관리, .env 기본값을 덮음
  value TEXT
);
CREATE TABLE IF NOT EXISTS reports (
  range TEXT NOT NULL,          -- 'day' | 'week'
  day TEXT NOT NULL,            -- 기준 로컬 날짜 'YYYY-MM-DD'
  markdown TEXT,
  generated_at TEXT,
  PRIMARY KEY (range, day)
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        CFG.db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(CFG.db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(SCHEMA)
        # 마이그레이션 (있으면 무시)
        for ddl in ("ALTER TABLE sessions ADD COLUMN task_summary TEXT",
                    "ALTER TABLE sessions ADD COLUMN model TEXT",
                    "ALTER TABLE sessions ADD COLUMN effort TEXT",
                    "ALTER TABLE sessions ADD COLUMN frontend TEXT"):
            try:
                _conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        _conn.commit()
    return _conn


def tx():
    """with db.tx() as c: ... — 락 잡고 커밋까지."""
    class _Tx:
        def __enter__(self):
            _lock.acquire()
            return conn()

        def __exit__(self, exc_type, *a):
            if exc_type is None:
                conn().commit()
            else:
                conn().rollback()
            _lock.release()
            return False
    return _Tx()
