"""Claude Code·Codex 구독 한도 — 허브가 직접 수집한다(외부 도구·파일 의존 없음).

슬롯: 허브는 Mac mini의 이름 붙은 폴더만 수집 대상으로 읽는다 — Claude `~/.claude-<이름>`, Codex `~/.codex-<이름>`.
기본 폴더(`~/.claude`, `~/.codex`)는 사람이 작업할 때 쓰는 로그인이라 허브 표시와 분리한다. 설정 env는 없고 폴더 자동
탐색(`_discover`)이 유일한 메커니즘이다 — 수집 주기마다 다시 탐색한다. 슬롯 조건: Claude는 폴더 안 `.claude.json`에
비어 있지 않은 `oauthAccount`가 있을 것(로그아웃·백업 복사본 제외), Codex는 `auth.json` 파일이 있을 것.
슬롯 key = 폴더 정규 절대경로(NFC·normpath·끝 슬래시 없음), label = <이름>, 폴더 이름순.
슬롯 계정 = server.accounts.resolve(슬롯 폴더만 담은 env) — 구독이면 로그인 이메일, API 로그인이면 `api:…`(수집·깨우기·
이력 없음), 모르면 "". 이메일은 로컬 파일에서만 얻고(API 응답에서 얻지 않는다) 로그에는 슬롯 라벨만 남긴다.

- Claude: Claude Code가 macOS 키체인("Claude Code-credentials-<해시>") 또는 <폴더>/.credentials.json에 두는 OAuth
  액세스 토큰으로 읽기 전용 엔드포인트 셋을 조회한다 — /api/oauth/usage?cedar_ember=1(한도 창·초기화권), /api/oauth/profile(플랜 표시명
  organization.plan_display_name·조직 uuid), /api/oauth/organizations/{uuid}/prepaid/credits(사용 크레딧 잔액).
  자격증명의 subscriptionType은 로그인 시점 값이라 갱신되지 않으므로 플랜에 쓰지 않는다. 토큰·조직 uuid는 메모리에만 두고 기록하지 않는다.
  키체인 서비스 이름은 "Claude Code-credentials-" + sha256(CLAUDE_CONFIG_DIR 문자열, NFC, utf-8) 앞 8자리다. Claude Code는
  경로 문자열을 그대로 해시하므로 끝 슬래시 유무에 따라 이름이 다르다 — 후보 [폴더, 폴더 + "/"]를 순서대로 찾고(없으면 파일),
  찾은 문자열을 깨우기의 CLAUDE_CONFIG_DIR로 그대로 쓴다. 429 백오프·프로필·크레딧 캐시·갱신 쿨다운은 슬롯별이고,
  한 슬롯의 실패가 다른 슬롯 수집을 막지 않는다.
  갱신 깨우기: 접근 토큰(수명 8시간)은 Claude Code가 실행될 때만 갱신된다. 접근 토큰이 만료됐거나(30초 여유) 사용량 조회가
  401이면 그 슬롯으로 `claude -p`(최소 호출, 위생 플래그 — llm.CLAUDE_HYGIENE)를 한 번 실행해 갱신을 일으키고,
  자격증명의 토큰이 바뀌었을 때만 같은 회차에 다시 조회한다. 실행 env에서 ANTHROPIC_*·CLAUDE_CODE_USE_*·CLAUDE_CODE_OAUTH_TOKEN을
  지운다(허브 환경의 API 키·토큰으로 과금되거나 갱신이 실패하지 않게). 만료 전에는 실행하지 않는다(유효 토큰은 갱신 없이 그대로
  쓰인다). 슬롯별 쿨다운 10분, 로그인 자체가 끝났으면(refreshTokenExpiresAt) 실행하지 않고 "로그인 만료"로 알린다.
- Codex: 슬롯마다 `CODEX_HOME=<폴더>`로 `codex app-server --stdio` JSON-RPC(initialize → initialized → account/rateLimits/read).
  실패하면 대체 수집 없이 그 슬롯은 마지막 성공값을 유지하고 error만 붙인다(슬롯별 백오프 동안 app-server도 다시 띄우지 않는다).
- 이력: 계정이 구독 이메일인 슬롯만 usage_history·usage_plan_history에 그 이메일로 기록한다. 같은 이메일 슬롯이 둘이면 한 회차에
  이름순 첫 슬롯만 기록한다. ''·api: 계정은 기록하지 않는다.
허브는 기본 로그인(~/.claude.json, ~/.codex/auth.json)이나 그 전사본에 기대지 않는다 — 수집은 슬롯 폴더만 쓴다.
한도는 계정 단위라 허브 기기 하나에서 읽으면 된다.
"""
import hashlib
import http.client
import json
import logging
import math
import os
import queue
import re
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from . import accounts, db
from .config import CFG

log = logging.getLogger("madison.usage")
_STORE_KEY = "usage.snapshot"     # settings 테이블에 마지막 성공값 보존 — 허브 재시작 직후 빈 타일 방지

PROVIDERS = ("claude", "codex")
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_CREDITS_URL = "https://api.anthropic.com/api/oauth/organizations/{org}/prepaid/credits"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
SLOT_PREFIX = {"claude": ".claude-", "codex": ".codex-"}      # 홈 아래 슬롯 폴더 이름 접두사
SLOT_AGENT = {"claude": "claude-code", "codex": "codex-cli"}
SLOT_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
API_SLOT_ERROR = "API 로그인 — 구독 한도 없음"

_lock = threading.Lock()
# 프로바이더별 {슬롯 key: 스냅샷}. 슬롯 key = 폴더 정규 절대경로. 스냅샷은 마지막 성공값 + error
_snap: dict = {"claude": {}, "codex": {}}
# 프로바이더별 마지막 탐색 결과 [{key, label, account}] — 폴더 이름순. 수집 주기마다 갱신
_slots: dict = {"claude": [], "codex": []}
CLAUDE_BACKOFF_SEC = 600     # 429(과호출) 뒤 쉬는 시간 — 같은 계정을 다른 도구도 조회할 수 있다
CODEX_BACKOFF_SEC = 300      # Codex RPC 실패 뒤 app-server를 다시 띄우기까지 쉬는 시간 — 고장난 동안 매 분 프로세스를 반복해 띄우지 않는다
CLAUDE_PROFILE_TTL = 3600    # 초 — 플랜 표시명은 거의 안 바뀐다
CLAUDE_CREDITS_TTL = 600     # 초 — 크레딧 잔액 재조회 간격
CLAUDE_AUX_RETRY_SEC = 600   # 초 — 프로필·크레딧 조회가 실패(429 포함)한 뒤 다시 시도하기까지 쉬는 시간
CLAUDE_WAKE_COOLDOWN = 600   # 초 — 슬롯별 갱신 깨우기 실행 간격(연속 실패 시 매 회차 실행하지 않는다)
CLAUDE_WAKE_TIMEOUT = 90     # 초 — 깨우기 실행 제한
CLAUDE_EXPIRY_SLACK = 30     # 초 — 접근 토큰 만료 직전 여유(이 안이면 만료로 본다)
LOGIN_EXPIRED = "로그인 만료 — 다시 로그인 필요"


class UsageError(RuntimeError):
    pass


class UnauthorizedError(UsageError):
    """usage 조회 401 — 접근 토큰 만료·거절. 갱신 깨우기의 신호."""


def _new_state() -> dict:
    """슬롯별 수집 상태(메모리 전용). 캐시의 fp는 접근 토큰 지문 — 토큰이 바뀔 때(회전 약 8시간마다, 재로그인 포함) 비운다. 계정 변경은 슬롯의 account로 따로 가른다.
    rpc_until·rpc_reason은 Codex RPC 백오프(until epoch까지 app-server를 부르지 않고, reason은 마지막 실패 사유)."""
    return {"backoff_until": 0.0,
            "profile": {"at": 0.0, "plan": None, "org": "", "fp": "", "failed_at": 0.0},
            "credits": {"at": 0.0, "entry": None, "fp": "", "failed_at": 0.0},
            "wake_at": 0.0, "wake_error": "",
            "rpc_until": 0.0, "rpc_reason": "",
            "login_ends": None, "login_seen": False, "error": ""}


