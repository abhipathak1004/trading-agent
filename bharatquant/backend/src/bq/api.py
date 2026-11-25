import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from hmac import compare_digest
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal, Protocol, runtime_checkable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from bq.auth import (
    SESSION_SECONDS,
    InvalidCredentials,
    LoginLocked,
    OwnerAlreadyConfigured,
    OwnerAuth,
    OwnerNotConfigured,
)
from bq.data.calendar import (
    ExchangeCalendar,
    SessionClass,
    SessionState,
    validate_coverage_query,
)
from bq.data.freshness import (
    DelayedDataMode,
    FreshnessStatus,
    freshness_policy_from_seconds,
    inspect_snapshot_freshness,
)
from bq.data.records import DataRecordStore, MissingDataRecord, QualityStatus
from bq.infra.jobs import JobRunStore
from bq.infra.model_attempts import ModelAttemptStore, TokenUsage
from bq.infra.scheduler import SchedulerLoop, SchedulerRunner
from bq.infra.schedules import ScheduleStore
from bq.infra.task_registry import TaskRegistry
from bq.paper_accounts import (
    PaperAccountStatus,
    PaperAccountStore,
)
from bq.schedules import (
    DailySchedule,
    IntervalSchedule,
    ScheduleError,
    WakeDecision,
    next_daily_occurrences,
    next_interval_occurrences,
)

WEB_ROOT = Path(__file__).with_name("static")
COOKIE_NAME = "bq_session"
ALLOWED_ORIGIN = "http://127.0.0.1:8000"


class Health(BaseModel):
    status: str


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256, strict=True)


class SetupRequest(BaseModel):
    password: str = Field(min_length=12, max_length=256, strict=True)
    confirmation: str = Field(min_length=12, max_length=256, strict=True)


class SessionStatus(BaseModel):
    state: Literal["setup_required", "signed_out", "signed_in"]
    csrf_token: str | None = None


class ImportStatus(BaseModel):
    import_id: str
    source_id: str
    recorded_at: datetime
    record_count: int


class DataStatus(BaseModel):
    import_count: int
    imported_record_count: int
    import_quality: Literal["unassessed"]
    live_provider: Literal["not_configured"]
    recent_imports: list[ImportStatus]


class SnapshotFreshnessStatus(BaseModel):
    record_id: str
    source_id: str
    quality_status: QualityStatus
    event_at: datetime | None
    known_at: datetime
    recorded_at: datetime
    as_of: datetime
    status: FreshnessStatus
    age_microseconds: str | None
    delayed_after_seconds: int
    critical_after_seconds: int
    delayed_mode: DelayedDataMode


class CalendarDayStatus(BaseModel):
    session_date: date
    state: Literal["UNCOVERED"] | SessionState
    session_class: SessionClass | None
    session_id: str | None
    opens_at: datetime | None
    closes_at: datetime | None
    known_at: datetime | None
    reviewed_at: datetime | None
    recorded_at: datetime | None


class CalendarCoverageStatus(BaseModel):
    exchange_code: str
    as_of: datetime
    days: list[CalendarDayStatus]


class TokenUsageStatus(BaseModel):
    reported_tokens: str
    reported_count: int
    estimated_tokens: str
    estimated_count: int
    unknown_count: int


class ModelUsageStatus(BaseModel):
    task_id: str
    task_version: str
    model_id: str
    attempt_count: int
    incomplete_count: int
    cache_hits: int
    input: TokenUsageStatus
    output: TokenUsageStatus


class UsageStatus(BaseModel):
    total_groups: int
    groups: list[ModelUsageStatus]
    provider_connected: Literal[False]


class PaperAccountStatusResponse(BaseModel):
    account_id: str
    name: str
    status: PaperAccountStatus
    opening_balance_paise: str
    deposited_paise: str
    withdrawn_paise: str
    balance_paise: str


class DailyScheduleStatus(BaseModel):
    schedule_id: str
    job_id: str
    enabled: bool
    timezone: str
    local_time: str
    interval_minutes: int | None
    window_end: str | None
    weekdays: list[int]
    preview_state: Literal["available", "disabled", "pending_change", "unavailable"]
    next_occurrences: list[datetime]


