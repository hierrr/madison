"""MADISON 허브 설정 — 저장소 루트의 .env를 읽는다 (외부 의존성 없음)."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split("#", 1)[0].strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


class Config:
    def __init__(self):
        env = _load_env(REPO_ROOT / ".env")
        get = lambda k, d="": env.get(k) or os.environ.get(f"MADISON_{k}") or d
        self.host = get("HOST", "127.0.0.1")
        self.port = int(get("PORT", "8787"))
        db_path = Path(get("DB_PATH", "data/madison.db"))
        self.db_path = db_path if db_path.is_absolute() else REPO_ROOT / db_path
        self.dashboard_host = get("DASHBOARD_HOST", "madison.example.com")
        self.api_host = get("API_HOST", "madison-api.example.com")
        self.cf_team_domain = get("CF_ACCESS_TEAM_DOMAIN")
        self.cf_aud = get("CF_ACCESS_AUD")
        self.enroll_secret = get("ENROLL_SECRET")
        self.ttl_stale_min = int(get("TTL_STALE_MIN", "15"))
        self.device_online_min = int(get("DEVICE_ONLINE_MIN", "10"))
        self.ended_hide_hours = int(get("ENDED_HIDE_HOURS", "24"))
        self.retention_days = int(get("EVENT_RETENTION_DAYS", "90"))
        # 태스크 한 줄 요약 — 허브가 haiku로 중앙 요약 (훅 예산과 무관, 기기 부담 0)
        self.task_summary_enabled = get("TASK_SUMMARY", "1") == "1"
        self.task_summary_model = get("TASK_SUMMARY_MODEL", "claude-haiku-4-5-20251001")
        self.task_summary_bin = get("TASK_SUMMARY_BIN", str(Path.home() / ".local/bin/claude"))
        # "home:1.2.3.4, office:5.6.7.8" → {ip, ...} (이름은 로깅용)
        self.ip_allowlist = {}
        for item in get("IP_ALLOWLIST").split(","):
            item = item.strip()
            if not item:
                continue
            name, _, ip = item.rpartition(":")
            self.ip_allowlist[ip.strip()] = name.strip() or "unnamed"


CFG = Config()