_slot_state: dict = {}


def _st(key: str) -> dict:
    """슬롯 key의 수집 상태 — 없으면 만든다."""
    with _lock:
        return _slot_state.setdefault(key, _new_state())


# ── 슬롯 탐색 ─────────────────────────────────────────

def _canon(path) -> str:
    """슬롯 key — NFC·normpath·절대경로·끝 슬래시 없음. Claude Code가 이 문자열을 그대로 해시한다."""
    return unicodedata.normalize("NFC", os.path.normpath(os.path.abspath(str(path))))


def _claude_slot_ok(folder: Path) -> bool:
    """폴더 안 .claude.json에 비어 있지 않은 oauthAccount dict가 있는가 — 로그아웃·백업 복사본 오탐을 막는다."""
    try:
        data = json.loads((folder / ".claude.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    return isinstance(oauth, dict) and bool(oauth)


def _codex_slot_ok(folder: Path) -> bool:
    return (folder / "auth.json").is_file()


def slot_account(provider: str, key: str) -> str:
    """슬롯의 계정 — 허브 프로세스 env가 섞이지 않게 슬롯 폴더만 담은 env로 푼다. 실패하면 ""(미상)."""
    try:
        return accounts.resolve(SLOT_AGENT[provider], {SLOT_ENV[provider]: key}, headless=False)
    except Exception:
        return ""


def _discover(provider: str, home=None) -> list:
    """홈 아래 슬롯 폴더 → [{key, label, account}] (이름순). 읽는 것은 로컬 파일뿐 — 네트워크·키체인을 쓰지 않는다."""
    prefix = SLOT_PREFIX[provider]
    ok = _claude_slot_ok if provider == "claude" else _codex_slot_ok
    home = Path(home) if home is not None else Path.home()
    try:
        names = sorted(os.listdir(home), key=lambda n: unicodedata.normalize("NFC", n))
    except OSError:
        return []
    # 기본 로그인 폴더(~/.claude·~/.codex)를 가리키는 심볼릭 링크는 슬롯이 아니다 — 허브가 사람의 로그인을 읽고 갱신하게 된다.
    # 같은 실제 폴더를 가리키는 슬롯이 둘이면 이름순 첫 슬롯만 남긴다.
    seen = {os.path.realpath(home / prefix.rstrip("-"))}
    out = []
    for name in names:
        if not name.startswith(prefix) or len(name) == len(prefix):
            continue
        folder = home / name
        try:
            if not folder.is_dir() or not ok(folder):
                continue
            real = os.path.realpath(folder)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        key = _canon(folder)
        label = unicodedata.normalize("NFC", name[len(prefix):])
        out.append({"key": key, "label": label, "account": slot_account(provider, key)})
    return out


def _rescan(provider: str) -> list:
    """다시 탐색해 _slots에 반영하고 돌려준다. 탐색이 예외로 실패하면 마지막 결과를 유지한다."""
    try:
        found = _discover(provider)
    except Exception:
        log.exception("usage %s 슬롯 탐색 오류", provider)
        with _lock:
            return list(_slots[provider])
    with _lock:
        _slots[provider] = found
    return list(found)


# ── 공통 ──────────────────────────────────────────────

def to_epoch(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:                 # 시간대 없는 ISO는 UTC로
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def format_remaining(resets_at, now=None) -> str:
    """리셋까지 남은 시간: '3d05h', '2h31m', '45m'; 지났으면 'resetting'."""
    if not isinstance(resets_at, (int, float)):
        return ""
    now = time.time() if now is None else now
    delta = int(resets_at - now)
    if delta <= 0:
        return "resetting"
    days, rem = divmod(delta, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{max(minutes, 1)}m"


def _window(title, percent, resets_at, order=9):
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    return {"title": title, "pct": int(round(max(0.0, min(float(percent), 100.0)))),
            "resets_at": to_epoch(resets_at), "order": order}


def _finish(windows, now=None) -> list:
    out = []
    for w in sorted(windows, key=lambda x: (x["order"], x["title"])):
        left = format_remaining(w["resets_at"], now)
        out.append({"title": w["title"], "pct": w["pct"], "left": left, "resets_at": w["resets_at"],
                    "value": f"{w['pct']}%" + (f" · {left} left" if left else "")})
    return out


def _reset_count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _reset_expiry(value) -> float | None:
    stamp = to_epoch(value)
    return stamp if stamp is not None and math.isfinite(stamp) else None


def _reset_entry(count: int, expirations=()) -> dict:
    entry = {"title": "Reset credits", "value": f"{count} available", "count": count}
    if expirations:
        entry["expirations"] = expirations
    return entry


# ── Claude ────────────────────────────────────────────

@lru_cache(maxsize=1)
def _claude_version_user_agent(cli: str, mtime_ns: int) -> str:
    """초기화권은 CLI 버전·surface를 확인한다. 설치된 버전을 읽고 MADISON도 명시한다.
    바이너리가 갱신되면 캐시 키(mtime)가 바뀐다. 조회 실패 시 기존 사용량 수집은 계속한다."""
    try:
        env = {**os.environ, "PATH": os.path.dirname(cli) + os.pathsep + os.environ.get("PATH", "")}
        done = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=5, check=False, env=env)
        version = re.match(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?(?=\s|$)", done.stdout.strip())
        if done.returncode == 0 and version:
            return f"claude-cli/{version[0]} (external, cli) madison-hub/usage"
    except (OSError, subprocess.SubprocessError):
        pass
    return "madison-hub/usage"


def _claude_user_agent() -> str:
    from . import llm
    try:
        cli = llm.conf("summary")["claude_bin"]
    except Exception:
        cli = CFG.task_summary_bin
    try:
        mtime_ns = Path(cli).stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return _claude_version_user_agent(cli, mtime_ns)


def claude_keychain_service(config_dir: str = "") -> str:
    """Claude Code의 키체인 서비스 이름 — CLAUDE_CONFIG_DIR이 없으면 기본, 있으면 폴더 경로 문자열(NFC)의
    sha256 앞 8자리를 붙인다(설치된 Claude Code 2.1.332에서 확인한 규칙). 문자열을 그대로 해시하므로
    끝 슬래시가 있고 없고가 서로 다른 이름이다."""
    if not config_dir:
        return CLAUDE_KEYCHAIN_SERVICE
    digest = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode("utf-8")).hexdigest()[:8]
    return f"{CLAUDE_KEYCHAIN_SERVICE}-{digest}"


def claude_credentials_file(config_dir: str) -> Path:
    """키체인 쓰기가 막힌 환경(SSH 등)의 대체 저장 위치 — 설정 폴더 아래."""
    return Path(config_dir) / ".credentials.json"


def claude_credentials(key: str, only: str | None = None) -> tuple[dict, str]:
    """슬롯의 (claudeAiOauth 객체, 찾은 CLAUDE_CONFIG_DIR 문자열). 객체엔 토큰이 있다 — 로그·응답에 넣지 말 것.
    키체인 서비스 이름 후보 [key, key + "/"]를 차례로 찾고(Claude Code가 로그인 때 쓴 문자열에 따라 둘 중 하나),
    둘 다 없으면 <key>/.credentials.json. 돌려주는 문자열을 깨우기의 CLAUDE_CONFIG_DIR로 그대로 써야 같은
    저장소가 갱신된다(파일이면 key). only를 주면 그 문자열 하나만 찾는다(깨우기 뒤 다시 읽기).
    오류 문구에는 어느 서비스·파일을 봤는지만 담는다."""
    candidates = [only] if only else [key, key + "/"]
    path = claude_credentials_file(key)
    services = [claude_keychain_service(c) for c in candidates]
    where = "키체인 " + "·".join(f"'{svc}'" for svc in services) + f"·{path}"
    raw, found = "", only or key
    for cand, service in zip(candidates, services):
        try:
            done = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                  capture_output=True, text=True, timeout=10, check=False)
            raw = done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raw = ""
        if raw:
            found = cand
            break
    if not raw:
        try:
            raw = path.read_text("utf-8")
        except OSError as exc:
            hint = " — 이 폴더로 CLAUDE_CONFIG_DIR=<폴더> claude 를 실행해 /login 했는지 확인(Claude Code는 그 경로 문자열 그대로 해시한다)"
            raise UsageError(f"Claude Code 자격증명 없음({where}){hint}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise UsageError(f"Claude Code 자격증명이 JSON이 아님({where})") from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise UsageError(f"OAuth 액세스 토큰 없음({where})")
    return oauth, found


def claude_get(url: str, token: str, label: str = "usage", timeout: float = 15, backoff: bool = False,
               key: str = "") -> dict:
    """Claude OAuth GET → JSON 객체. 401 처리는 공통. 429는 backoff=True(usage)일 때만 그 슬롯(key)의
    호출 백오프를 건다."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "anthropic-beta": CLAUDE_OAUTH_BETA,
        "User-Agent": _claude_user_agent() if url == CLAUDE_USAGE_URL else "madison-hub/usage"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise UnauthorizedError("토큰 거절(401)") from exc
        if exc.code == 429:
            if not backoff:
                raise UsageError(f"{label} HTTP 429") from exc
            _st(key)["backoff_until"] = time.time() + CLAUDE_BACKOFF_SEC
            raise UsageError(f"{label} HTTP 429 — {CLAUDE_BACKOFF_SEC // 60}분 뒤 재시도") from exc
        raise UsageError(f"{label} HTTP {exc.code}") from exc
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise UsageError(f"{label} 조회 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError(f"{label} 응답이 객체가 아님")
    return data


def claude_fetch(token: str, timeout: float = 15, key: str = "") -> dict:
    return claude_get(CLAUDE_USAGE_URL, token, "usage", timeout, backoff=True, key=key)


def _scope_name(item: dict):
    scope = item.get("scope")
    if not isinstance(scope, dict):
        return None
    model = scope.get("model")
    name = (model.get("display_name") or model.get("id")) if isinstance(model, dict) else None
    return name or scope.get("surface")


def parse_claude(data: dict) -> list:
    """limits[] (또는 구형 five_hour/seven_day) → 창 목록: 5h, 7d 전체, 7d 모델별, 기타."""
    windows = []
    limits = data.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            if kind == "session":
                title, order = "5h", 0
            elif kind == "weekly_all":
                title, order = "7d all models", 1
            elif kind == "weekly_scoped":
                title, order = f"7d {_scope_name(item) or 'scoped'}", 2
            else:
                title, order = kind.replace("_", " ") or "limit", 3
            w = _window(title, item.get("percent"), item.get("resets_at"), order)
            if w:
                windows.append(w)
    if not windows:
        for key, title, order in (("five_hour", "5h", 0), ("seven_day", "7d all models", 1)):
            item = data.get(key)
            if isinstance(item, dict):
                w = _window(title, item.get("utilization"), item.get("resets_at"), order)
                if w:
                    windows.append(w)
    return windows


def parse_profile(data: dict) -> tuple[str, str]:
    """/api/oauth/profile → (플랜 표시명, 조직 uuid). 없으면 빈 문자열."""
    org = data.get("organization") if isinstance(data, dict) else None
    if not isinstance(org, dict):
        return "", ""
    plan = org.get("plan_display_name")
    uuid = org.get("uuid")
    return (plan.strip() if isinstance(plan, str) else ""), (uuid.strip() if isinstance(uuid, str) else "")


def _claude_reset_kind(grant: dict) -> str:
    """CLI와 같이 Fable 환급 표시를 type보다 우선한다. 불명확한 종류는 추정하지 않는다."""
    if grant.get("gives_back_weekly") is True:
        return "fable"
    kind = grant.get("type")
    if kind in ("session", "weekly", "full"):
        return kind
    if kind is not None:
        return "unknown"
    clears = grant.get("clears")
    known = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
             "seven_day_overage_included", "seven_day_cowork", "seven_day_omelette", "seven_day_oauth_apps")
    if not isinstance(clears, list) or not clears or any(x not in known for x in clears):
        return "unknown"
    session = "five_hour" in clears
    weekly = any(x != "five_hour" for x in clears)
    return "full" if session and weekly else "session" if session else "weekly"


def parse_claude_resets(data: dict, now=None) -> dict | None:
    """유효한 grant의 보유량·종류별 만료. 사용 한도 resets_at과 ends_at은 별개다."""
    status = data.get("cedar_ember")
    if not isinstance(status, dict) or status.get("eligible") is not True:
        return None  # surface/cli_version 등 조회 불가를 0장으로 오인하지 않는다
    grants = status.get("grants")
    if not isinstance(grants, list):
        return None
    now = time.time() if now is None else now
    count, valid, expirations, groups = 0, 0, [], {}
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        left = _reset_count(grant.get("resets_left"))
        if left is None:
            continue
        valid += 1
        starts = _reset_expiry(grant.get("starts_at"))
        ends = _reset_expiry(grant.get("ends_at"))
        if (grant.get("paused") is True
                or (starts is not None and starts > now) or (ends is not None and ends <= now)):
            continue
        kind = _claude_reset_kind(grant)
        group = groups.setdefault(kind, {"kind": kind, "count": 0})
        if not left:
            continue  # 유효 기간 안의 소진된 종류는 0장으로 표시
        # usable_now=False는 아직 한도에 닿지 않았다는 뜻일 수도 있다. 미사용 권리는 계속 표시한다.
        count += left
        group["count"] += left
        if ends is not None:
            expiry = {"count": left, "expires_at": ends}
            expirations.append(expiry)
            group.setdefault("expirations", []).append(expiry)
    if not valid and grants:
        return None
    entry = _reset_entry(count, expirations)
    if any(kind != "unknown" for kind in groups):
        entry["groups"] = [groups[kind] for kind in ("session", "weekly", "fable", "full", "unknown")
                           if kind in groups]
    return entry


def format_money(minor: int, currency: str, exponent: int = 2) -> str:
    """최소 단위 정수 → 'USD'는 '$12.34', 그 외는 '12.34 EUR'. 음수는 '-$1.50'."""
    exponent = exponent if isinstance(exponent, int) and not isinstance(exponent, bool) and exponent >= 0 else 2
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(int(minor)), 10 ** exponent)
    num = str(whole)
    if exponent:
        num += "." + str(frac).zfill(exponent)
    cur = str(currency or "").strip().upper()
    if cur == "USD":
        return f"{sign}${num}"
    return f"{sign}{num} {cur}".rstrip()


def parse_credits(data: dict, enabled) -> dict | None:
    """/prepaid/credits → extras 항목. amount가 숫자가 아니면 None. amount는 최소 단위(센트)."""
    if not isinstance(data, dict):
        return None
    amount = data.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if not math.isfinite(amount):       # JSON NaN/Infinity
        return None
    amount = int(amount)
    currency = data.get("currency")
    currency = currency.strip().upper() if isinstance(currency, str) and currency.strip() else "USD"
    balance = data.get("balance")
    credits = balance.get("credits") if isinstance(balance, dict) else None
    exponent = credits.get("exponent") if isinstance(credits, dict) else None
    if isinstance(exponent, bool) or not isinstance(exponent, int):
        exponent = 2
    return {"title": "Credits", "value": format_money(amount, currency, exponent), "amount": amount,
            "currency": currency, "enabled": enabled if isinstance(enabled, bool) else None,
            "expires_at": to_epoch(data.get("next_expires_at"))}


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _snapshot_prev(provider: str, key: str, account: str) -> dict | None:
    """슬롯의 마지막 성공 스냅샷 — 스냅샷의 account가 지금 슬롯 계정과 같을 때만 쓴다.
    같은 폴더에 다른 계정으로 다시 로그인했으면 옛 계정의 플랜·크레딧을 새 계정의 값으로 내지 않는다."""
    with _lock:
        prev = _snap[provider].get(key)
    if isinstance(prev, dict) and prev.get("account", "") == account:
        return prev
    return None


def _claude_profile(token: str, now: float, key: str, account: str = "") -> tuple[str, str, bool]:
    """(플랜, 조직 uuid, 조회값 여부) — 슬롯별 TTL 캐시. 실패하면 마지막 값 유지(모듈 캐시 → 같은 계정의 스냅샷 플랜 → 빈 값).
    실패 뒤 CLAUDE_AUX_RETRY_SEC 동안은 다시 부르지 않는다. 모듈 캐시는 접근 토큰이 바뀔 때마다(회전 약 8시간마다, 다른 계정으로
    다시 로그인한 경우 포함) 비운다 — 토큰 회전과 계정 변경은 이것만으로 구분되지 않으므로, 캐시가 빈 뒤의 폴백(스냅샷)은
    스냅샷의 account가 슬롯 계정과 같을 때만 쓴다.
    조회값 여부 = 이 토큰으로 실제 조회한 플랜인가(조회 성공 또는 같은 토큰의 캐시). 스냅샷 폴백·빈 값은 False —
    표시에만 쓰고 플랜 이력에는 쓰지 않는다."""
    cache = _st(key)["profile"]
    fp = _fingerprint(token)
    if cache["fp"] != fp:
        cache.update(at=0.0, plan=None, org="", fp=fp, failed_at=0.0)
    if cache["plan"] is not None and now - cache["at"] < CLAUDE_PROFILE_TTL:
        return cache["plan"], cache["org"], True
    if not (cache["failed_at"] and now - cache["failed_at"] < CLAUDE_AUX_RETRY_SEC):
        try:
            plan, org = parse_profile(claude_get(CLAUDE_PROFILE_URL, token, "profile"))
            if plan:
                cache.update(at=now, plan=plan, org=org or cache["org"], failed_at=0.0)
                return plan, cache["org"], True
            log.info("claude profile에 플랜 표시명 없음")
        except UsageError as exc:
            log.info("claude profile 조회 실패: %s", exc)
        except Exception as exc:        # 부가 정보가 한도 창 기록을 막지 않게
            log.info("claude profile 처리 오류: %s", type(exc).__name__)
        cache["failed_at"] = now
    if cache["plan"]:
        return cache["plan"], cache["org"], True
    prev = _snapshot_prev("claude", key, account)
    return (str(prev.get("plan") or "") if prev else ""), cache["org"], False


def _claude_credits(token: str, org: str, enabled, now: float, key: str, account: str = "") -> dict | None:
    """사용 크레딧 extras 항목 — 슬롯별 TTL 캐시, 실패하면 마지막 항목 유지(모듈 캐시 → 같은 계정의 스냅샷 extras), 없으면 None.
    실패 뒤 CLAUDE_AUX_RETRY_SEC 동안은 다시 부르지 않는다. 모듈 캐시는 접근 토큰이 바뀔 때마다 비우고(토큰 회전·재로그인),
    스냅샷 폴백은 스냅샷의 account가 슬롯 계정과 같을 때만 쓴다."""
    st = _st(key)
    cache = st["credits"]
    fp = _fingerprint(token)
    if cache["fp"] != fp:
        cache.update(at=0.0, entry=None, fp=fp, failed_at=0.0)
    entry = None
    if cache["entry"] is not None and now - cache["at"] < CLAUDE_CREDITS_TTL:
        entry = cache["entry"]
    elif (org and time.time() >= st["backoff_until"]
          and not (cache["failed_at"] and now - cache["failed_at"] < CLAUDE_AUX_RETRY_SEC)):
        try:
            url = CLAUDE_CREDITS_URL.format(org=urllib.parse.quote(org, safe=""))
            entry = parse_credits(claude_get(url, token, "credits"), enabled)
            if entry is None:
                log.info("claude credits 응답에 amount 없음")
                cache["failed_at"] = now
            else:
                cache.update(at=now, entry=entry, failed_at=0.0)
        except UsageError as exc:
            log.info("claude credits 조회 실패: %s", exc)
            cache["failed_at"] = now
        except Exception as exc:        # 부가 정보가 한도 창 기록을 막지 않게
            log.info("claude credits 처리 오류: %s", type(exc).__name__)
            cache["failed_at"] = now
    if entry is None:
        entry = cache["entry"]
    if entry is None:
        prev = _snapshot_prev("claude", key, account)
        for ex in (prev.get("extras") if prev else None) or []:
            if isinstance(ex, dict) and ex.get("title") == "Credits":
                entry = ex
                break
    # on/off는 매 회차 usage 응답의 값을 쓴다(잔액 캐시와 별개로 갱신)
    return {**entry, "enabled": enabled if isinstance(enabled, bool) else None} if entry else None


# ── 갱신 깨우기 ───────────────────────────────────────

def _ms_to_epoch(value) -> float | None:
    """자격증명의 밀리초 시각 → 초 epoch. 숫자가 아니면 None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) / 1000.0


def _wake_env(cli: str, config_dir: str) -> dict:
    """깨우기 실행 env — 허브 환경의 인증 관련 값(ANTHROPIC_*·CLAUDE_CODE_USE_*·CLAUDE_CODE_OAUTH_TOKEN)을 지우고
    CLAUDE_CONFIG_DIR만 슬롯 문자열로 둔다(허브 환경의 API 키로 과금되거나 슬롯 로그인이 아닌 토큰으로 실행되지 않게)."""
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_")
                   or k in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"))}
    env["MADISON_SUPPRESS"] = "1"
    env["PATH"] = os.path.dirname(cli) + os.pathsep + os.environ.get("PATH", "")
    env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def _run_claude_wake(slot: dict, config_dir: str) -> str:
    """그 슬롯으로 Claude Code를 최소 호출로 한 번 실행한다 — 접근 토큰 갱신은 Claude Code가 실행될 때만 일어난다.
    llm.run과 같은 위생 플래그(--safe-mode: 사용자 훅·플러그인 비활성)·전용 비-git cwd·독립 프로세스 그룹·MADISON_SUPPRESS.
    config_dir은 자격증명을 찾은 문자열 그대로(끝 슬래시 포함 여부까지) — 키체인 서비스 이름이 그 문자열의 해시다.
    반환: 로그용 요약(토큰·응답 본문은 담지 않는다). 실행 실패는 UsageError."""
    from . import llm
    try:
        cli = llm.conf("summary")["claude_bin"]
    except Exception:
        cli = CFG.task_summary_bin
    cmd = [cli, "-p", "ok", "--output-format", "json", *llm.CLAUDE_HYGIENE, "--model", "haiku"]
    env = _wake_env(cli, config_dir)
    t0 = time.monotonic()
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=CLAUDE_WAKE_TIMEOUT, check=False,
                              stdin=subprocess.DEVNULL, cwd=str(llm._cwd()), start_new_session=True, env=env)
    except subprocess.TimeoutExpired as exc:
        raise UsageError(f"갱신용 claude 실행 시간 초과({CLAUDE_WAKE_TIMEOUT}초)") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsageError(f"갱신용 claude 실행 실패: {type(exc).__name__}") from exc
    summary = f"exit {done.returncode}, {time.monotonic() - t0:.1f}s"
    log.info("claude 갱신 깨우기 '%s': %s", slot["label"], summary)
    return summary


def claude_wake(slot: dict, oauth: dict, config_dir: str) -> dict:
    """접근 토큰을 갱신시키고 다시 읽은 oauth를 돌려준다. 로그인이 끝났거나 쿨다운 중이거나 실행·갱신에 실패하면 UsageError.
    토큰 지문이 바뀐 경우에만 성공으로 본다 — 안 바뀌었으면 호출측이 재조회해 봐야 같은 401이다."""
    st = _st(slot["key"])
    now = time.time()
    ends = _ms_to_epoch(oauth.get("refreshTokenExpiresAt"))
    if ends is not None and ends <= now:
        raise UsageError(LOGIN_EXPIRED)
    waited = now - st["wake_at"]
    if st["wake_at"] and waited < CLAUDE_WAKE_COOLDOWN:
        minutes = max(1, math.ceil((CLAUDE_WAKE_COOLDOWN - waited) / 60))
        raise UsageError(f"{st['wake_error'] or '토큰 갱신 대기'} — {minutes}분 뒤 재시도")
    st["wake_at"] = now
    try:
        summary = _run_claude_wake(slot, config_dir)
        fresh, _ = claude_credentials(slot["key"], only=config_dir)
        if _fingerprint(str(fresh["accessToken"])) == _fingerprint(str(oauth["accessToken"])):
            raise UsageError(f"토큰이 갱신되지 않음(갱신용 claude {summary})")
    except UsageError as exc:
        st["wake_error"] = str(exc)
        raise
    st["wake_error"] = ""
    st["login_ends"] = _ms_to_epoch(fresh.get("refreshTokenExpiresAt"))
    return fresh


def collect_claude(slot: dict, now=None) -> dict:
    key = slot["key"]
    st = _st(key)
    if time.time() < st["backoff_until"]:
        raise UsageError("429 백오프 중")
    oauth, config_dir = claude_credentials(key)
    st["login_ends"], st["login_seen"] = _ms_to_epoch(oauth.get("refreshTokenExpiresAt")), True
    woke = False
    expires = _ms_to_epoch(oauth.get("expiresAt"))
    if expires is not None and expires <= time.time() + CLAUDE_EXPIRY_SLACK:
        # 만료된 토큰으로 조회해 봐야 401이다. 만료 전에는 깨우지 않는다 — 유효 토큰은 갱신 없이 그대로 쓰인다
        oauth, woke = claude_wake(slot, oauth, config_dir), True
    token = str(oauth["accessToken"])
    try:
        usage = claude_fetch(token, key=key)
    except UnauthorizedError as exc:
        if woke:
            raise
        try:
            oauth, woke = claude_wake(slot, oauth, config_dir), True
        except UsageError as wake_exc:
            if str(wake_exc) == LOGIN_EXPIRED:
                raise
            raise UsageError(f"{exc} — {wake_exc}") from wake_exc
        token = str(oauth["accessToken"])
        usage = claude_fetch(token, key=key)      # 갱신 직후에도 401이면 그대로 오류
    windows = parse_claude(usage)
    if not windows:
        raise UsageError("usage 응답에 한도 창이 없음")
    # 플랜·크레딧은 부가 정보 — 실패해도 한도 창 기록은 막지 않는다. subscriptionType은 로그인 시점 값이라 쓰지 않는다.
    t = time.time()
    account = slot.get("account", "")
    plan, org, plan_live = _claude_profile(token, t, key, account)
    extra_usage = usage.get("extra_usage")
    enabled = extra_usage.get("is_enabled") if isinstance(extra_usage, dict) else None
    credits = _claude_credits(token, org, enabled, t, key, account)
    resets = parse_claude_resets(usage, now)
    # plan_live: 이 회차에 조회한(또는 같은 토큰으로 조회해 둔) 플랜인가 — 이력에 쓸 수 있는지의 표시이며 스냅샷에는 두지 않는다
    return {"key": key, "label": slot["label"], "account": account, "plan": plan, "plan_live": plan_live,
            "windows": _finish(windows, now),
            "extras": [x for x in (resets, credits) if x is not None], "login_ends": st["login_ends"],
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))}


