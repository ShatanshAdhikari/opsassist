"""Authentication and session lifecycle.

Session tokens are opaque and random. Only their SHA-256 hash is stored, so a database read
does not yield usable sessions. A fast hash is appropriate because tokens have 256 bits of
entropy and are not guessable, unlike passwords.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select, update

from opsassist.audit import record_event
from opsassist.config import Settings
from opsassist.db import SessionFactory, global_scope
from opsassist.errors import AuthenticationRequired, RateLimited, ValidationFailed
from opsassist.models import Session, User, utcnow

MIN_PASSWORD_LENGTH = 12
_LAST_SEEN_RESOLUTION = timedelta(minutes=1)
_LIMITER_MAX_KEYS = 10_000

_hasher = PasswordHasher()
# Verified when the account does not exist, so response time does not reveal which emails exist.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(f"Password must have at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def _verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


class SlidingWindowLimiter:
    """In-process limiter. Per replica: replace with a shared store before scaling out."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events[key]
        while events and events[0] <= now - self._window:
            events.popleft()
        if not events:
            del self._events[key]
            return deque()
        return events

    def check(self, key: str) -> None:
        now = time.monotonic()
        events = self._prune(key, now)
        if len(events) >= self._limit:
            retry_after = max(1, int(events[0] + self._window - now) + 1)
            raise RateLimited(
                "Too many attempts. Try again later.", retry_after_seconds=retry_after
            )

    def hit(self, key: str) -> None:
        now = time.monotonic()
        if len(self._events) >= _LIMITER_MAX_KEYS:
            for existing in list(self._events):
                self._prune(existing, now)
        self._events[key].append(now)

    def reset(self, key: str) -> None:
        self._events.pop(key, None)


class AuthService:
    def __init__(self, settings: Settings, factory: SessionFactory) -> None:
        self._settings = settings
        self._factory = factory
        self._limiter = SlidingWindowLimiter(
            settings.login_max_failures, settings.login_window_seconds
        )

    async def login(self, email: str, password: str, client_ip: str) -> tuple[User, str, datetime]:
        """Return (user, raw session token, expiry). All failures look the same to the caller."""
        account_key = f"account:{email.strip().lower()}"
        ip_key = f"ip:{client_ip}"
        self._limiter.check(account_key)
        self._limiter.check(ip_key)

        async with global_scope(self._factory) as session:
            user = await session.scalar(
                select(User).where(func.lower(User.email) == email.strip().lower())
            )
            stored_hash = user.password_hash if user else _DUMMY_HASH
            valid = await asyncio.to_thread(_verify_password, stored_hash, password)
            if user is None or not valid or not user.is_active:
                self._limiter.hit(account_key)
                self._limiter.hit(ip_key)
                record_event(
                    session,
                    "auth.login",
                    actor_user_id=user.id if user else None,
                    outcome="failure",
                )
            else:
                self._limiter.reset(account_key)
                token = secrets.token_urlsafe(32)
                now = utcnow()
                expires_at = now + timedelta(hours=self._settings.session_absolute_hours)
                session.add(
                    Session(
                        user_id=user.id,
                        token_hash=hash_token(token),
                        created_at=now,
                        last_seen_at=now,
                        expires_at=expires_at,
                    )
                )
                record_event(session, "auth.login", actor_user_id=user.id)
                return user, token, expires_at
        # Raised after the scope closes so the failure audit event commits.
        raise AuthenticationRequired("Invalid email or password")

    async def authenticate(self, token: str) -> User:
        """Resolve a session token to an active user, or raise."""
        now = utcnow()
        idle_limit = timedelta(minutes=self._settings.session_idle_minutes)
        async with global_scope(self._factory) as session:
            row = (
                await session.execute(
                    select(Session, User)
                    .join(User, User.id == Session.user_id)
                    .where(Session.token_hash == hash_token(token))
                )
            ).first()
            if row is None:
                raise AuthenticationRequired("Not authenticated")
            db_session, user = row
            expired = (
                db_session.revoked_at is not None
                or db_session.expires_at <= now
                or db_session.last_seen_at + idle_limit <= now
            )
            if expired or not user.is_active:
                raise AuthenticationRequired("Session expired")
            if now - db_session.last_seen_at >= _LAST_SEEN_RESOLUTION:
                db_session.last_seen_at = now
            return user

    async def logout(self, token: str) -> None:
        async with global_scope(self._factory) as session:
            user_id: uuid.UUID | None = await session.scalar(
                update(Session)
                .where(Session.token_hash == hash_token(token), Session.revoked_at.is_(None))
                .values(revoked_at=utcnow())
                .returning(Session.user_id)
            )
            if user_id:
                record_event(session, "auth.logout", actor_user_id=user_id)