class WakeStatus(BaseModel):
    schedule_id: str
    scheduled_at: datetime
    observed_at: datetime
    schedule_version: int
    decision: WakeDecision


class ScheduleStatus(BaseModel):
    latest_version: int
    observed_at: datetime
    effective_state: Literal["not_configured", "available", "unknown"]
    effective_version: int | None
    effective_at: datetime | None
    schedule_count: int | None
    schedules: list[DailyScheduleStatus]
    decision_count: int
    recent_decisions: list[WakeStatus]
    scheduler_running: bool


class ConfiguredTask(BaseModel):
    task_id: str
    task_version: str
    task_class: str
    allowed_data: list[str]
    allowed_tiers: list[str]
    prompt_version: str
    spec_version: str


class SettingsOverview(BaseModel):
    """Read-only settings surface: what is configured, and what is not."""

    tasks: list[ConfiguredTask]
    task_count: int
    schedule_count: int
    scheduler_running: bool
    model_usage_groups: int
    provider_connected: Literal[False]
    api_keys_configured: Literal[False]
    agent_configuration_state: Literal["not_configured"]


def _daily_schedule_status(
    schedule: DailySchedule, *, observed_at: datetime, pending_change: bool
) -> DailyScheduleStatus:
    preview_state: Literal["available", "disabled", "pending_change", "unavailable"]
    occurrences: tuple[datetime, ...] = ()
    if not schedule.enabled:
        preview_state = "disabled"
    elif pending_change:
        preview_state = "pending_change"
    else:
        try:
            occurrences = (
                next_interval_occurrences(schedule, after=observed_at)
                if isinstance(schedule, IntervalSchedule)
                else next_daily_occurrences(schedule, after=observed_at)
            )
        except ScheduleError:
            preview_state = "unavailable"
        else:
            preview_state = "available"
    return DailyScheduleStatus(
        schedule_id=schedule.schedule_id,
        job_id=schedule.job_id,
        enabled=schedule.enabled,
        timezone=schedule.timezone,
        local_time=schedule.local_time.isoformat(timespec="minutes"),
        interval_minutes=schedule.interval_minutes
        if isinstance(schedule, IntervalSchedule)
        else None,
        window_end=schedule.window_end.isoformat(timespec="minutes")
        if isinstance(schedule, IntervalSchedule)
        else None,
        weekdays=list(schedule.weekdays),
        preview_state=preview_state,
        next_occurrences=list(occurrences),
    )


def _token_usage_status(usage: TokenUsage) -> TokenUsageStatus:
    return TokenUsageStatus(
        reported_tokens=str(usage.reported_tokens),
        reported_count=usage.reported_count,
        estimated_tokens=str(usage.estimated_tokens),
        estimated_count=usage.estimated_count,
        unknown_count=usage.unknown_count,
    )


@runtime_checkable
class StreamingResponseLike(Protocol):
    body_iterator: AsyncIterable[str | bytes | memoryview]


