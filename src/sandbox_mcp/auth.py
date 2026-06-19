"""Bearer-token check for /mcp and HMAC-signed URLs for the file side-channel."""

import base64
import hashlib
import hmac
import json
import time

from .config import config


def _b64e(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _b64d(s: bytes) -> bytes:
    pad = b"=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _key() -> bytes:
    k = config.SIGNING_KEY
    return bytes(k) if isinstance(k, (bytes, bytearray)) else str(k).encode()


def sign(payload: dict) -> str:
    """Return a tamper-proof, URL-safe token encoding `payload`."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = _b64e(raw)
    mac = hmac.new(_key(), body, hashlib.sha256).digest()
    return (body + b"." + _b64e(mac)).decode()


def verify(token: str) -> dict | None:
    """Validate a token's signature and expiry; return its payload or None."""
    try:
        body_s, mac_s = token.encode().split(b".", 1)
    except ValueError:
        return None
    expected = hmac.new(_key(), body_s, hashlib.sha256).digest()
    if not hmac.compare_digest(_b64e(expected), mac_s):
        return None
    try:
        payload = json.loads(_b64d(body_s))
    except Exception:
        return None
    if float(payload.get("exp", 0)) < time.time():
        return None
    return payload


def make_url(sandbox: str, path: str, op: str, ttl: int | None = None) -> str:
    """Build a signed file URL. `op` is "get" (download) or "put" (upload)."""
    ttl = ttl or config.URL_TTL
    token = sign(
        {"sb": sandbox, "p": path, "op": op, "exp": int(time.time()) + ttl}
    )
    return f"{config.PUBLIC_BASE_URL}/files/{op}/{token}"


class BearerAuth:
    """Pure-ASGI middleware: require `Authorization: Bearer <token>` under a prefix.

    Pure ASGI (not BaseHTTPMiddleware) so it never buffers the streaming /mcp
    responses. The /files endpoints are NOT covered here; they are gated by the
    HMAC signature embedded in their URL instead.
    """

    def __init__(self, app, token: str, protect_prefix: str = "/mcp"):
        self.app = app
        self.expected = f"Bearer {token}"
        self.prefix = protect_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != self.expected:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)