# ── Codex ─────────────────────────────────────────────

def _codex_bin() -> str:
    from . import llm
    try:
        return llm.conf("summary")["codex_bin"]
    except Exception:
        return CFG.codex_bin


def codex_rpc(timeout: float = 20, codex_home: str | None = None) -> dict:
    """`codex app-server --stdio`: initialize → initialized → account/rateLimits/read.
    codex_home이 있으면 CODEX_HOME으로 그 슬롯의 로그인을 읽는다(없으면 허브 환경 그대로)."""
    codex = _codex_bin()
    try:
        # launchd의 최소 PATH엔 nvm node가 없다 — codex(셔뱅 `env node`)의 디렉터리를 PATH 앞에 (llm.run과 동일)
        env = {**os.environ, "PATH": os.path.dirname(codex) + os.pathsep + os.environ.get("PATH", "")}
        if codex_home:
            env["CODEX_HOME"] = codex_home
        proc = subprocess.Popen([codex, "app-server", "--stdio"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
    except OSError as exc:
        raise UsageError(f"codex app-server 시작 실패: {exc}") from exc
    lines: queue.Queue = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)
    threading.Thread(target=pump, daemon=True).start()

    def send(msg):
        proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    deadline = time.monotonic() + timeout
    try:
        send({"method": "initialize", "id": 1,
              "params": {"clientInfo": {"name": "madison", "title": "MADISON hub", "version": "0.1"}}})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageError("codex app-server 응답 없음(timeout)")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise UsageError("codex app-server 응답 없음(timeout)") from exc
            if line is None:
                raise UsageError("codex app-server가 먼저 종료됨")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == 1:
                send({"method": "initialized", "params": {}})
                send({"method": "account/rateLimits/read", "id": 2, "params": {}})
            elif msg.get("id") == 2:
                if msg.get("error"):
                    err = msg["error"]
                    raise UsageError(f"rateLimits/read 실패: {err.get('message') if isinstance(err, dict) else err}")
                result = msg.get("result")
                if not isinstance(result, dict):
                    raise UsageError("rateLimits/read 결과 없음")
                return result
    except (OSError, ValueError) as exc:
        # 자식이 먼저 죽으면 stdin 쓰기가 BrokenPipeError(OSError)·닫힌 파일(ValueError)로 터진다
        raise UsageError(f"codex app-server 통신 실패: {exc}") from exc
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass


def _window_title(minutes) -> str:
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
        return "window"
    v = int(minutes)
    if v % 10_080 == 0:
        return "7d" if v == 10_080 else f"{v // 10_080}w"
    if v % 1_440 == 0:
        return f"{v // 1_440}d"
    if v % 60 == 0:
        return f"{v // 60}h"
    return f"{v}m"


def format_credit_count(balance) -> str | None:
    """크레딧 잔액(십진 문자열 등) → 표시 문자열. 정수면 "12", 아니면 소수 2자리까지 0 제거("12.5").
    파싱 불가·비유한(NaN/inf)·bool 등은 None."""
    if isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        return None
    try:
        d = Decimal(str(balance).strip())
        if not d.is_finite():
            return None
        q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # 유효숫자 과다(1e30 등)면 InvalidOperation
    except (InvalidOperation, ValueError):
        return None
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _normalize_credits(raw) -> dict | None:
    """RPC(camelCase) credits 객체 → {balance, unlimited, has_credits}. 없거나 깨지면 None."""
    if not isinstance(raw, dict):
        return None
    balance = raw.get("balance")
    if balance is None or isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        balance = None
    has = raw.get("hasCredits")
    unlimited = raw.get("unlimited")
    if balance is None and unlimited is not True:
        return None  # 쓸 만한 잔액도 무제한 표시도 없으면 없는 것으로 본다(다음 한도 항목의 credits를 쓴다)
    return {"balance": str(balance) if balance is not None else None,
            "unlimited": unlimited is True, "has_credits": has is True}


def parse_codex_rpc(result: dict) -> dict:
    """account/rateLimits/read 결과 → {windows, plan, credits, reset_credits, reset_credit_expirations}."""
    by_id = result.get("rateLimitsByLimitId")
    if not isinstance(by_id, dict) or not by_id:
        single = result.get("rateLimits")
        by_id = {"codex": single} if isinstance(single, dict) else {}
    windows, plan, cr_norm = [], None, None
    for limit_id, limit in by_id.items():
        if not isinstance(limit, dict):
            continue
        plan = plan or limit.get("planType")
        cr_norm = cr_norm or _normalize_credits(limit.get("credits"))
        for slot in ("primary", "secondary", "individualLimit"):
            win = limit.get(slot)
            if not isinstance(win, dict):
                continue
            mins = win.get("windowDurationMins")
            title = _window_title(mins)
            if len(by_id) > 1:
                title = f"{title} {limit.get('limitName') or limit_id}"
            w = _window(title, win.get("usedPercent"), win.get("resetsAt"),
                        float(mins) if isinstance(mins, (int, float)) else 1e9)
            if w:
                windows.append(w)
    if cr_norm is None and isinstance(result.get("rateLimits"), dict):
        cr_norm = _normalize_credits(result["rateLimits"].get("credits"))
    credits = result.get("rateLimitResetCredits")
    avail = _reset_count(credits.get("availableCount")) if isinstance(credits, dict) else None
    details = credits.get("credits") if isinstance(credits, dict) else None
    expirations = []
    if avail and isinstance(details, list):
        # availableCount가 권위값이다. 상세 목록은 일부만 내려올 수도 있다.
        available = [x for x in details if isinstance(x, dict) and x.get("status") == "available"]
        for item in available[:avail]:
            expires = _reset_expiry(item.get("expiresAt"))
            if expires is not None:
                expirations.append({"count": 1, "expires_at": expires})
    return {"windows": windows, "plan": plan, "credits": cr_norm,
            "reset_credits": avail, "reset_credit_expirations": expirations}


def _rpc_failure_text(reason) -> str:
    """Codex RPC 실패 오류 문구 — 첫 실패와 백오프 창 안의 호출이 같은 문구를 쓴다(대시보드가 뒤에 로그인 명령을 붙인다)."""
    return f"Codex 읽기 실패: {reason} — {CODEX_BACKOFF_SEC // 60}분마다 재시도"


def collect_codex(slot: dict, now=None) -> dict:
    now = now or time.time()
    key = slot["key"]
    st = _st(key)
    # 호출 때마다 RPC를 읽는다 — 수집 주기는 loop()의 프로바이더별 주기(USAGE_CODEX_POLL_SEC)가 단일 출처.
    # 단, RPC가 실패하면 그 슬롯은 CODEX_BACKOFF_SEC 동안 app-server를 다시 부르지 않고 같은 문구로 실패한다(대체 수집 없음).
    if now < st["rpc_until"]:
        raise UsageError(_rpc_failure_text(st["rpc_reason"]))
    try:
        parsed = {**parse_codex_rpc(codex_rpc(codex_home=key)), "seen_at": now}
        if not parsed.get("windows"):
            # 응답은 왔지만 쓸 게 없으면 실패와 같다 — 백오프도 똑같이 탄다
            raise UsageError("rateLimits/read 응답에 한도 창이 없음")
        st.update(rpc_until=0.0, rpc_reason="")
    except UsageError as exc:
        st.update(rpc_until=now + CODEX_BACKOFF_SEC, rpc_reason=str(exc))
        log.info("codex RPC 실패 '%s': %s", slot["label"], exc)
        raise UsageError(_rpc_failure_text(exc)) from exc
    plan = str(parsed.get("plan") or "").strip()
    extras = []
    # extras 순서(Reset credits → Credits)는 여기가 단일 출처 — 프런트는 목록 순서대로 그린다.
    if isinstance(parsed.get("reset_credits"), int):
        extras.append(_reset_entry(parsed["reset_credits"], parsed.get("reset_credit_expirations", [])))
    # 크레딧은 부가 정보라 어떤 예외든 항목만 생략하고 한도 창·Reset 행을 막지 않는다.
    try:
        cr = parsed.get("credits")
        if isinstance(cr, dict):
            unlimited = cr.get("unlimited") is True
            count = format_credit_count(cr.get("balance"))
            if unlimited or count is not None:
                extras.append({"title": "Credits", "value": "unlimited" if unlimited else count,
                               "unlimited": unlimited, "has_credits": cr.get("has_credits") is True})
    except Exception as exc:
        log.info("codex 크레딧 항목 생략: %s", type(exc).__name__)
    return {"key": key, "label": slot["label"], "account": slot.get("account", ""),
            "plan": plan.capitalize() if plan else "", "windows": _finish(parsed["windows"], now),
            "extras": extras, "login_ends": None,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(parsed["seen_at"]))}


