import asyncio
import getpass
import sqlite3
import warnings
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher

import bq.auth as auth_module
import bq.cli as cli
from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import LOCK_SECONDS, MAX_FAILURES, SESSION_SECONDS, OwnerAuth
from bq.infra import database
from bq.infra.database import migrate

PASSWORD = "a long owner passphrase"


class FrozenClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def test_cli_owner_setup_never_overwrites_or_stores_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    assert cli.main(["init"]) == 0
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda _: next(answers))
    assert cli.main(["auth", "setup"]) == 0
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err

    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM owner_credentials"
        ).fetchone()[0]
    assert password_hash.startswith("$argon2id$")
    assert PASSWORD not in password_hash
    assert PasswordHasher().verify(password_hash, PASSWORD)
    assert cli.main(["auth", "setup"]) == 1
    assert "already configured" in capsys.readouterr().err


def test_cli_rejects_mismatched_and_short_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    assert cli.main(["init"]) == 0
    answers = iter([PASSWORD, "different", "short", "short"])
    monkeypatch.setattr(getpass, "getpass", lambda _: next(answers))
    assert cli.main(["auth", "setup"]) == 1
    assert cli.main(["auth", "setup"]) == 1
    assert not OwnerAuth(tmp_path / "bq.db").is_configured()


def test_cli_refuses_password_input_if_echo_cannot_be_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    assert cli.main(["init"]) == 0

    def unsafe_input(_: str) -> str:
        warnings.warn("Password may be echoed", getpass.GetPassWarning)
        return PASSWORD

    monkeypatch.setattr(getpass, "getpass", unsafe_input)
    assert cli.main(["auth", "setup"]) == 1
    assert "echo disabled" in capsys.readouterr().err
    assert not OwnerAuth(tmp_path / "bq.db").is_configured()


def test_login_session_csrf_logout_and_origin_checks(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            session_url = "/api/v1/auth/session"
            login_url = "/api/v1/auth/login"
            logout_url = "/api/v1/auth/logout"
            origin = {"Origin": ALLOWED_ORIGIN}
            response = await client.get(session_url)
            assert response.json()["state"] == "setup_required"
            response = await client.post(
                login_url, json={"password": PASSWORD}, headers=origin
            )
            assert response.status_code == 409

            auth.setup(PASSWORD)
            response = await client.post(login_url, json={"password": PASSWORD})
            assert response.status_code == 403
            response = await client.post(
                login_url,
                json={"password": PASSWORD},
                headers={"Origin": "http://evil.test"},
            )
            assert response.status_code == 403
            response = await client.post(
                login_url, json={"password": PASSWORD}, headers=origin
            )
            assert response.status_code == 200
            csrf = response.json()["csrf_token"]
            cookie = response.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie
            assert "Domain=" not in cookie
            assert response.headers["cache-control"] == "no-store"
            session_token = client.cookies.get("bq_session")
            assert session_token
            with closing(sqlite3.connect(path)) as connection:
                token_hash = connection.execute(
                    "SELECT token_hash FROM auth_sessions"
                ).fetchone()[0]
            assert session_token != token_hash
            response = await client.get(session_url)
            assert response.json() == {"state": "signed_in", "csrf_token": csrf}

            assert (await client.post(logout_url, headers=origin)).status_code == 403
            bad_origin = {"Origin": "http://evil.test", "X-CSRF-Token": csrf}
            assert (
                await client.post(logout_url, headers=bad_origin)
            ).status_code == 403
            assert (await client.get(session_url)).json()["state"] == "signed_in"
            response = await client.post(
                logout_url, headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf}
            )
            assert response.status_code == 204
            assert (await client.get(session_url)).json()["state"] == "signed_out"
            response = await client.get(
                session_url, headers={"Cookie": f"bq_session={session_token}"}
            )
            assert response.json()["state"] == "signed_out"

            secret = "x" * 257
            response = await client.post(
                login_url, json={"password": secret}, headers=origin
            )
            assert response.status_code == 422
            assert secret not in response.text

    asyncio.run(check())


