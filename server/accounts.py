"""계정 식별 규칙의 허브 쪽 진입점.

규칙의 정본은 collector/account.py(수집기가 각 기기에 배포하는 파일)이고, 허브는 같은 파일을
importlib로 한 번만 불러 그대로 재노출한다 — 허브·수집기가 한 규칙을 쓰게 하는 단일 출처.
계정 문자열: 구독 로그인=소문자 이메일, API 로그인='api:<라벨>', 모르면 ''(계정 미상).
"""
import importlib.util
import os
import threading
import time
from pathlib import Path

from .config import REPO_ROOT

_ACCOUNT_PY = REPO_ROOT / "collector" / "account.py"
_mod = None
_mod_lock = threading.Lock()


def _rule():
    """collector/account.py 모듈 — 처음 부를 때 한 번만 로드(이후 캐시). 파일이 없으면 ImportError."""
    global _mod
    if _mod is None:
        with _mod_lock:
            if _mod is None:
                spec = importlib.util.spec_from_file_location("madison_collector_account", _ACCOUNT_PY)
                if spec is None or spec.loader is None:
                    raise ImportError(f"계정 규칙 모듈을 불러올 수 없음: {_ACCOUNT_PY}")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _mod = mod
    return _mod


def resolve(agent, environ, *, home=None, headless=False, cache_dir=None) -> str:
    return _rule().resolve(agent, environ, home=home, headless=headless, cache_dir=cache_dir)


def normalize(v) -> str:
    return _rule().normalize(v)


def is_subscription(a) -> bool:
    return _rule().is_subscription(a)


def is_api(a) -> bool:
    return _rule().is_api(a)


# 워커(허브가 띄우는 claude -p / codex exec)의 계정 — 호출마다 파일을 읽지 않게 TTL 캐시
WORKER_TTL = 300
_worker_cache: dict = {}
_worker_lock = threading.Lock()


def worker_account(agent: str) -> str:
    """허브 프로세스 env 기준(headless=True)의 계정 — 워커 사용량의 원장 귀속. TTL 300초 캐시.
    규칙 로드·해석이 실패해도 예외를 내지 않고 ''(미상) — 토큰 기록을 막지 않는다."""
    now = time.monotonic()
    with _worker_lock:
        hit = _worker_cache.get(agent)
        if hit and now - hit[0] < WORKER_TTL:
            return hit[1]
    try:
        value = resolve(agent, os.environ, headless=True)
    except Exception:
        value = ""
    with _worker_lock:
        _worker_cache[agent] = (now, value)
    return value


def clear_worker_cache():
    """테스트용 — 워커 계정 캐시 비우기."""
    with _worker_lock:
        _worker_cache.clear()