# ── 수집 루프·스냅샷 ─────────────────────────────────

def _persist():
    try:
        with _lock:
            # 현재 슬롯에 없는 key는 저장하지 않는다 — 표시하지 않는 스냅샷이 쌓이지 않게
            payload = json.dumps({p: {s["key"]: _snap[p][s["key"]] for s in _slots[p] if s["key"] in _snap[p]}
                                  for p in PROVIDERS}, ensure_ascii=False)
        with db.tx() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_STORE_KEY, payload))
    except Exception:
        log.exception("usage 스냅샷 저장 실패")


def load_persisted():
    """마지막 성공값 복원 + 슬롯 탐색(재시작 직후에도 타일이 바로 보이게). 두 프로바이더 모두 {폴더 key: 스냅샷}이다.
    이전 형식(프로바이더당 스냅샷 하나·Claude 계정 key "")은 버린다 — 값이 어느 로그인의 것인지 알 수 없다."""
    for p in PROVIDERS:
        _rescan(p)
    try:
        with db.tx() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (_STORE_KEY,)).fetchone()
        data = json.loads(row["value"]) if row else None
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with _lock:
        for p in PROVIDERS:
            saved = data.get(p)
            if not isinstance(saved, dict):
                continue
            for key, snap in saved.items():
                if (isinstance(key, str) and key.startswith("/") and isinstance(snap, dict)
                        and isinstance(snap.get("windows"), list) and not _snap[p].get(key)):
                    _snap[p][key] = snap


