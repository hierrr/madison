"""Claude Code·Codex 구독 한도 — 허브가 직접 수집한다(외부 도구·파일 의존 없음).

- Claude: Claude Code가 macOS 키체인("Claude Code-credentials") 또는 ~/.claude/.credentials.json에 두는 OAuth
  액세스 토큰으로 읽기 전용 엔드포인트 셋을 조회한다 — /api/oauth/usage(한도 창), /api/oauth/profile(플랜 표시명
  organization.plan_display_name·조직 uuid), /api/oauth/organizations/{uuid}/prepaid/credits(사용 크레딧 잔액).
  자격증명의 subscriptionType은 로그인 시점 값이라 갱신되지 않으므로 플랜에 쓰지 않는다. 토큰·조직 uuid는
  메모리에만 두고 기록하지 않는다.
  만료·거절(401)이면 이번 회차는 건너뛴다 — Claude Code를 쓰면 토큰이 갱신되어 다음 회차에 회복된다(자체 갱신은
  세션·훅을 유발하므로 하지 않는다).
- Codex: `codex app-server --stdio` JSON-RPC(initialize → initialized → account/rateLimits/read). 실패하면
  ~/.codex/sessions 최신 전사본의 token_count 스냅샷으로 대체.
한도는 계정 단위라 허브 기기 하나에서 읽으면 된다.
"""
import hashlib
import http.client
import json
import logging
import math
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from . import db
from .config import CFG

log = logging.getLogger("madison.usage")
_STORE_KEY = "usage.snapshot"     # settings 테이블에 마지막 성공값 보존 — 허브 재시작 직후 빈 타일 방지

PROVIDERS = ("claude", "codex")
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_CREDITS_URL = "https://api.anthropic.com/api/oauth/organizations/{org}/prepaid/credits"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

_lock = threading.Lock()
_snap: dict = {p: None for p in PROVIDERS}
_claude_backoff_until = 0.0
CLAUDE_BACKOFF_SEC = 600     # 429(과호출) 뒤 쉬는 시간 — 같은 계정을 다른 도구도 조회할 수 있다
CODEX_BACKOFF_SEC = 300      # Codex RPC 실패 뒤 app-server를 다시 띄우기까지 쉬는 시간 — 고장난 동안 매 분 프로세스·전사본 스캔을 반복하지 않는다
# RPC 백오프 상태 — until(epoch)까지는 codex_rpc를 부르지 않고, reason은 마지막 실패 사유(오류 문구를 창 내내 같게 유지)
_codex_backoff: dict = {"until": 0.0, "reason": ""}
CLAUDE_PROFILE_TTL = 3600    # 초 — 플랜 표시명은 거의 안 바뀐다
CLAUDE_CREDITS_TTL = 600     # 초 — 크레딧 잔액 재조회 간격
CLAUDE_AUX_RETRY_SEC = 600   # 초 — 프로필·크레딧 조회가 실패(429 포함)한 뒤 다시 시도하기까지 쉬는 시간
# 부가 정보 캐시 — fp는 토큰 지문(메모리 전용). 계정이 바뀌면 다른 계정의 값을 내지 않도록 비운다.
_claude_profile_cache: dict = {"at": 0.0, "plan": None, "org": "", "fp": "", "failed_at": 0.0}
_claude_credits_cache: dict = {"at": 0.0, "entry": None, "fp": "", "failed_at": 0.0}


class UsageError(RuntimeError):
    pass


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


# ── Claude ────────────────────────────────────────────

