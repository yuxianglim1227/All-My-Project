"""Single-owner login for the studio: scrypt password, optional TOTP, server-side sessions, brute-force limits.

Everything here is standard library. Secrets are never logged; the session cookie holds a random token and the
database holds only its SHA-256, so a stolen database copy does not log anyone in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Callable
from urllib.parse import quote

from ..store import Store
from ..timeutil import iso, parse_iso, utcnow

PASSWORD_KEY = "studio.password"
TOTP_KEY = "studio.totp_secret"
TOTP_LAST_KEY = "studio.totp_last_step"
COOKIE = "studio_session"
MIN_PASSWORD = 10

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_sessions (
  token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, user_agent TEXT NOT NULL DEFAULT ''
);
"""


class AuthError(ValueError):
    """Shown to the owner as-is (never contains a secret)."""


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **_SCRYPT, salt=base64.b64encode(salt).decode(), digest=base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, want = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters (a short sentence is easiest to remember).")
    if password.strip() != password:
        raise AuthError("The password must not start or end with a space.")


# ---------------------------------------------------------------------------
# TOTP (RFC 6238, SHA-1, 6 digits, 30 s) - works with Google Authenticator, Microsoft Authenticator, 1Password, ...
# ---------------------------------------------------------------------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def totp_uri(secret: str, account: str = "owner", issuer: str = "Social Studio") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


# ---------------------------------------------------------------------------
# Brute-force limiter (in memory: resets on restart, which is fine for one owner)
# ---------------------------------------------------------------------------
class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_seconds: int = 900, clock: Callable[[], float] = time.monotonic):
        self.max, self.window, self.clock = max_failures, window_seconds, clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str) -> deque[float]:
        q, cutoff = self._fails[key], self.clock() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked_for(self, key: str) -> int:
        """Seconds until `key` may try again (0 = allowed)."""
        with self._lock:
            q = self._trim(key)
            return 0 if len(q) < self.max else max(1, int(q[0] + self.window - self.clock()))

    def failed(self, key: str) -> None:
        with self._lock:
            self._trim(key).append(self.clock())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


# ---------------------------------------------------------------------------
# The owner account + sessions
# ---------------------------------------------------------------------------
class Auth:
    def __init__(self, store: Store, *, session_days: int = 30, clock: Callable[[], float] = time.time):
        self.store, self.session_days, self.clock = store, session_days, clock
        self.limiter = LoginLimiter()
        store._exec(SCHEMA.strip())

    # -- setup -----------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.store.kv_get(PASSWORD_KEY))

    @property
    def totp_enabled(self) -> bool:
        return bool(self.store.kv_get(TOTP_KEY))

    def set_password(self, password: str) -> None:
        check_password_strength(password)
        self.store.kv_set(PASSWORD_KEY, hash_password(password))
        self.revoke_all()  # a new password logs every device out

    def enable_totp(self, secret: str, code: str) -> None:
        if not self._totp_ok(secret, code, remember=False):
            raise AuthError("That code doesn't match. Check the phone's clock and type the current 6-digit code.")
        self.store.kv_set(TOTP_KEY, secret)

    def disable_totp(self) -> None:
        self.store.kv_set(TOTP_KEY, "")

    def _totp_ok(self, secret: str, code: str, *, remember: bool = True) -> bool:
        code = "".join(ch for ch in code if ch.isdigit())
        if len(code) != 6:
            return False
        now_step = int(self.clock() // 30)
        last = int(self.store.kv_get(TOTP_LAST_KEY, "-1") or -1)
        for step in (now_step - 1, now_step, now_step + 1):  # +-30 s clock drift
            if hmac.compare_digest(totp_code(secret, step), code):
                if remember:
                    if step <= last:
                        return False  # replayed code
                    self.store.kv_set(TOTP_LAST_KEY, str(step))
                return True
        return False

    # -- login -----------------------------------------------------------------------
    def login(self, password: str, totp: str = "", *, client: str = "?", user_agent: str = "") -> str:
        """Return a new session token, or raise AuthError with a message safe to show."""
        wait = self.limiter.blocked_for(client)
        if wait:
            raise AuthError(f"Too many attempts. Try again in {wait // 60 + 1} minute(s).")
        stored = self.store.kv_get(PASSWORD_KEY)
        if not stored:
            raise AuthError("No password is set yet. On the server run: python -m socialagent studio-password")
        secret = self.store.kv_get(TOTP_KEY) or ""
        ok = verify_password(password, stored) and (not secret or self._totp_ok(secret, totp))
        if not ok:
            self.limiter.failed(client)
            raise AuthError("Wrong password" + (" or code." if secret else "."))
        self.limiter.succeeded(client)
        return self._new_session(user_agent)

    def _new_session(self, user_agent: str) -> str:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        self.store._exec("INSERT INTO studio_sessions(token_hash,created_at,expires_at,user_agent) VALUES(?,?,?,?)",
                         (_h(token), iso(now), iso(now + timedelta(days=self.session_days)), user_agent[:200]))
        return token

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        rows = self.store._rows("SELECT expires_at FROM studio_sessions WHERE token_hash=?", (_h(token),))
        if not rows:
            return False
        if parse_iso(rows[0]["expires_at"]) <= utcnow():
            self.logout(token)
            return False
        return True

    def logout(self, token: str | None) -> None:
        if token:
            self.store._exec("DELETE FROM studio_sessions WHERE token_hash=?", (_h(token),))

    def revoke_all(self) -> int:
        return self.store._exec("DELETE FROM studio_sessions").rowcount


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