# 관측 시점에 이미 resets_at이 지난 행 = 묵은 스냅샷(예: 과거에 대체 수집으로 쌓인 옛 값). 그 %는 현재 창의 값이
# 아니므로 차트·리셋 감지에서 뺀다. 과거에 이렇게 쌓인 행은 지우지 않고 조회에서 거른다.
_LIVE = "(resets_at IS NULL OR resets_at > CAST(strftime('%s', ts) AS REAL))"


def record_history(c, provider: str, windows: list, ts: str | None = None, account: str = "") -> int:
    """창별 마지막 행과 pct가 다를 때만 usage_history에 append. 반환: 추가 행 수. 비교·기록은 provider+account 단위
    (account = 구독 로그인 이메일).
    변화만 쌓아 폴링 주기와 무관하게 용량을 억제한다(같은 pct로 돌아오는 리셋은 안 보이지만
    차트 해상도에선 무해). 조회는 계단선(step-after)으로 사이를 메운다. 이미 만료된 창은 기록하지 않는다."""
    ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    at = to_epoch(ts)
    n = 0
    for w in windows:
        if isinstance(w.get("resets_at"), (int, float)) and at is not None and w["resets_at"] <= at:
            continue
        last = c.execute(
            f"SELECT pct FROM usage_history WHERE provider=? AND account=? AND win=? AND {_LIVE}"
            " ORDER BY id DESC LIMIT 1", (provider, account, w["title"])).fetchone()
        if last is None or last["pct"] != w["pct"]:
            c.execute("INSERT INTO usage_history (ts, provider, account, win, pct, resets_at) VALUES (?,?,?,?,?,?)",
                      (ts, provider, account, w["title"], w["pct"], w.get("resets_at")))
            n += 1
    return n


