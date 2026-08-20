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


def _find_codex() -> str:
    """PATH → nvm 설치본 순으로 codex를 찾는다 (launchd의 최소 PATH 대비)."""
    import glob
    import shutil
    found = shutil.which("codex")
    if found:
        return found
    cands = glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/codex"))
    if cands:
        return max(cands, key=lambda p: Path(p).stat().st_mtime)  # 최근 설치본
    return "codex"


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
        self.retention_days = int(get("EVENT_RETENTION_DAYS", "0"))  # 0 = 무기한 보존
        # 태스크 한 줄 요약 — 허브가 haiku로 중앙 요약 (훅 예산과 무관, 기기 부담 0)
        self.task_summary_enabled = get("TASK_SUMMARY", "1") == "1"
        self.task_summary_model = get("TASK_SUMMARY_MODEL", "claude-haiku-4-5-20251001")
        self.task_summary_bin = get("TASK_SUMMARY_BIN", str(Path.home() / ".local/bin/claude"))
        # codex CLI 경로 — launchd PATH엔 nvm이 없어 기동 시 동적 탐색(버전 경로 하드코딩 금지).
        # .env CODEX_BIN 또는 설정 탭(llm.codex_bin)이 있으면 그 값을 쓴다.
        self.codex_bin = get("CODEX_BIN") or _find_codex()
        # 업무 리포트 — 프로젝트별 지시/완료를 허브 LLM으로 업무일지 마크다운 요약
        self.report_enabled = get("REPORT", "1") == "1"
        self.report_model = get("REPORT_MODEL", "claude-sonnet-5")   # 요약 품질 위해 haiku보다 상위
        self.report_daily_min = int(get("REPORT_DAILY_MIN", "60"))       # 일일 리포트 자동 갱신 주기(분)
        self.report_weekly_min = int(get("REPORT_WEEKLY_MIN", "1440"))   # 주간 리포트 자동 갱신 주기(분) — 부팅 직후 + 주기
        self.report_monthly_min = int(get("REPORT_MONTHLY_MIN", "1440"))  # 월간 리포트 자동 갱신 주기(분)
        # 리포트 제외 프로젝트(콤마 구분) — 해당 프로젝트 섹션은 물론, 다른 프로젝트 로그에서
        # 그 이름이 언급된 줄까지 리포트 재료에서 뺀다 (언급이 요약에 되살아나는 재발 방지)
        self.report_exclude_projects = tuple(
            x.strip() for x in get("REPORT_EXCLUDE_PROJECTS").split(",") if x.strip())
        # "home:1.2.3.4, office:5.6.7.8" → {ip, ...} (이름은 로깅용)
        self.ip_allowlist = {}
        for item in get("IP_ALLOWLIST").split(","):
            item = item.strip()
            if not item:
                continue
            name, _, ip = item.rpartition(":")
            self.ip_allowlist[ip.strip()] = name.strip() or "unnamed"


CFG = Config()