def claude_oauth() -> dict:
    raw = ""
    try:
        done = subprocess.run(["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
                              capture_output=True, text=True, timeout=10, check=False)
        raw = done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raw = ""
    if not raw:
        try:
            raw = CLAUDE_CREDENTIALS_FILE.read_text("utf-8")
        except OSError as exc:
            raise UsageError("Claude Code 자격증명 없음(키체인·~/.claude/.credentials.json)") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise UsageError("Claude Code 자격증명이 JSON이 아님") from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise UsageError("OAuth 액세스 토큰 없음")
    return oauth


def claude_get(url: str, token: str, label: str = "usage", timeout: float = 15, backoff: bool = False) -> dict:
    """Claude OAuth GET → JSON 객체. 401 처리는 공통. 429는 backoff=True(usage)일 때만 전체 호출 백오프를 건다."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "anthropic-beta": CLAUDE_OAUTH_BETA, "User-Agent": "madison-hub/usage"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise UsageError("토큰 거절(401) — Claude Code를 한 번 쓰면 갱신됨") from exc
        if exc.code == 429:
            if not backoff:
                raise UsageError(f"{label} HTTP 429") from exc
            global _claude_backoff_until
            _claude_backoff_until = time.time() + CLAUDE_BACKOFF_SEC
            raise UsageError(f"{label} HTTP 429 — {CLAUDE_BACKOFF_SEC // 60}분 뒤 재시도") from exc
        raise UsageError(f"{label} HTTP {exc.code}") from exc
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise UsageError(f"{label} 조회 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError(f"{label} 응답이 객체가 아님")
    return data


def claude_fetch(token: str, timeout: float = 15) -> dict:
    return claude_get(CLAUDE_USAGE_URL, token, "usage", timeout, backoff=True)


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


def _claude_profile(token: str, now: float) -> tuple[str, str]:
    """(플랜, 조직 uuid) — TTL 캐시. 실패하면 마지막 값 유지(모듈 캐시 → 스냅샷 플랜 → 빈 값).
    실패 뒤 CLAUDE_AUX_RETRY_SEC 동안은 다시 부르지 않는다. 토큰(계정)이 바뀌면 캐시를 비운다."""
    cache = _claude_profile_cache
    fp = _fingerprint(token)
    if cache["fp"] != fp:
        cache.update(at=0.0, plan=None, org="", fp=fp, failed_at=0.0)
    if cache["plan"] is not None and now - cache["at"] < CLAUDE_PROFILE_TTL:
        return cache["plan"], cache["org"]
    if not (cache["failed_at"] and now - cache["failed_at"] < CLAUDE_AUX_RETRY_SEC):
        try:
            plan, org = parse_profile(claude_get(CLAUDE_PROFILE_URL, token, "profile"))
            if plan:
                cache.update(at=now, plan=plan, org=org or cache["org"], failed_at=0.0)
                return plan, cache["org"]
            log.info("claude profile에 플랜 표시명 없음")
        except UsageError as exc:
            log.info("claude profile 조회 실패: %s", exc)
        except Exception as exc:        # 부가 정보가 한도 창 기록을 막지 않게
            log.info("claude profile 처리 오류: %s", type(exc).__name__)
        cache["failed_at"] = now
    if cache["plan"]:
        return cache["plan"], cache["org"]
    with _lock:
        prev = _snap.get("claude")
    return (str(prev.get("plan") or "") if isinstance(prev, dict) else ""), cache["org"]


def _claude_credits(token: str, org: str, enabled, now: float) -> dict | None:
    """사용 크레딧 extras 항목 — TTL 캐시, 실패하면 마지막 항목 유지(모듈 캐시 → 스냅샷 extras), 없으면 None.
    실패 뒤 CLAUDE_AUX_RETRY_SEC 동안은 다시 부르지 않는다. 토큰(계정)이 바뀌면 캐시를 비운다."""
    cache = _claude_credits_cache
    fp = _fingerprint(token)
    if cache["fp"] != fp:
        cache.update(at=0.0, entry=None, fp=fp, failed_at=0.0)
    entry = None
    if cache["entry"] is not None and now - cache["at"] < CLAUDE_CREDITS_TTL:
        entry = cache["entry"]
    elif (org and time.time() >= _claude_backoff_until
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
        with _lock:
            prev = _snap.get("claude")
        for ex in (prev.get("extras") if isinstance(prev, dict) else None) or []:
            if isinstance(ex, dict) and ex.get("title") == "Credits":
                entry = ex
                break
    # on/off는 매 회차 usage 응답의 값을 쓴다(잔액 캐시와 별개로 갱신)
    return {**entry, "enabled": enabled if isinstance(enabled, bool) else None} if entry else None


def collect_claude(now=None) -> dict:
    if time.time() < _claude_backoff_until:
        raise UsageError("429 백오프 중")
    oauth = claude_oauth()
    token = str(oauth["accessToken"])
    usage = claude_fetch(token)
    windows = parse_claude(usage)
    if not windows:
        raise UsageError("usage 응답에 한도 창이 없음")
    # 플랜·크레딧은 부가 정보 — 실패해도 한도 창 기록은 막지 않는다. subscriptionType은 로그인 시점 값이라 쓰지 않는다.
    t = time.time()
    plan, org = _claude_profile(token, t)
    extra_usage = usage.get("extra_usage")
    enabled = extra_usage.get("is_enabled") if isinstance(extra_usage, dict) else None
    credits = _claude_credits(token, org, enabled, t)
    return {"plan": plan, "windows": _finish(windows, now), "extras": [credits] if credits else [],
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))}


# ── Codex ─────────────────────────────────────────────

def _codex_bin() -> str:
    from . import llm
    try:
        return llm.conf("summary")["codex_bin"]
    except Exception:
        return CFG.codex_bin


def codex_rpc(timeout: float = 20) -> dict:
    """`codex app-server --stdio`: initialize → initialized → account/rateLimits/read."""
    codex = _codex_bin()
    try:
        # launchd의 최소 PATH엔 nvm node가 없다 — codex(셔뱅 `env node`)의 디렉터리를 PATH 앞에 (llm.run과 동일)
        env = {**os.environ, "PATH": os.path.dirname(codex) + os.pathsep + os.environ.get("PATH", "")}
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
    """RPC(camelCase)·전사본(snake_case) credits 객체 → {balance, unlimited, has_credits}. 없거나 깨지면 None."""
    if not isinstance(raw, dict):
        return None
    balance = raw.get("balance")
    if balance is None or isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        balance = None
    has = raw.get("hasCredits", raw.get("has_credits"))
    unlimited = raw.get("unlimited")
    if balance is None and unlimited is not True:
        return None  # 쓸 만한 잔액도 무제한 표시도 없으면 폴백 후보에서 제외
    return {"balance": str(balance) if balance is not None else None,
            "unlimited": unlimited is True, "has_credits": has is True}


def parse_codex_rpc(result: dict) -> dict:
    """account/rateLimits/read 결과 → {windows, plan, credits, reset_credits}."""
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
    avail = credits.get("availableCount") if isinstance(credits, dict) else None
    return {"windows": windows, "plan": plan, "credits": cr_norm,
            "reset_credits": int(avail) if isinstance(avail, (int, float)) else None}


def codex_transcript_snapshot():
    """~/.codex/sessions 최신 전사본의 token_count.rate_limits — RPC 실패 시 대체.
    반환: (rate_limits, 관측 epoch) — 관측 시각은 이벤트 timestamp, 없으면 파일 mtime."""
    try:
        files = sorted(CODEX_SESSIONS_DIR.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files[:5]:
        latest, seen_at = None, None
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    pl = ev.get("payload") if isinstance(ev, dict) else None
                    if isinstance(pl, dict) and pl.get("type") == "token_count" and isinstance(pl.get("rate_limits"), dict):
                        latest, seen_at = pl["rate_limits"], to_epoch(ev.get("timestamp"))
            if latest and seen_at is None:
                seen_at = path.stat().st_mtime
        except OSError:
            continue
        if latest:
            return latest, seen_at
    return None


def parse_codex_transcript(rl: dict) -> dict:
    windows = []
    for slot in ("primary", "secondary"):
        win = rl.get(slot)
        if isinstance(win, dict):
            mins = win.get("window_minutes")
            w = _window(_window_title(mins), win.get("used_percent"), win.get("resets_at"),
                        float(mins) if isinstance(mins, (int, float)) else 1e9)
            if w:
                windows.append(w)
    return {"windows": windows, "plan": rl.get("plan_type"), "reset_credits": None,
            "credits": _normalize_credits(rl.get("credits"))}


def codex_fallback(exc: UsageError, now: float) -> dict:
    """RPC 실패 시 전사본 스냅샷 — 마지막 수집값보다 새 관측일 때만 쓴다. 전사본은 codex를 쓸 때만
    갱신되므로 대개 며칠~몇 주 묵은 값이고, 그대로 기록하면 %가 옛값으로 튀었다 돌아오며
    가짜 리셋(하락)이 잡힌다. 이미 만료된 창(resets_at 경과)도 버린다."""
    found = codex_transcript_snapshot()
    if found is None:
        raise UsageError(f"{exc}; 전사본 스냅샷도 없음") from exc
    rl, seen_at = found
    with _lock:
        last = to_epoch((_snap.get("codex") or {}).get("updated"))
    if last is not None and seen_at <= last:
        raise UsageError(f"{exc}; 전사본 스냅샷이 마지막 수집값보다 오래됨") from exc
    parsed = parse_codex_transcript(rl)
    parsed["windows"] = [w for w in parsed["windows"]
                         if not (isinstance(w["resets_at"], float) and w["resets_at"] <= now)]
    if not parsed["windows"]:
        raise UsageError(f"{exc}; 전사본 스냅샷의 창이 모두 만료됨") from exc
    return {**parsed, "seen_at": seen_at}


def collect_codex(now=None) -> dict:
    now = now or time.time()
    # 호출 때마다 RPC를 읽는다 — 수집 주기는 loop()의 프로바이더별 주기(USAGE_CODEX_POLL_SEC)가 단일 출처.
    # 단, RPC가 실패하면 CODEX_BACKOFF_SEC 동안은 app-server도 전사본 스캔도 하지 않는다(문구는 창 내내 동일).
    if now < _codex_backoff["until"]:
        raise UsageError(f"Codex 읽기 실패: {_codex_backoff['reason']} — {CODEX_BACKOFF_SEC // 60}분마다 재시도")
    try:
        parsed = {**parse_codex_rpc(codex_rpc()), "seen_at": now}
        if not parsed.get("windows"):
            # 응답은 왔지만 쓸 게 없으면 실패와 같다 — 백오프·전사본 대체도 똑같이 탄다
            raise UsageError("rateLimits/read 응답에 한도 창이 없음")
        _codex_backoff.update(until=0.0, reason="")
    except UsageError as exc:
        _codex_backoff.update(until=now + CODEX_BACKOFF_SEC, reason=str(exc))
        log.info("codex RPC 실패 — 전사본 대체 시도: %s", exc)
        parsed = codex_fallback(exc, now)
    if not parsed.get("windows"):
        raise UsageError("Codex 응답에 한도 창이 없음")
    plan = str(parsed.get("plan") or "").strip()
    extras = []
    # extras 순서(Reset credits → Credits)는 여기가 단일 출처 — 프런트는 목록 순서대로 그린다.
    if isinstance(parsed.get("reset_credits"), int):
        extras.append({"title": "Reset credits", "value": f"{parsed['reset_credits']} available",
                       "count": parsed["reset_credits"]})
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
    return {"plan": plan.capitalize() if plan else "", "windows": _finish(parsed["windows"], now),
            "extras": extras, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(parsed["seen_at"]))}


# ── 수집 루프·스냅샷 ─────────────────────────────────

def _persist():
    try:
        with _lock:
            payload = json.dumps(_snap, ensure_ascii=False)
        with db.tx() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_STORE_KEY, payload))
    except Exception:
        log.exception("usage 스냅샷 저장 실패")


def load_persisted():
    try:
        with db.tx() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (_STORE_KEY,)).fetchone()
        data = json.loads(row["value"]) if row else None
    except Exception:
        return
    if isinstance(data, dict):
        with _lock:
            for p in PROVIDERS:
                if isinstance(data.get(p), dict) and not _snap[p]:
                    _snap[p] = data[p]


# 관측 시점에 이미 resets_at이 지난 행 = 묵은 스냅샷(예: 몇 주 전 codex 전사본). 그 %는 현재 창의 값이
# 아니므로 차트·리셋 감지에서 뺀다. 과거에 이렇게 쌓인 행은 지우지 않고 조회에서 거른다.
_LIVE = "(resets_at IS NULL OR resets_at > CAST(strftime('%s', ts) AS REAL))"


def record_history(c, provider: str, windows: list, ts: str | None = None) -> int:
    """창별 마지막 행과 pct가 다를 때만 usage_history에 append. 반환: 추가 행 수.
    변화만 쌓아 폴링 주기와 무관하게 용량을 억제한다(같은 pct로 돌아오는 리셋은 안 보이지만
    차트 해상도에선 무해). 조회는 계단선(step-after)으로 사이를 메운다. 이미 만료된 창은 기록하지 않는다."""
    ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    at = to_epoch(ts)
    n = 0
    for w in windows:
        if isinstance(w.get("resets_at"), (int, float)) and at is not None and w["resets_at"] <= at:
            continue
        last = c.execute(
            f"SELECT pct FROM usage_history WHERE provider=? AND win=? AND {_LIVE} ORDER BY id DESC LIMIT 1",
            (provider, w["title"])).fetchone()
        if last is None or last["pct"] != w["pct"]:
            c.execute("INSERT INTO usage_history (ts, provider, win, pct, resets_at) VALUES (?,?,?,?,?)",
                      (ts, provider, w["title"], w["pct"], w.get("resets_at")))
            n += 1
    return n


def record_plan(c, provider: str, plan, ts: str | None = None) -> bool:
    """플랜명이 마지막 행과 다를 때만 usage_plan_history에 append. 반환: 추가 여부.
    플랜이 바뀌면 창 구성·한도 자체가 달라 전후 %는 비교 대상이 아니므로 차트에 경계를 그리기 위해 남긴다.
    빈 플랜(응답에 없음)은 기록하지 않는다. 마지막 행은 ts 기준 — 손으로 넣은 과거 행(백필)이 id 순서와
    어긋나도 현재 플랜을 바르게 본다."""
    plan = str(plan or "").strip()
    if not plan:
        return False
    ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last = c.execute("SELECT plan FROM usage_plan_history WHERE provider=? ORDER BY ts DESC, id DESC LIMIT 1",
                     (provider,)).fetchone()
    if last is not None and last["plan"] == plan:
        return False
    c.execute("INSERT INTO usage_plan_history (ts, provider, plan) VALUES (?,?,?)", (ts, provider, plan))
    return True


def plan_changes(c, provider: str, frm: float | None) -> list:
    """범위 안의 플랜 변경 [{ts, from, to}] — 직전 플랜은 범위 밖 마지막 행에서 이어받는다.
    이전 행이 없는 첫 관측은 변경이 아니다."""
    if frm is not None:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frm))
        prev = c.execute("SELECT plan FROM usage_plan_history WHERE provider=? AND ts < ?"
                         " ORDER BY ts DESC, id DESC LIMIT 1", (provider, cutoff)).fetchone()
        rows = c.execute("SELECT ts, plan FROM usage_plan_history WHERE provider=? AND ts >= ? ORDER BY ts, id",
                         (provider, cutoff))
    else:
        prev = None
        rows = c.execute("SELECT ts, plan FROM usage_plan_history WHERE provider=? ORDER BY ts, id", (provider,))
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


def detect_resets(c, provider: str, win: str, frm: float | None) -> list:
    """pct 하락 = 창 리셋. 직전 행의 resets_at보다 이르면 early(프로바이더가 임의 초기화한 경우).
    만료 스냅샷 행은 건너뛰고, 창 끝 시각이 뒤로 간 하락은 리셋이 아니다.
    반환: [{ts, from, to, early}] — 범위 안의 하락만 (직전 기준행은 범위 밖에서 이어받는다)."""
    if frm is not None:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frm))
        prev = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND win=?"
                         f" AND ts < ? AND {_LIVE} ORDER BY id DESC LIMIT 1", (provider, win, cutoff)).fetchone()
        rows = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND win=?"
                         f" AND ts >= ? AND {_LIVE} ORDER BY id", (provider, win, cutoff))
    else:
        prev = None
        rows = c.execute("SELECT ts, pct, resets_at FROM usage_history WHERE provider=? AND win=?"
                         f" AND {_LIVE} ORDER BY id", (provider, win))
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


def history(c, days: int = 30) -> dict:
    """한도 % 시계열 — {provider: {windows: {win: [[epoch, pct], …]}, resets: {win: […]}, plan_changes: […]}, from, to}.
    변화 시점만 저장돼 있으므로 범위 직전 carry-in 1행을 붙여 계단선 시작 레벨을 준다.
    다운샘플은 **실제 데이터 폭** 기준(≤14일 원본, ≤92일 시간별, 그 이상 일별) — 요청 범위가
    길어도 이력이 짧으면 원본 그대로. 버킷은 마지막 행(MAX(id)의 bare column, SQLite 보장) —
    MAX(ts)+MAX(pct)처럼 다른 행의 값이 짝지어지지 않게. 리셋 감지는 항상 원본 행 기준."""
    now = time.time()
    days = max(0, min(int(days or 0), 3660))
    frm = now - days * 86_400 if days else None
    first = to_epoch((c.execute("SELECT MIN(ts) t FROM usage_history").fetchone() or {"t": None})["t"])
    span_start = max(first, frm) if (first is not None and frm is not None) else (first if first is not None else now)
    span_days = max(1, int((now - span_start) // 86_400) + 1)
    out = {p: {"windows": {}, "resets": {}, "plan_changes": []} for p in PROVIDERS}
    for provider in PROVIDERS:
        changes = plan_changes(c, provider, frm)
        out[provider]["plan_changes"] = changes
        for row in c.execute("SELECT DISTINCT win FROM usage_history WHERE provider=?", (provider,)):
            win = row["win"]
            series = []
            if frm is not None:
                cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(frm))
                carry = c.execute(
                    f"SELECT ts, pct FROM usage_history WHERE provider=? AND win=? AND ts < ? AND {_LIVE}"
                    " ORDER BY id DESC LIMIT 1", (provider, win, cutoff)).fetchone()
                if carry:
                    series.append([int(frm), carry["pct"]])
                pred, args = f"AND ts >= ? AND {_LIVE}", (provider, win, cutoff)
            else:
                pred, args = f"AND {_LIVE}", (provider, win)
            if span_days <= 14:
                q = f"SELECT ts, pct FROM usage_history WHERE provider=? AND win=? {pred} ORDER BY id"
            elif span_days <= 92:
                q = (f"SELECT MAX(id) _last, ts, pct FROM usage_history WHERE provider=? AND win=? {pred}"
                     f" GROUP BY strftime('%Y-%m-%dT%H', ts) ORDER BY ts")
            else:
                q = (f"SELECT MAX(id) _last, ts, pct FROM usage_history WHERE provider=? AND win=? {pred}"
                     f" GROUP BY date(ts) ORDER BY ts")
            for r in c.execute(q, args):
                epoch = to_epoch(r["ts"])
                if epoch is not None:
                    series.append([int(epoch), r["pct"]])
            if series:
                out[provider]["windows"][win] = series
            resets = _claim_plan_drops(detect_resets(c, provider, win, frm), changes, win)
            if resets:
                out[provider]["resets"][win] = resets
    return {**out, "from": int(frm) if frm else None, "to": int(now)}


def refresh(providers=PROVIDERS) -> dict:
    """주어진 프로바이더(기본 둘 다)를 한 번 수집. 실패한 쪽은 마지막 성공값을 유지하고 error만 붙인다."""
    changed = False
    collectors = {"claude": collect_claude, "codex": collect_codex}
    for name in providers:
        fn = collectors[name]
        try:
            data = fn()
            data["error"] = ""
            with _lock:
                _snap[name] = data
            changed = True
            try:
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())   # 같은 시각으로 — 플랜 변경과 그 하락이 짝지어지게
                with db.tx() as c:
                    record_history(c, name, data["windows"], ts)
                    record_plan(c, name, data.get("plan"), ts)
            except Exception:
                log.exception("usage 히스토리 append 실패")
        except UsageError as exc:
            log.info("usage %s 건너뜀: %s", name, exc)
            with _lock:
                if _snap[name]:
                    _snap[name]["error"] = str(exc)
        except Exception:
            log.exception("usage %s 오류", name)
    if changed:
        _persist()
    return snapshot()


def snapshot() -> dict:
    """현황 KPI용 — 남은 시간은 조회 시점 기준으로 다시 계산한다."""
    now = time.time()
    with _lock:
        out = {}
        for p in PROVIDERS:
            d = _snap[p]
            if not d:
                out[p] = None
                continue
            out[p] = {**d, "windows": [
                {**w, "left": format_remaining(w.get("resets_at"), now)} for w in d["windows"]]}
    return out


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