def record_plan(c, provider: str, plan, ts: str | None = None, account: str = "") -> bool:
    """플랜명이 마지막 행과 다를 때만 usage_plan_history에 append. 반환: 추가 여부.
    플랜이 바뀌면 창 구성·한도 자체가 달라 전후 %는 비교 대상이 아니므로 차트에 경계를 그리기 위해 남긴다.
    빈 플랜(응답에 없음)은 기록하지 않는다. 마지막 행은 ts 기준 — 손으로 넣은 과거 행(백필)이 id 순서와
    어긋나도 현재 플랜을 바르게 본다."""
    plan = str(plan or "").strip()
    if not plan:
        return False
    ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last = c.execute("SELECT plan FROM usage_plan_history WHERE provider=? AND account=?"
                     " ORDER BY ts DESC, id DESC LIMIT 1", (provider, account)).fetchone()
    if last is not None and last["plan"] == plan:
        return False
    c.execute("INSERT INTO usage_plan_history (ts, provider, account, plan) VALUES (?,?,?,?)",
              (ts, provider, account, plan))
    return True


def plan_changes(c, provider: str, frm: float | None, account: str = "") -> list:
    """범위 안의 플랜 변경 [{ts, from, to}] — 직전 플랜은 범위 밖 마지막 행에서 이어받는다.
    이전 행이 없는 첫 관측은 변경이 아니다."""
    if frm is not None:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frm))
        prev = c.execute("SELECT plan FROM usage_plan_history WHERE provider=? AND account=? AND ts < ?"
                         " ORDER BY ts DESC, id DESC LIMIT 1", (provider, account, cutoff)).fetchone()
        rows = c.execute("SELECT ts, plan FROM usage_plan_history WHERE provider=? AND account=? AND ts >= ?"
                         " ORDER BY ts, id", (provider, account, cutoff))
    else:
        prev = None
        rows = c.execute("SELECT ts, plan FROM usage_plan_history WHERE provider=? AND account=? ORDER BY ts, id",
                         (provider, account))
    out, prev_plan = [], (prev["plan"] if prev else None)
    for r in rows:
        if prev_plan is not None and r["plan"] != prev_plan:
            ts = to_epoch(r["ts"])
            out.append({"ts": int(ts) if ts else None, "from": prev_plan, "to": r["plan"], "drops": []})
        prev_plan = r["plan"]
    return out


