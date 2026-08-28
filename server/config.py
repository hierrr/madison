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
        # 긴 세션 압축(digest)은 판단이 아니라 추출이라 리포트 모델보다 싼 모델로 충분하다
        self.digest_model = get("DIGEST_MODEL", "claude-sonnet-5")
        # LLM 호출 타임아웃(초) — 사이트별. 리포트 모델(opus·xhigh)은 호출당 1~4분이 실측이라 넉넉히.
        self._llm_timeouts = {"summary": int(get("LLM_TIMEOUT_SUMMARY", "90")),
                              "digest": int(get("LLM_TIMEOUT_DIGEST", "300")),
                              "report": int(get("LLM_TIMEOUT_REPORT", "900"))}
        # 허브 LLM 호출의 작업 디렉터리 — 저장소 밖·비-git(훅이 새어도 project가 자명하고 git 컨텍스트가 없다)
        llm_cwd = Path(get("LLM_CWD", str(Path.home() / ".madison" / "llm-cwd")))
        self.llm_cwd = llm_cwd if llm_cwd.is_absolute() else REPO_ROOT / llm_cwd
        # 자동 갱신 시각(크론 5필드, 로컬 시간) — 그 시각에 바뀐 것이 있을 때만 생성한다. 재시작은 생성을 유발하지 않는다.
        # 리포트가 아예 없는 새 날·주·월은 크론과 무관하게 바로 만든다.
        self.report_daily_cron = get("REPORT_DAILY_CRON", "0 * * * *")
        self.report_weekly_cron = get("REPORT_WEEKLY_CRON", "15 */4 * * *")
        self.report_monthly_cron = get("REPORT_MONTHLY_CRON", "45 */8 * * *")
        # 리포트 제외 프로젝트(콤마 구분) — 해당 프로젝트 섹션은 물론, 다른 프로젝트 로그에서
        # 그 이름이 언급된 줄까지 리포트 재료에서 뺀다 (언급이 요약에 되살아나는 재발 방지)
        self.report_exclude_projects = tuple(
            x.strip() for x in get("REPORT_EXCLUDE_PROJECTS").split(",") if x.strip())
        # 프로젝트 → 리포트 최상위 서비스명 매핑("proj=서비스, proj2=서비스").
        # 최상위 묶음을 LLM 추측이 아니라 설정으로 고정한다 — 매핑이 없으면 프로젝트명을 그대로 쓴다.
        # (같은 접두어라고 임의로 합치던 동작 때문에 별개 서비스가 흡수되는 문제를 막기 위함)
        self.report_service_map = {}
        for item in get("REPORT_SERVICE_MAP").split(","):
            proj, _, svc = item.strip().partition("=")
            if proj.strip() and svc.strip():
                self.report_service_map[proj.strip()] = svc.strip()
        # 잡동사니 디렉터리(콤마 구분) — 매핑은 유지하되 '약한 기본값'으로 시드: 내용이 다른 서비스면 모델이 번복한다
        self.report_weak_projects = tuple(
            x.strip() for x in get("REPORT_WEAK_PROJECTS").split(",") if x.strip())
        # 자기 프로젝트가 없는 서비스("서비스=식별 단서; 서비스2") — 다른 서비스의 저장소(모노리포 등) 안에서
        # 작업되는 경우. 세미콜론 구분(단서에 콤마를 쓸 수 있게). 프롬프트의 '알려진 서비스' 목록에 단서와
        # 함께 실려, 모델이 그 서비스 대상 항목을 로그가 붙은 서비스가 아니라 이 이름 밑으로 옮길 수 있게 한다.
        self.report_known_services = {}
        for item in get("REPORT_KNOWN_SERVICES").split(";"):
            name, _, hint = item.strip().partition("=")
            if name.strip():
                self.report_known_services[name.strip()] = hint.strip()
        # "home:1.2.3.4, office:5.6.7.8" → {ip, ...} (이름은 로깅용)
        self.ip_allowlist = {}
        for item in get("IP_ALLOWLIST").split(","):
            item = item.strip()
            if not item:
                continue
            name, _, ip = item.rpartition(":")
            self.ip_allowlist[ip.strip()] = name.strip() or "unnamed"

        # 현황 KPI의 구독 한도(Claude·Codex) — 허브가 직접 수집. USAGE=0이면 끔
        self.usage_enabled = get("USAGE", "1") == "1"
        self.usage_poll_sec = max(60, int(get("USAGE_POLL_SEC", "180")))

    def llm_timeout(self, site: str) -> int:
        return self._llm_timeouts.get(site, self._llm_timeouts["report"])


CFG = Config()
