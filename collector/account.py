#!/usr/bin/env python3
"""Account identity rule shared by the collector (each device) and the hub.

The hub loads this file with importlib, so it must stay standard-library only
and free of import-time side effects.  Public functions never raise: any
failure resolves to the empty string, which means "account unknown".

CLI:  python3 account.py [--ts] [--agent A] [--headless] [--cache-dir D]
The environment is read from os.environ (secrets are never taken from argv).
Prints "<ts>\\t<account>\\n" with --ts, otherwise "<account>\\n"; always exits 0.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_LEN = 200
API_PREFIX = "api:"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_CLOUD_FLAGS = (
    ("CLAUDE_CODE_USE_BEDROCK", "api:bedrock"),
    ("CLAUDE_CODE_USE_VERTEX", "api:vertex"),
    ("CLAUDE_CODE_USE_FOUNDRY", "api:foundry"),
)
_CACHE_VERSION = 2   # v2 added billingType; v1 caches are re-parsed
_WS = re.compile(r"\s+")


def sha8(s) -> str:
    """First 8 lowercase hex characters of the sha256 of a string."""
    return hashlib.sha256(str(s).encode("utf-8", "surrogatepass")).hexdigest()[:8]


def normalize(v) -> str:
    """Canonical account string: 'api:<label>' or a lowercase e-mail, else ''."""
    try:
        if not isinstance(v, str):
            return ""
        if v.startswith(API_PREFIX):
            rest = _WS.sub(" ", v[len(API_PREFIX):].strip())
            if not rest:
                return ""
            out = API_PREFIX + rest
        else:
            out = _WS.sub("", v).lower()
            if "@" not in out:
                return ""
        return out[:MAX_LEN]
    except Exception:
        return ""


def is_subscription(a) -> bool:
    return isinstance(a, str) and bool(a) and not a.startswith(API_PREFIX)


def is_api(a) -> bool:
    return isinstance(a, str) and a.startswith(API_PREFIX)


def resolve(agent, environ, *, home=None, headless=False, cache_dir=None) -> str:
    """Return the normalized account for an agent, or '' when unknown."""
    try:
        if agent == "claude-code":
            return normalize(_resolve_claude(environ, home, bool(headless), cache_dir))
        if agent == "codex-cli":
            return normalize(_resolve_codex(environ, home))
    except Exception:
        pass
    return ""


# ── helpers ────────────────────────────────────────────────

def _env(environ, key) -> str:
    v = environ.get(key)
    return v if isinstance(v, str) else ""


def _flag_on(value: str) -> bool:
    return value.strip().lower() in _TRUE_VALUES


def _home_dir(home) -> Path:
    return Path(home) if home is not None else Path.home()


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


# ── Claude ─────────────────────────────────────────────────

def _resolve_claude(environ, home, headless, cache_dir) -> str:
    for name, label in _CLOUD_FLAGS:
        if _flag_on(_env(environ, name)):
            return label

    token = _env(environ, "ANTHROPIC_AUTH_TOKEN")
    if token.strip():
        return API_PREFIX + sha8(token)

    login_path = None
    info = None

    def login_info():
        nonlocal login_path, info
        if login_path is None:
            config_dir = _env(environ, "CLAUDE_CONFIG_DIR")
            base = Path(config_dir) if config_dir else _home_dir(home)
            login_path = base / ".claude.json"
            info = _load_login(login_path, cache_dir)
        return info

    key = _env(environ, "ANTHROPIC_API_KEY")
    if key.strip():
        # A key the user declined in the interactive prompt is not in use.
        # Headless runs never show that prompt, so the key is used as-is.
        if headless:
            return API_PREFIX + sha8(key)
        data = login_info()
        if data is None or _sha256(key[-20:]) not in data["rejected"]:
            return API_PREFIX + sha8(key)

    data = login_info()
    if not data:
        return ""
    name = data["organizationName"].strip()
    api_account = API_PREFIX + name if name else ""
    # 1. claude_*  -> subscription.  2. api*  (api_team, api_individual, ...) -> API.
    # 3. empty or an organization type this rule has not seen -> billingType decides, so
    #    a new subscription type is never misread as API.  Console (API billing) logins
    #    carry no organizationType at all.  Both empty: older Claude Code wrote neither
    #    field, which has always meant a subscription login.
    org_type = data["organizationType"].strip().lower()
    if org_type.startswith("claude_"):
        return data["email"]
    if org_type.startswith("api"):
        return api_account
    billing = data["billingType"].strip().lower()
    if not billing or "subscription" in billing:
        return data["email"]
    return api_account


def _extract_login(obj) -> dict | None:
    if not isinstance(obj, dict):
        return None

    def text(d, k):
        v = d.get(k) if isinstance(d, dict) else None
        return v if isinstance(v, str) else ""

    oauth = obj.get("oauthAccount")
    rejected = []
    responses = obj.get("customApiKeyResponses")
    if isinstance(responses, dict) and isinstance(responses.get("rejected"), list):
        rejected = sorted({_sha256(t) for t in responses["rejected"] if isinstance(t, str)})
    return {
        "email": text(oauth, "emailAddress"),
        "organizationType": text(oauth, "organizationType"),
        "organizationName": text(oauth, "organizationName"),
        "billingType": text(oauth, "billingType"),
        "rejected": rejected,
    }


def _load_login(path: Path, cache_dir) -> dict | None:
    """Parsed subset of the Claude login file, or None if unreadable.

    The file can be several MB (project history), so the subset is cached by
    (mtime_ns, size, inode).  The cache holds only hashes of rejected key tails.
    """
    try:
        st = os.stat(path)
    except Exception:
        return None
    stamp = [st.st_mtime_ns, st.st_size, st.st_ino]

    cache_file = None
    if cache_dir:
        try:
            digest = _sha256(os.path.abspath(path))[:12]
            cache_file = Path(cache_dir) / f"claude-login-{digest}.json"
            cached = _read_cache(cache_file, stamp)
            if cached is not None:
                return cached
        except Exception:
            pass

    try:
        data = _extract_login(json.loads(Path(path).read_text(encoding="utf-8")))
    except Exception:
        return None
    if data is None:
        return None
    if cache_file is not None:
        _write_cache(cache_file, stamp, data)
    return data


def _read_cache(cache_file: Path, stamp) -> dict | None:
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or raw.get("version") != _CACHE_VERSION
            or raw.get("stamp") != stamp
        ):
            return None
        rejected = raw.get("rejected")
        data = {k: raw.get(k) for k in ("email", "organizationType", "organizationName", "billingType")}
        if (
            not all(isinstance(v, str) for v in data.values())
            or not isinstance(rejected, list)
            or not all(isinstance(r, str) for r in rejected)
        ):
            return None
        data["rejected"] = rejected
        return data
    except Exception:
        return None


def _write_cache(cache_file: Path, stamp, data: dict) -> None:
    tmp = None
    try:
        directory = cache_file.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=cache_file.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump({"version": _CACHE_VERSION, "stamp": stamp, **data}, f)
        os.replace(tmp, cache_file)
        tmp = None
    except Exception:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ── Codex ──────────────────────────────────────────────────

def _resolve_codex(environ, home) -> str:
    codex_home = _env(environ, "CODEX_HOME")
    base = Path(codex_home) if codex_home else _home_dir(home) / ".codex"
    auth = json.loads((base / "auth.json").read_text(encoding="utf-8"))
    if not isinstance(auth, dict):
        return ""

    api_key = auth.get("OPENAI_API_KEY")
    api_key = api_key if isinstance(api_key, str) and api_key.strip() else ""
    tokens = auth.get("tokens")
    if auth.get("auth_mode") == "apikey" or (api_key and not tokens):
        return API_PREFIX + sha8(api_key) if api_key else ""

    if not isinstance(tokens, dict):
        return ""
    claims = _jwt_payload(tokens.get("id_token"))
    if not claims:
        return ""
    email = claims.get("email")
    if not (isinstance(email, str) and email.strip()):
        profile = claims.get("https://api.openai.com/profile")
        email = profile.get("email") if isinstance(profile, dict) else ""
    return email if isinstance(email, str) else ""


def _jwt_payload(token) -> dict | None:
    """Decode a JWT payload without verifying the signature."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")).decode("utf-8"))
    return claims if isinstance(claims, dict) else None


# ── CLI ────────────────────────────────────────────────────

def _timestamp() -> str:
    # Same format as collector/report.sh's TS (UTC, milliseconds, trailing Z).
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    want_ts = False
    headless = False
    agent = "claude-code"
    cache_dir = None
    try:
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--ts":
                want_ts = True
            elif a == "--headless":
                headless = True
            elif a == "--agent" and i + 1 < len(args):
                i += 1
                agent = args[i]
            elif a == "--cache-dir" and i + 1 < len(args):
                i += 1
                cache_dir = args[i] or None
            i += 1
    except Exception:
        pass
    account = resolve(agent, os.environ, headless=headless, cache_dir=cache_dir)
    try:
        line = f"{_timestamp()}\t{account}\n" if want_ts else f"{account}\n"
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        code = 0
    sys.exit(code)