PLAN_CHANGE_SLACK = 600   # 초 — 플랜 변경 관측과 이만큼 가까운 pct 하락은 리셋이 아니라 새 플랜의 한도로 본다


def _claim_plan_drops(resets: list, changes: list, win: str) -> list:
    """플랜 변경 시점(±PLAN_CHANGE_SLACK)에 잡힌 하락은 그 변경의 drops로 옮기고 리셋 목록에서 뺀다.
    플랜이 바뀌면 한도 자체가 달라져 %가 떨어지는데(2026-10-12 Team→Prolite: 7d 16%→2%), 이는 조기
    리셋(프로바이더 발 초기화)이 아니다. 반환: 남은 리셋."""
    kept = []
    for r in resets:
        hit = None
        if r.get("ts") is not None:
            for pc in changes:
                if pc.get("ts") is not None and abs(r["ts"] - pc["ts"]) <= PLAN_CHANGE_SLACK:
                    hit = pc
                    break
        if hit is None:
            kept.append(r)
        else:
            hit["drops"].append({"win": win, "from": r["from"], "to": r["to"]})
    return kept


RESET_EARLY_SLACK = 600   # 초 — 예정 리셋 시각보다 이만큼 이르면 프로바이더 발 조기 리셋으로 본다


def _rewound(prev, row) -> bool:
    """창 끝 시각이 뒤로 갔다 = 새 창이 아니라 옛 창의 값이 늦게 들어온 것. 진짜 리셋(정상·조기 모두)의
    새 창은 리셋 시점 + 창 길이에 끝나므로 항상 이전 창보다 늦게 끝난다."""
    a, b = prev["resets_at"], row["resets_at"]
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and b < a - RESET_EARLY_SLACK


def detect_resets(c, provider: str, win: str, frm: float | None, account: str = "") -> list:
    """pct 하락 = 창 리셋. 직전 행의 resets_at보다 이르면 early(프로바이더가 임의 초기화한 경우).
    만료 스냅샷 행은 건너뛰고, 창 끝 시각이 뒤로 간 하락은 리셋이 아니다.
    반환: [{ts, from, to, early}] — 범위 안의 하락만 (직전 기준행은 범위 밖에서 이어받는다)."""
    if frm is not None:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frm))
        prev = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND account=? AND win=?"
                         f" AND ts < ? AND {_LIVE} ORDER BY id DESC LIMIT 1",
                         (provider, account, win, cutoff)).fetchone()
        rows = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND account=? AND win=?"
                         f" AND ts >= ? AND {_LIVE} ORDER BY id", (provider, account, win, cutoff))
    else:
        prev = None
        rows = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND account=? AND win=?"
                         f" AND {_LIVE} ORDER BY id", (provider, account, win))
    out = []
    for r in rows:
        if prev is not None and _rewound(prev, r):
            continue   # 옛 창 값 — 다음 행의 기준으로도 쓰지 않는다
        if prev is not None and r["pct"] < prev["pct"]:
            ts = to_epoch(r["ts"])
            early = (isinstance(prev["resets_at"], (int, float)) and ts is not None
                     and ts < prev["resets_at"] - RESET_EARLY_SLACK)
            out.append({"ts": int(ts) if ts else None, "from": prev["pct"], "to": r["pct"],
                        "early": bool(early)})
        prev = r
    return out


def _history_series(c, provider: str, account: str, frm: float | None, span_days: int) -> dict:
    """한 계정(provider+account)의 {windows, resets, plan_changes}."""
    changes = plan_changes(c, provider, frm, account)
    out = {"windows": {}, "resets": {}, "plan_changes": changes}
    for row in c.execute("SELECT DISTINCT win FROM usage_history WHERE provider=? AND account=?", (provider, account)):
        win = row["win"]
        series = []
        if frm is not None:
            cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frm))
            carry = c.execute(
                f"SELECT ts, pct FROM usage_history WHERE provider=? AND account=? AND win=? AND ts < ? AND {_LIVE}"
                " ORDER BY id DESC LIMIT 1", (provider, account, win, cutoff)).fetchone()
            if carry:
                series.append([int(frm), carry["pct"]])
            pred, args = f"AND ts >= ? AND {_LIVE}", (provider, account, win, cutoff)
        else:
            pred, args = f"AND {_LIVE}", (provider, account, win)
        if span_days <= 14:
            q = f"SELECT ts, pct FROM usage_history WHERE provider=? AND account=? AND win=? {pred} ORDER BY id"
        elif span_days <= 92:
            q = (f"SELECT MAX(id) _last, ts, pct FROM usage_history WHERE provider=? AND account=? AND win=? {pred}"
                 f" GROUP BY strftime('%Y-%m-%dT%H', ts) ORDER BY ts")
        else:
            q = (f"SELECT MAX(id) _last, ts, pct FROM usage_history WHERE provider=? AND account=? AND win=? {pred}"
                 f" GROUP BY date(ts) ORDER BY ts")
        for r in c.execute(q, args):
            epoch = to_epoch(r["ts"])
            if epoch is not None:
                series.append([int(epoch), r["pct"]])
        if series:
            out["windows"][win] = series
        resets = _claim_plan_drops(detect_resets(c, provider, win, frm, account), changes, win)
        if resets:
            out["resets"][win] = resets
    return out


def _has_history(slot: dict) -> bool:
    return accounts.is_subscription(slot.get("account", ""))


