"""User authentication for the control panel.

* Passwords: Argon2id (argon2-cffi defaults), rehashed when parameters change.
* Second factor: TOTP (RFC 6238), a code is accepted at most once.
* Sessions: opaque random tokens in an HTTP-only, SameSite=Strict cookie; only
  their SHA-256 is stored. Idle timeout plus absolute lifetime.
* CSRF: state-changing requests with a session cookie must echo the session's
  CSRF token in the X-CSRF-Token header (double submit, compared in constant time).
* Rate limiting: failed logins per username and per IP over a sliding window.
* Sensitive actions require a recent re-authentication (password + TOTP).
"""

import hmac
import secrets
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator.models import AppUser, LoginAttempt, UserSession
from pos_coordinator.security import hash_secret  # noqa: F401  (re-exported)

COOKIE = "pos_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_IDLE = timedelta(hours=12)
SESSION_MAX = timedelta(days=7)
REAUTH_WINDOW = timedelta(minutes=10)
FAIL_WINDOW = timedelta(minutes=15)
MAX_FAILS_PER_USER = 5
MAX_FAILS_PER_IP = 30
MIN_PASSWORD_LEN = 12

_hasher = PasswordHasher()
# Verified against when the user does not exist, so timing does not reveal usernames.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class AuthError(Exception):
    def __init__(self, reason: str, *, totp_required: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.totp_required = totp_required


class RateLimited(AuthError):
    pass


def normalize_username(u: str) -> str:
    return u.strip().lower()


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    if password.strip() != password or len(set(password)) < 5:
        raise AuthError("password is too simple")


def hash_password(password: str) -> str:
    validate_password(password)
    return _hasher.hash(password)


def verify_password(user: AppUser | None, password: str) -> bool:
    try:
        return _hasher.verify(user.password_hash if user else _DUMMY_HASH, password) and bool(user)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_totp(user: AppUser, code: str | None) -> bool:
    """Accept a current code once (window ±1 step for clock drift); store its counter."""
    if not user.totp_secret:
        return True
    if not code or not code.strip().isdigit():
        return False
    totp = pyotp.TOTP(user.totp_secret)
    now = datetime.now(UTC)
    for drift in (0, -1, 1):
        at = now + timedelta(seconds=30 * drift)
        if hmac.compare_digest(totp.at(at), code.strip()):
            counter = int(at.timestamp()) // 30
            if user.totp_last_counter is not None and counter <= user.totp_last_counter:
                return False  # replay
            user.totp_last_counter = counter
            return True
    return False


async def check_rate_limit(session: AsyncSession, username: str, ip: str | None) -> None:
    since = datetime.now(UTC) - FAIL_WINDOW
    fails_user = (
        await session.execute(
            select(func.count()).where(
                LoginAttempt.username == username,
                LoginAttempt.ok.is_(False),
                LoginAttempt.at > since,
            )
        )
    ).scalar_one()
    fails_ip = 0
    if ip:
        fails_ip = (
            await session.execute(
                select(func.count()).where(
                    LoginAttempt.ip == ip, LoginAttempt.ok.is_(False), LoginAttempt.at > since
                )
            )
        ).scalar_one()
    if fails_user >= MAX_FAILS_PER_USER or fails_ip >= MAX_FAILS_PER_IP:
        raise RateLimited("too many failed logins; try again later")


async def authenticate(
    session: AsyncSession, username: str, password: str, totp: str | None, ip: str | None
) -> AppUser:
    username = normalize_username(username)
    await check_rate_limit(session, username, ip)
    user = (
        await session.execute(select(AppUser).where(AppUser.username == username))
    ).scalar_one_or_none()
    if not verify_password(user, password) or user is None or user.disabled_at is not None:
        session.add(LoginAttempt(username=username, ip=ip, ok=False, reason="credentials"))
        await session.commit()
        raise AuthError("invalid username or password")
    if user.totp_secret and not totp:
        raise AuthError("second factor required", totp_required=True)
    if not verify_totp(user, totp):
        session.add(LoginAttempt(username=username, ip=ip, ok=False, reason="totp"))
        await session.commit()
        raise AuthError("invalid second factor", totp_required=True)
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    user.last_login_at = datetime.now(UTC)
    session.add(LoginAttempt(username=username, ip=ip, ok=True))
    return user


async def create_session(
    session: AsyncSession, user: AppUser, ip: str | None, user_agent: str | None
) -> tuple[str, UserSession]:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    s = UserSession(
        token_hash=hash_secret(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(24),
        expires_at=now + SESSION_MAX,
        last_seen_at=now,
        reauth_at=now,
        ip=ip,
        user_agent=(user_agent or "")[:300],
    )
    session.add(s)
    return token, s


async def load_session(session: AsyncSession, token: str) -> tuple[UserSession, AppUser] | None:
    row = (
        await session.execute(
            select(UserSession, AppUser)
            .join(AppUser, AppUser.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_secret(token))
        )
    ).first()
    if row is None:
        return None
    s, user = row
    now = datetime.now(UTC)
    if user.disabled_at is not None or s.expires_at <= now or now - s.last_seen_at > SESSION_IDLE:
        await session.delete(s)
        await session.commit()
        return None
    if now - s.last_seen_at > timedelta(minutes=1):  # avoid a write per request
        s.last_seen_at = now
    return s, user


def csrf_ok(s: UserSession, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(s.csrf_token, header)


def recently_reauthenticated(s: UserSession) -> bool:
    return datetime.now(UTC) - s.reauth_at <= REAUTH_WINDOW