def create_app(
    web_root: Path | None = None, *, database_path: Path | None = None
) -> FastAPI:
    scheduler: SchedulerLoop | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()

    app = FastAPI(title="BharatQuant", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1"], www_redirect=False
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if request.url.path == "/api/v1/auth/login":
            return JSONResponse(
                status_code=422, content={"detail": "Invalid login request"}
            )
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(
            (
                "/api/v1/auth/",
                "/api/v1/calendar/",
                "/api/v1/data/",
                "/api/v1/llm/",
                "/api/v1/schedules/",
                "/api/v1/settings/",
                "/api/v1/paper/",
            )
        ):
            response.headers["Cache-Control"] = "no-store"
        if response.headers.get("content-type", "").startswith("text/html"):
            nonce = token_urlsafe(32)
            if isinstance(response, StreamingResponseLike):
                chunks: list[bytes] = []
                async for chunk in response.body_iterator:
                    if isinstance(chunk, str):
                        chunks.append(chunk.encode("utf-8"))
                    else:
                        chunks.append(bytes(chunk))
                body = b"".join(chunks)
            else:
                body = bytes(response.body)
            body = re.sub(
                rb"<script(?=[\s>])",
                f'<script nonce="{nonce}"'.encode("ascii"),
                body,
            )
            headers = dict(response.headers)
            headers.pop("content-length", None)
            headers.pop("etag", None)
            headers["Cache-Control"] = "no-store"
            headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}' 'strict-dynamic'; "
                "style-src 'self'; img-src 'self' data:; font-src 'self'; "
                "connect-src 'self'; base-uri 'none'; object-src 'none'; "
                "frame-ancestors 'none'; form-action 'self'"
            )
            response = Response(
                content=body,
                status_code=response.status_code,
                headers=headers,
                background=response.background,
            )
        return response

    @app.get("/api/v1/system/health")
    def health() -> Health:
        return Health(status="ok")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    if database_path is not None:
        auth = OwnerAuth(database_path)
        calendar = ExchangeCalendar(database_path)
        records = DataRecordStore(database_path)
        attempts = ModelAttemptStore(database_path)
        schedules = ScheduleStore(database_path, available_jobs=frozenset())
        paper_accounts = PaperAccountStore(database_path)
        scheduler = SchedulerLoop(
            SchedulerRunner(
                database_path,
                schedule_store=schedules,
                job_store=JobRunStore(database_path),
            )
        )

        def verify_origin(request: Request) -> None:
            if request.headers.get("origin") != ALLOWED_ORIGIN:
                raise HTTPException(status_code=403, detail="Invalid request origin")

        @app.get("/api/v1/data/status")
        def data_status(request: Request) -> DataStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            summary = records.import_summary()
            return DataStatus(
                import_count=summary.import_count,
                imported_record_count=summary.record_count,
                import_quality="unassessed",
                live_provider="not_configured",
                recent_imports=[
                    ImportStatus(
                        import_id=item.import_id,
                        source_id=item.source_id,
                        recorded_at=item.recorded_at,
                        record_count=item.record_count,
                    )
                    for item in summary.recent_imports
                ],
            )

        @app.get("/api/v1/data/snapshot-freshness")
        def snapshot_freshness(
            request: Request,
            record_id: str,
            as_of: str,
            delayed_after_seconds: str,
            critical_after_seconds: str,
            delayed_mode: str,
        ) -> SnapshotFreshnessStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                if (
                    len(as_of) > 64
                    or re.fullmatch(r"[0-9]{1,8}", delayed_after_seconds) is None
                    or re.fullmatch(r"[0-9]{1,8}", critical_after_seconds) is None
                ):
                    raise ValueError("Invalid freshness query.")
                instant = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                delayed = int(delayed_after_seconds)
                critical = int(critical_after_seconds)
                mode = DelayedDataMode(delayed_mode)
                policy = freshness_policy_from_seconds(delayed, critical, mode)
                version = records.version_for(record_id, as_of=instant)
                result = inspect_snapshot_freshness(
                    version, as_of=instant, policy=policy
                )
            except MissingDataRecord as exc:
                raise HTTPException(
                    status_code=404, detail="Snapshot unavailable"
                ) from exc
            except (ValueError, OverflowError) as exc:
                raise HTTPException(
                    status_code=422, detail="Invalid freshness query"
                ) from exc
            return SnapshotFreshnessStatus(
                record_id=version.record_id,
                source_id=version.record.source_id,
                quality_status=version.record.quality_status,
                event_at=version.record.event_at,
                known_at=version.record.known_at,
                recorded_at=version.recorded_at,
                as_of=instant,
                status=result.status,
                age_microseconds=(
                    str(result.age // timedelta(microseconds=1))
                    if result.age is not None
                    else None
                ),
                delayed_after_seconds=delayed,
                critical_after_seconds=critical,
                delayed_mode=mode,
            )

        @app.get("/api/v1/calendar/coverage")
        def calendar_coverage(
            request: Request, exchange_code: str, start: str, end: str, as_of: str
        ) -> CalendarCoverageStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                start_date = date.fromisoformat(start)
                end_date = date.fromisoformat(end)
                if start_date.isoformat() != start or end_date.isoformat() != end:
                    raise ValueError("Dates must use YYYY-MM-DD.")
                instant = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                validate_coverage_query(
                    exchange_code, start=start_date, end=end_date, as_of=instant
                )
            except (OverflowError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail="Invalid calendar coverage query"
                ) from exc
            days = calendar.coverage(
                exchange_code, start=start_date, end=end_date, as_of=instant
            )
            result = []
            for day in days:
                version = day.version
                session = version.session if version is not None else None
                result.append(
                    CalendarDayStatus(
                        session_date=day.session_date,
                        state=session.state if session is not None else "UNCOVERED",
                        session_class=session.session_class
                        if session is not None
                        else None,
                        session_id=version.session_id if version is not None else None,
                        opens_at=session.opens_at if session is not None else None,
                        closes_at=session.closes_at if session is not None else None,
                        known_at=session.known_at if session is not None else None,
                        reviewed_at=session.reviewed_at
                        if session is not None
                        else None,
                        recorded_at=version.recorded_at
                        if version is not None
                        else None,
                    )
                )
            return CalendarCoverageStatus(
                exchange_code=exchange_code, as_of=instant.astimezone(UTC), days=result
            )

        @app.get("/api/v1/llm/usage")
        def model_usage(request: Request) -> UsageStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            summaries = attempts.usage()
            return UsageStatus(
                total_groups=len(summaries),
                groups=[
                    ModelUsageStatus(
                        task_id=item.task_id,
                        task_version=item.task_version,
                        model_id=item.model_id,
                        attempt_count=item.attempt_count,
                        incomplete_count=item.incomplete_count,
                        cache_hits=item.cache_hits,
                        input=_token_usage_status(item.input),
                        output=_token_usage_status(item.output),
                    )
                    for item in summaries[:20]
                ],
                provider_connected=False,
            )

        @app.get("/api/v1/paper/accounts/{account_id}")
        def paper_account_status(
            request: Request, account_id: str
        ) -> PaperAccountStatusResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                balance = paper_accounts.get_balance(account_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=404, detail="Paper account unavailable"
                ) from exc
            account = balance.account
            return PaperAccountStatusResponse(
                account_id=account.account_id,
                name=account.name,
                status=account.status,
                opening_balance_paise=str(account.opening_balance_paise),
                deposited_paise=str(balance.deposited_paise),
                withdrawn_paise=str(balance.withdrawn_paise),
                balance_paise=str(balance.balance_paise),
            )

        @app.get("/api/v1/schedules/status")
        def schedule_status(request: Request) -> ScheduleStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            overview = schedules.overview()
            effective = overview.effective
            active = effective.snapshot.schedules if effective is not None else ()
            return ScheduleStatus(
                latest_version=overview.latest.version,
                observed_at=overview.observed_at,
                effective_state=overview.effective_state,
                effective_version=effective.snapshot.version if effective else None,
                effective_at=effective.effective_at if effective else None,
                schedule_count=len(active) if effective else None,
                schedules=[
                    _daily_schedule_status(
                        item,
                        observed_at=overview.observed_at,
                        pending_change=effective is not None
                        and effective.snapshot.version != overview.latest.version,
                    )
                    for item in active[:20]
                ],
                decision_count=overview.decision_count,
                recent_decisions=[
                    WakeStatus(
                        schedule_id=item.schedule_id,
                        scheduled_at=item.scheduled_at,
                        observed_at=item.observed_at,
                        schedule_version=item.schedule_version,
                        decision=item.decision,
                    )
                    for item in overview.recent_decisions
                ],
                scheduler_running=scheduler.running,
            )

        @app.get("/api/v1/settings/overview")
        def settings_overview(request: Request) -> SettingsOverview:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            definitions = TaskRegistry(database_path).list_latest(limit=50)
            overview = schedules.overview()
            effective = overview.effective
            active = effective.snapshot.schedules if effective is not None else ()
            usage = attempts.usage()
            return SettingsOverview(
                tasks=[
                    ConfiguredTask(
                        task_id=item.policy.task_id,
                        task_version=item.policy.version,
                        task_class=item.policy.task_class.value,
                        allowed_data=sorted(
                            value.value for value in item.policy.allowed_data
                        ),
                        allowed_tiers=sorted(
                            value.value for value in item.policy.allowed_tiers
                        ),
                        prompt_version=item.prompt_version,
                        spec_version=item.spec_version,
                    )
                    for item in definitions
                ],
                task_count=len(definitions),
                schedule_count=len(active),
                scheduler_running=scheduler.running,
                model_usage_groups=len(usage),
                provider_connected=False,
                api_keys_configured=False,
                agent_configuration_state="not_configured",
            )

        @app.get("/api/v1/auth/session")
        def session_status(request: Request) -> SessionStatus:
            if not auth.is_configured():
                return SessionStatus(state="setup_required")
            csrf_token = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf_token is None:
                return SessionStatus(state="signed_out")
            return SessionStatus(state="signed_in", csrf_token=csrf_token)

        @app.post("/api/v1/auth/login")
        def login(
            request: Request, body: LoginRequest, response: Response
        ) -> SessionStatus:
            verify_origin(request)
            try:
                session = auth.login(body.password)
            except OwnerNotConfigured as exc:
                raise HTTPException(
                    status_code=409, detail="Owner password is not configured"
                ) from exc
            except InvalidCredentials as exc:
                raise HTTPException(
                    status_code=401, detail="Incorrect password"
                ) from exc
            except LoginLocked as exc:
                raise HTTPException(
                    status_code=429, detail="Too many login attempts"
                ) from exc
            response.set_cookie(
                COOKIE_NAME,
                session.token,
                max_age=SESSION_SECONDS,
                httponly=True,
                samesite="strict",
                path="/",
            )
            return SessionStatus(state="signed_in", csrf_token=session.csrf_token)

        @app.post("/api/v1/auth/setup")
        def setup_owner(
            request: Request, response: Response, body: SetupRequest
        ) -> SessionStatus:
            verify_origin(request)
            if auth.is_configured():
                raise HTTPException(
                    status_code=409, detail="Owner password is already configured"
                )
            if body.password != body.confirmation:
                raise HTTPException(status_code=422, detail="Passwords do not match")
            try:
                auth.setup(body.password, source="web")
            except OwnerAlreadyConfigured as exc:
                raise HTTPException(
                    status_code=409, detail="Owner password is already configured"
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            try:
                session = auth.login(body.password)
            except OwnerNotConfigured:
                return SessionStatus(state="signed_out")
            except InvalidCredentials:
                return SessionStatus(state="signed_out")
            except LoginLocked:
                return SessionStatus(state="signed_out")
            response.set_cookie(
                COOKIE_NAME,
                session.token,
                max_age=SESSION_SECONDS,
                httponly=True,
                samesite="strict",
                path="/",
            )
            return SessionStatus(state="signed_in", csrf_token=session.csrf_token)

        @app.post("/api/v1/auth/logout", status_code=204)
        def logout(request: Request) -> Response:
            verify_origin(request)
            token = request.cookies.get(COOKIE_NAME)
            expected = auth.csrf_for_session(token)
            if token is None or expected is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            supplied = request.headers.get("x-csrf-token", "")
            if not compare_digest(supplied.encode("utf-8"), expected.encode("ascii")):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            auth.logout(token)
            response = Response(status_code=204)
            response.delete_cookie(
                COOKIE_NAME, path="/", httponly=True, samesite="strict"
            )
            return response

    static_root = web_root or WEB_ROOT
    if static_root.is_dir():

        @app.get("/api/{path:path}", include_in_schema=False)
        def unknown_api_path(path: str) -> None:
            raise HTTPException(status_code=404, detail="Not found")

        app.mount("/", StaticFiles(directory=static_root, html=True), name="web")

    return app