def history(c, days: int = 30) -> dict:
    """한도 % 시계열 — {claude: [HistSlot, …], codex: [HistSlot, …], from, to}. 프로바이더마다 마지막 탐색 결과의
    슬롯(폴더 이름순)이 한 항목씩이고, HistSlot = {key, label, account, windows: {win: [[epoch, pct], …]},
    resets: {win: […]}, plan_changes: […]}. 슬롯의 현재 계정이 구독 이메일이면 그 이메일의 행을 읽고, 아니면
    (API·미상·계정이 바뀐 직후) 빈 시리즈다. 같은 이메일 슬롯이 둘이면 둘 다 같은 시리즈를 가리킨다.
    plan·email·live는 호출측(app)이 현재 스냅샷으로 붙인다(api는 슬롯 계정에서 여기서 채운다).
    변화 시점만 저장돼 있으므로 범위 직전 carry-in 1행을 붙여 계단선 시작 레벨을 준다.
    다운샘플은 **실제 데이터 폭**(전 계정 공통) 기준(≤14일 원본, ≤92일 시간별, 그 이상 일별) — 요청 범위가
    길어도 이력이 짧으면 원본 그대로. 버킷은 마지막 행(MAX(id)의 bare column, SQLite 보장) —
    MAX(ts)+MAX(pct)처럼 다른 행의 값이 짝지어지지 않게. 리셋 감지는 항상 원본 행 기준."""
    now = time.time()
    days = max(0, min(int(days or 0), 3660))
    frm = now - days * 86_400 if days else None
    first = to_epoch((c.execute("SELECT MIN(ts) t FROM usage_history").fetchone() or {"t": None})["t"])
    span_start = max(first, frm) if (first is not None and frm is not None) else (first if first is not None else now)
    span_days = max(1, int((now - span_start) // 86_400) + 1)
    out = {"from": int(frm) if frm else None, "to": int(now)}
    for provider in PROVIDERS:
        with _lock:
            slots = list(_slots[provider])
        items = []
        for slot in slots:
            account = slot.get("account", "")
            series = (_history_series(c, provider, account, frm, span_days) if _has_history(slot)
                      else {"windows": {}, "resets": {}, "plan_changes": []})
            items.append({"key": slot["key"], "label": slot["label"], "account": account,
                          "api": accounts.is_api(account), **series})
        out[provider] = items
    return out


def _record(name: str, data: dict, account: str = "", plan_live: bool = True) -> None:
    """수집 성공값을 히스토리에 append — 실패해도 수집 결과에는 영향 없다.
    plan_live=False(폴백으로 얻은 표시용 플랜)면 플랜 이력은 건드리지 않는다."""
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())   # 같은 시각으로 — 플랜 변경과 그 하락이 짝지어지게
        with db.tx() as c:
            record_history(c, name, data["windows"], ts, account=account)
            record_plan(c, name, data.get("plan") if plan_live else "", ts, account=account)
    except Exception:
        log.exception("usage 히스토리 append 실패")


def _store_ok(provider: str, slot: dict, data: dict, recorded: set) -> None:
    """수집 성공값을 스냅샷에 두고, 구독 이메일 슬롯이면 이력에 append(같은 이메일은 한 회차 1회)."""
    key, account = slot["key"], slot.get("account", "")
    plan_live = bool(data.pop("plan_live", True))      # Codex는 플랜이 rateLimits 응답에서 바로 오므로 항상 조회값
    data.update(key=key, label=slot["label"], account=account, error="")
    with _lock:
        _snap[provider][key] = data
    _st(key)["error"] = ""
    if accounts.is_subscription(account) and account not in recorded:
        recorded.add(account)
        _record(provider, data, account, plan_live)


def _store_error(provider: str, slot: dict, message: str, with_login: bool = False) -> None:
    """실패 — 마지막 성공값은 유지하고 error만 붙인다(이전 성공값이 있어도 오류 표시 없이 옛 값만 보이지 않게)."""
    st = _st(slot["key"])
    st["error"] = message
    with _lock:
        snap = _snap[provider].get(slot["key"])
        if snap:
            snap["error"] = message
            if with_login and st["login_seen"]:
                snap["login_ends"] = st["login_ends"]


def _refresh_claude_slot(slot: dict, recorded: set) -> bool:
    """슬롯 하나를 수집해 스냅샷·히스토리에 반영(다른 슬롯에 영향 없음). 반환: 스냅샷이 새로 갱신됐는지."""
    st = _st(slot["key"])
    try:
        data = {**collect_claude(slot)}
        data.setdefault("login_ends", st["login_ends"])
        _store_ok("claude", slot, data, recorded)
        return True
    except UsageError as exc:
        log.info("usage claude '%s' 건너뜀: %s", slot["label"], exc)
        _store_error("claude", slot, str(exc), with_login=True)
    except Exception:
        log.exception("usage claude '%s' 오류", slot["label"])
        _store_error("claude", slot, "내부 오류 — 허브 로그 확인")
    return False


def _refresh_codex_slot(slot: dict, recorded: set) -> bool:
    try:
        _store_ok("codex", slot, {**collect_codex(slot)}, recorded)
        return True
    except UsageError as exc:
        log.info("usage codex '%s' 건너뜀: %s", slot["label"], exc)
        _store_error("codex", slot, str(exc))
    except Exception:
        log.exception("usage codex '%s' 오류", slot["label"])
        _store_error("codex", slot, "내부 오류 — 허브 로그 확인")
    return False


def refresh(providers=PROVIDERS) -> dict:
    """주어진 프로바이더(기본 둘 다)를 한 번 수집. 매번 슬롯 폴더를 다시 탐색해 이름순으로 수집한다(한 슬롯의
    실패가 다른 슬롯을 막지 않는다). API 로그인 슬롯은 수집·깨우기·이력을 하지 않는다. 실패한 슬롯은 마지막 성공값을
    유지하고 error만 붙인다."""
    changed = False
    for name in providers:
        if name not in PROVIDERS:
            raise KeyError(name)
        with _lock:
            before = [s["key"] for s in _slots[name]]
        slots = _rescan(name)
        changed = changed or before != [s["key"] for s in slots]
        recorded: set = set()
        run = _refresh_claude_slot if name == "claude" else _refresh_codex_slot
        for slot in slots:
            if accounts.is_api(slot.get("account", "")):
                continue
            changed = run(slot, recorded) or changed
    if changed:
        _persist()
    return snapshot()


def _slot_view(provider: str, slot: dict, d: dict | None, st: dict, now: float) -> dict:
    account = slot.get("account", "")
    base = {"key": slot["key"], "label": slot["label"], "account": account,
            "email": account if accounts.is_subscription(account) else "", "api": accounts.is_api(account)}
    if base["api"]:
        return {**base, "plan": "", "windows": [], "extras": [], "updated": "", "error": API_SLOT_ERROR,
                "login_ends": None}
    if d and d.get("account", account) != account:
        d = None       # 폴더의 로그인이 바뀌었다 — 이전 계정의 값을 새 계정의 것처럼 내지 않는다
    if d:
        return {**base, "plan": d.get("plan") or "",
                "windows": [{**w, "left": format_remaining(w.get("resets_at"), now)} for w in d.get("windows") or []],
                "extras": d.get("extras") or [], "updated": d.get("updated") or "",
                "error": d.get("error") or "", "login_ends": d.get("login_ends")}
    return {**base, "plan": "", "windows": [], "extras": [], "updated": "", "error": st["error"] or "수집 전",
            "login_ends": st["login_ends"] if provider == "claude" else None}


def snapshot() -> dict:
    """현황 KPI용 — {claude: [Slot, …], codex: [Slot, …]} (폴더 이름순, 슬롯 없으면 빈 목록).
    Slot = {key(폴더 절대경로), label, account, email(구독 이메일이 아니면 ""), api, plan, windows, extras, updated,
    error, login_ends(Codex는 None)}. 남은 시간은 조회 시점 기준으로 다시 계산한다. 아직 수집하지 못한 슬롯도
    error(사유 또는 "수집 전")와 함께 넣고, API 슬롯은 api=True·windows=[]·error="API 로그인 — 구독 한도 없음"이다."""
    now = time.time()
    with _lock:
        slots = {p: list(_slots[p]) for p in PROVIDERS}
    states = {s["key"]: _st(s["key"]) for p in PROVIDERS for s in slots[p]}
    with _lock:
        return {p: [_slot_view(p, s, _snap[p].get(s["key"]), states[s["key"]], now) for s in slots[p]]
                for p in PROVIDERS}


def poll_intervals() -> dict:
    """프로바이더별 수집 주기(초) — 설정에서 매번 읽는다."""
    return {"claude": CFG.usage_poll_sec, "codex": CFG.usage_codex_poll_sec}


def due_providers(next_due: dict, now: float) -> list:
    """다음 수집 시각이 지난 프로바이더(PROVIDERS 순서)."""
    return [p for p in PROVIDERS if next_due.get(p, 0.0) <= now]


def loop(sleep=time.sleep, clock=time.monotonic):
    load_persisted()
    next_due = {p: 0.0 for p in PROVIDERS}      # 첫 회차는 전부 즉시
    while True:
        started = clock()
        due = due_providers(next_due, started)
        intervals = poll_intervals()
        for p in due:
            next_due[p] = started + intervals[p]     # 시도 시작 기준 — 느린 RPC가 주기를 밀지 않고, 실패해도 같다
        if due:
            try:
                refresh(tuple(due))
            except Exception:
                log.exception("usage loop 오류")
        sleep(max(0.0, min(next_due.values()) - clock()))
