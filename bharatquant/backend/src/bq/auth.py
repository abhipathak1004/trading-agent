import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from bq.core import Clock, SystemClock
from bq.infra.audit import append_event

SESSION_SECONDS = 12 * 60 * 60
LOCK_SECONDS = 15 * 60
MAX_FAILURES = 5
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 256
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


class OwnerAlreadyConfigured(Exception):
    pass


class OwnerNotConfigured(Exception):
    pass


class InvalidCredentials(Exception):
    pass


class LoginLocked(Exception):
    pass


@dataclass(frozen=True)
class AuthenticatedSession:
    token: str
    csrf_token: str


def validate_password(password: str) -> None:
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"Password must contain {PASSWORD_MIN_LENGTH} to "
            f"{PASSWORD_MAX_LENGTH} characters."
        )


class OwnerAuth:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )

    def _now(self) -> int:
        instant = self.clock.now()
        if instant.utcoffset() is None:
            raise ValueError("Authentication clock must be timezone-aware.")
        return int(instant.timestamp())

    def is_configured(self) -> bool:
        with closing(self._connect()) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM owner_credentials WHERE owner_id = 1"
                ).fetchone()
                is not None
            )

    def setup(self, password: str) -> None:
        validate_password(password)
        if self.is_configured():
            raise OwnerAlreadyConfigured("Owner password is already configured.")
        now = self._now()
        password_hash = PASSWORD_HASHER.hash(password)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                if connection.execute(
                    "SELECT 1 FROM owner_credentials WHERE owner_id = 1"
                ).fetchone():
                    raise OwnerAlreadyConfigured(
                        "Owner password is already configured."
                    )
                connection.execute(
                    "INSERT INTO owner_credentials VALUES (1, ?)", (password_hash,)
                )
                append_event(
                    connection,
                    occurred_at=datetime.fromtimestamp(now, UTC),
                    actor="owner",
                    action="owner.password.configured",
                    object_type="owner",
                    object_id="owner:1",
                    payload={"source": "cli"},
                )

    def login(self, password: str) -> AuthenticatedSession:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            failed = False
            session: AuthenticatedSession | None = None
            with connection:
                now = self._now()
                credentials = connection.execute(
                    "SELECT password_hash FROM owner_credentials WHERE owner_id = 1"
                ).fetchone()
                if credentials is None:
                    raise OwnerNotConfigured("Owner password is not configured.")
                failures, locked_until = connection.execute(
                    "SELECT failures, locked_until FROM auth_throttle WHERE owner_id = 1"
                ).fetchone()
                if locked_until > now:
                    raise LoginLocked("Too many login attempts.")
                if locked_until:
                    failures = 0
                try:
                    PASSWORD_HASHER.verify(credentials[0], password)
                except VerifyMismatchError:
                    failures += 1
                    lock_end = now + LOCK_SECONDS if failures >= MAX_FAILURES else 0
                    connection.execute(
                        "UPDATE auth_throttle SET failures = ?, locked_until = ? "
                        "WHERE owner_id = 1",
                        (failures, lock_end),
                    )
                    failed = True
                    if lock_end:
                        append_event(
                            connection,
                            occurred_at=datetime.fromtimestamp(now, UTC),
                            actor="anonymous",
                            action="auth.login.locked",
                            object_type="owner",
                            object_id="owner:1",
                            payload={"reason": "failure_limit_reached"},
                        )
                else:
                    session = AuthenticatedSession(
                        token=secrets.token_urlsafe(32),
                        csrf_token=secrets.token_urlsafe(32),
                    )
                    connection.execute(
                        "DELETE FROM auth_sessions WHERE expires_at <= ?", (now,)
                    )
                    connection.execute(
                        "INSERT INTO auth_sessions VALUES (?, ?, ?)",
                        (
                            sha256(session.token.encode("ascii")).hexdigest(),
                            session.csrf_token,
                            now + SESSION_SECONDS,
                        ),
                    )
                    connection.execute(
                        "UPDATE auth_throttle SET failures = 0, locked_until = 0 "
                        "WHERE owner_id = 1"
                    )
                    append_event(
                        connection,
                        occurred_at=datetime.fromtimestamp(now, UTC),
                        actor="owner",
                        action="auth.login.succeeded",
                        object_type="owner",
                        object_id="owner:1",
                        payload={},
                    )
            if failed:
                raise InvalidCredentials("Incorrect password.")
            if session is None:
                raise RuntimeError("Login completed without a session.")
            return session

    def csrf_for_session(self, token: str | None) -> str | None:
        if token is None or TOKEN_PATTERN.fullmatch(token) is None:
            return None
        token_hash = sha256(token.encode("ascii")).hexdigest()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT csrf_token FROM auth_sessions "
                "WHERE token_hash = ? AND expires_at > ?",
                (token_hash, self._now()),
            ).fetchone()
        return str(row[0]) if row else None

    def logout(self, token: str) -> None:
        token_hash = sha256(token.encode("ascii")).hexdigest()
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                deleted = connection.execute(
                    "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
                )
                if deleted.rowcount:
                    append_event(
                        connection,
                        occurred_at=datetime.fromtimestamp(now, UTC),
                        actor="owner",
                        action="auth.logout",
                        object_type="owner",
                        object_id="owner:1",
                        payload={},
                    )
