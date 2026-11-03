"""Secret generation, hashing and signing helpers (stdlib only)."""
import base64
import hashlib
import hmac
import secrets


def new_secret(prefix: str, nbytes: int = 32) -> str:
    return f"{prefix}_{secrets.token_urlsafe(nbytes)}"


def hash_secret(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def consteq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def hmac_b64url(secret: str, msg: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256)
    return _b64url(mac.digest())


def hmac_hex(secret: str, msg: bytes) -> str:
    """HMAC-SHA256 of ``msg`` keyed with ``secret`` (UTF-8), as lowercase hex (webhook signatures)."""
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def pkce_s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