def test_web_owner_setup_is_origin_protected_confirmed_and_audited(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            origin = {"Origin": ALLOWED_ORIGIN}
            without_origin = await client.post(
                "/api/v1/auth/setup",
                json={"password": PASSWORD, "confirmation": PASSWORD},
            )
            assert without_origin.status_code == 403

            mismatch = await client.post(
                "/api/v1/auth/setup",
                headers=origin,
                json={"password": PASSWORD, "confirmation": "Different password 123"},
            )
            assert mismatch.status_code == 422

            weak = await client.post(
                "/api/v1/auth/setup",
                headers=origin,
                json={"password": "xxxxxxxxxxxx", "confirmation": "xxxxxxxxxxxx"},
            )
            assert weak.status_code == 422
            assert "common or repetitive" in weak.json()["detail"]

            setup = await client.post(
                "/api/v1/auth/setup",
                headers=origin,
                json={"password": PASSWORD, "confirmation": PASSWORD},
            )
            assert setup.status_code == 200
            assert setup.json()["state"] == "signed_in"
            assert setup.json()["csrf_token"]
            cookie = setup.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie
            assert "Domain=" not in cookie
            assert setup.headers["cache-control"] == "no-store"

            session = await client.get("/api/v1/auth/session")
            assert session.json()["state"] == "signed_in"
            assert session.json()["csrf_token"] == setup.json()["csrf_token"]

            duplicate = await client.post(
                "/api/v1/auth/setup",
                headers=origin,
                json={
                    "password": "Another strong password",
                    "confirmation": "Another strong password",
                },
            )
            assert duplicate.status_code == 409

    asyncio.run(check())


def test_auth_audit_is_redacted_append_only_and_verified(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    auth.setup(PASSWORD)
    session = auth.login(PASSWORD)
    auth.logout(session.token)

    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT action, actor, payload_hash, previous_hash, event_hash "
            "FROM audit_events ORDER BY sequence"
        ).fetchall()
    assert [row[0] for row in rows] == [
        "owner.password.configured",
        "auth.login.succeeded",
        "auth.logout",
    ]
    assert [row[1] for row in rows] == ["owner", "owner", "owner"]
    assert rows[0][3] is None
    assert rows[1][3] == rows[0][4]
    assert rows[2][3] == rows[1][4]
    assert all(len(row[2]) == 64 and len(row[4]) == 64 for row in rows)
    assert PASSWORD not in repr(rows)
    assert session.token not in repr(rows)
    assert session.csrf_token not in repr(rows)
    database.check_database(path)

    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE audit_events SET action = 'tampered' WHERE sequence = 1"
            )
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit_events WHERE sequence = 1")
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "INSERT OR REPLACE INTO audit_events "
                "SELECT * FROM audit_events WHERE sequence = 1"
            )


def test_doctor_rejects_modified_audit_chain(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute(
            "UPDATE audit_events SET action = 'tampered' WHERE sequence = 1"
        )

    with pytest.raises(database.DatabaseError, match="Audit event chain"):
        database.check_database(path)


def test_lockout_and_session_expiry_survive_app_recreation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    monkeypatch.setattr(auth_module, "SystemClock", lambda: clock)
    OwnerAuth(path).setup(PASSWORD)

    async def check() -> None:
        origin = {"Origin": ALLOWED_ORIGIN}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(database_path=path)),
            base_url="http://127.0.0.1:8000",
        ) as client:
            for _ in range(MAX_FAILURES):
                result = await client.post(
                    "/api/v1/auth/login", json={"password": "wrong"}, headers=origin
                )
                assert result.status_code == 401
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(database_path=path)),
            base_url="http://127.0.0.1:8000",
        ) as client:
            result = await client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers=origin
            )
            assert result.status_code == 429
            clock.instant += timedelta(seconds=LOCK_SECONDS)
            result = await client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers=origin
            )
            assert result.status_code == 200
            clock.instant += timedelta(seconds=SESSION_SECONDS)
            result = await client.get("/api/v1/auth/session")
            assert result.json()["state"] == "signed_out"
        with closing(sqlite3.connect(path)) as connection:
            actions = connection.execute(
                "SELECT action FROM audit_events ORDER BY sequence"
            ).fetchall()
        assert [row[0] for row in actions] == [
            "owner.password.configured",
            "auth.login.locked",
            "auth.login.succeeded",
        ]

    asyncio.run(check())
