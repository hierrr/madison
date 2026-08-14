"""요청 주체 분류 — IMPLEMENTATION.md §6의 인증 규칙.

- local  : CF 헤더가 전혀 없는 루프백 연결 (허브 기기 자신) — admin 권한 포함
- admin  : Cloudflare Access JWT 검증 통과 (대시보드 경유)
- device : Bearer 기기 토큰
루프백이라는 사실만으로는 신뢰하지 않는다(터널이 127.0.0.1로 프록시하므로).
"""
import hashlib
import secrets
from datetime import datetime, timezone

import jwt as pyjwt
from fastapi import Request

from .config import CFG

_jwks_client = None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1")


def _has_cf_headers(request: Request) -> bool:
    return "cf-connecting-ip" in request.headers or "cf-ray" in request.headers


def is_local(request: Request) -> bool:
    return _is_loopback(request) and not _has_cf_headers(request)


def real_ip(request: Request) -> str:
    """IP 허용 목록용 실제 IP — CF 헤더는 루프백(터널) 연결에서 온 것만 신뢰(§8.1)."""
    if _is_loopback(request) and _has_cf_headers(request):
        return request.headers.get("cf-connecting-ip", "")
    return request.client.host if request.client else ""


def verify_access_jwt(request: Request) -> dict | None:
    """Cf-Access-Jwt-Assertion(또는 CF_Authorization 쿠키)을 팀 도메인 공개키로 검증.
    성공 시 claims(email 포함)를 반환 — 관리자 행위 주체 기록 등에 사용."""
    global _jwks_client
    if not CFG.cf_team_domain or not CFG.cf_aud:
        return None
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
    if not token:
        return None
    try:
        if _jwks_client is None:
            _jwks_client = pyjwt.PyJWKClient(
                f"https://{CFG.cf_team_domain}/cdn-cgi/access/certs",
                cache_keys=True, timeout=5)
        key = _jwks_client.get_signing_key_from_jwt(token)
        return pyjwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=CFG.cf_aud)
    except Exception:
        return None


def device_from_bearer(request: Request, c) -> dict | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    row = c.execute(
        "SELECT * FROM devices WHERE token_hash=? AND revoked=0",
        (token_hash(auth[7:].strip()),),
    ).fetchone()
    if row is None:
        return None
    if CFG.ip_allowlist:
        ip = real_ip(request)
        if not _is_loopback(request) or _has_cf_headers(request):
            if ip not in CFG.ip_allowlist:
                return None
    c.execute(
        "UPDATE devices SET last_seen_at=? WHERE id=?",
        (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), row["id"]),
    )
    return dict(row)


def classify_device(request: Request, c) -> dict | None:
    """DB 락이 필요한 부분만 — 기기 토큰 조회. (JWT 검증은 네트워크라 락 밖에서)"""
    device = device_from_bearer(request, c)
    if device:
        return {"kind": "device", "device": device, "email": None}
    return None


def classify_nodb(request: Request) -> dict:
    """DB 없이 판정 — 루프백/Access JWT. 네트워크(JWKS)가 관여하므로 락 밖에서 호출."""
    if is_local(request):
        return {"kind": "local", "device": None, "email": None}
    claims = verify_access_jwt(request)
    if claims:
        return {"kind": "admin", "device": None, "email": claims.get("email")}
    return {"kind": None, "device": None, "email": None}
