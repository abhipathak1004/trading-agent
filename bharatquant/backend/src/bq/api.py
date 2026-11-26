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

from bq.analysis.batch import BatchAnalyst, BatchError, parse_symbol_list
from bq.analysis.debate import Debate
from bq.analysis.pipeline import AnalysisPipeline, PipelineError
from bq.analysis.risk_team import RiskAnalyst
from bq.analysis.run_settings import AnalysisRunError, AnalysisRunStore
from bq.analysis.wiring import build_analysis_router
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
from bq.data.symbols import SymbolError
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import DataDomain
from bq.data.yahoo import YahooDataClient, YahooError
from bq.infra.jobs import JobRunStore
from bq.infra.model_attempts import ModelAttemptStore, TokenUsage
from bq.infra.scheduler import SchedulerLoop, SchedulerRunner
from bq.infra.schedules import ScheduleStore
from bq.infra.task_registry import TaskRegistry
from bq.llm_client import LLMClientError, LLMNotConfigured
from bq.llm_service import LLMService
from bq.model_router import ModelRouter, RoutedModelCaller
from bq.news import PULSE_SOURCE_ID, MemoryKind, NewsError, NewsStore
from bq.news_agent import NewsResearchAgent
from bq.paper_accounts import (
    PaperAccountStatus,
    PaperAccountStore,
)
from bq.research_agent import ResearchAgent
from bq.schedules import (
    DailySchedule,
    IntervalSchedule,
    ScheduleError,
    WakeDecision,
    next_daily_occurrences,
    next_interval_occurrences,
)
from bq.settings import (
    MAX_AGENT_ROUTING_ENTRIES,
    MAX_API_KEY_CHARS,
    MAX_ATTEMPTS,
    MAX_MODEL_NAME_CHARS,
    MAX_SETTING_CHARS,
    MAX_TIMEOUT_MS,
    MAX_TOKENS_LIMIT,
    MIN_TIMEOUT_MS,
    AgentRoute,
    AppConfig,
    DecisionFallback,
    DecisionPolicyConfig,
    DecisionRoute,
    PipelineConfig,
    Provider,
    SettingsError,
    SettingsStore,
    TierConfig,
    TierName,
)
from bq.tools.base import ToolBlocked, ToolError, ToolSpec

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
    live_provider: str
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
    provider_connected: bool


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


class NewsItemStatus(BaseModel):
    news_id: str
    source_id: str
    url: str
    title: str
    summary: str
    category: str
    long_term_impact: bool
    published_at: datetime


class NewsMemoryStatus(BaseModel):
    memory_id: str
    category: str
    title: str
    body: str
    known_at: datetime


class NewsDigest(BaseModel):
    source_id: str
    item_count: int
    items: list[NewsItemStatus]
    memory_count: int
    memories: list[NewsMemoryStatus]


class NewsIngestResult(BaseModel):
    source_id: str
    fetched: int
    stored: int
    duplicates: int
    long_term_memories: int


class ToolSpecStatus(BaseModel):
    name: str
    summary: str
    mutating: bool
    arguments: list[str]


class ToolCatalog(BaseModel):
    agent_id: str
    read_only: bool
    tools: list[ToolSpecStatus]


class ToolRunRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, strict=True)
    arguments: dict[str, object] = Field(default_factory=dict)


class ToolRunResponse(BaseModel):
    tool: str
    message: str
    break_loop: bool


class AgentStepStatus(BaseModel):
    turn: int
    kind: str
    tool: str
    arguments: dict[str, object]
    output: str


class AgentRunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4_000, strict=True)
    max_turns: int = Field(default=10, ge=1, le=12, strict=True)


class AgentRunResponse(BaseModel):
    task: str
    final_text: str
    turns: int
    stopped: bool
    tool_calls: list[str]
    steps: list[AgentStepStatus]
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None


class AgentStatus(BaseModel):
    agent_id: str
    configured: bool
    provider: str
    model: str
    tools: list[ToolSpecStatus]


class AnalysisRunRequest(BaseModel):
    """A request to analyse one symbol.

    Bounds are deliberately tight: this is a single-owner loopback app, and an
    unbounded symbol or timeframe would let a typo turn into a broker storm.
    """

    symbol: str = Field(min_length=1, max_length=32, strict=True)
    timeframe: Literal["intraday", "swing"] = "swing"
    equity_paise: int = Field(gt=0, le=10**13, strict=True)
    available_cash_paise: int = Field(ge=0, le=10**13, strict=True)
    risk_basis_points: int = Field(default=100, ge=0, le=10_000, strict=True)


class BatchAnalysisRequest(BaseModel):
    """Analyse several symbols in one submission.

    ``symbols`` is free text because that is what a person types ("RELIANCE, TCS,
    HDFCBANK"). Parsing and de-duplication happen server-side in
    ``parse_symbol_list`` so the rules live in one place, not in the browser.
    """

    symbols: str = Field(min_length=1, max_length=512, strict=True)
    timeframe: Literal["intraday", "swing"] = "swing"
    equity_paise: int = Field(gt=0, le=10**13, strict=True)
    available_cash_paise: int = Field(ge=0, le=10**13, strict=True)
    risk_basis_points: int = Field(default=100, ge=0, le=10_000, strict=True)


class BatchAgentStatus(BaseModel):
    node: str
    role: str
    status: str
    summary: str


class BatchAnalysisItem(BaseModel):
    """One symbol's outcome. ``analysis`` is null when ``error`` is set."""

    requested: str
    symbol: str
    ok: bool
    error: str | None
    analysis: dict[str, object] | None


class BatchAnalysisResponse(BaseModel):
    timeframe: str
    as_of: str
    requested: int
    succeeded: int
    failed: int
    items: list[BatchAnalysisItem]


class AnalysisProvenance(BaseModel):
    vendor_daily: str
    vendor_quotes: str
    is_realtime: bool
    data_freshness: str


class AnalysisRunResponse(BaseModel):
    run_id: str
    symbol: str
    timeframe: str
    mode: str
    as_of: str
    data_provenance: AnalysisProvenance
    report: dict[str, object]
    plan: dict[str, object] | None
    verdict: dict[str, object] | None
    risk: dict[str, object] | None
    vetoes: list[str]
    notes: list[str]
    skipped_nodes: list[str]


class AnalysisRunSummary(BaseModel):
    run_id: str
    symbol: str
    timeframe: str
    as_of: str
    mode: str
    signal: str
    data_freshness: str
    is_realtime: bool
    composite: float | None


class AnalysisRunList(BaseModel):
    runs: list[AnalysisRunSummary]


class VendorChainStatus(BaseModel):
    domain: str
    vendors: list[str]


class VendorsResponse(BaseModel):
    kite_enabled: bool
    chains: list[VendorChainStatus]


class LLMCompleteRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32_000, strict=True)
    system: str = Field(default="", max_length=8_000, strict=True)


class LLMCompleteResponse(BaseModel):
    provider: str
    model: str
    text: str
    reasoning: str
    finish_reason: str
    truncated: bool
    input_tokens: int | None
    output_tokens: int | None


class DataDownloadRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=24, strict=True)
    range: str = Field(default="1mo", max_length=8, strict=True)
    interval: str = Field(default="1d", max_length=8, strict=True)


class DataDownloadResponse(BaseModel):
    import_id: str
    symbol: str
    record_count: int


class ApiKeyStatus(BaseModel):
    provider: Provider
    configured: bool
    masked: str | None


class TierConfigStatus(BaseModel):
    provider: Provider | None
    model: str
    temperature: float
    max_tokens: int
    timeout_ms: int
    max_attempts: int
    fallback_tier: TierName | None
    configured: bool


class AgentRouteStatus(BaseModel):
    tier: TierName
    provider: Provider | None
    model: str | None


class DecisionPolicyStatus(BaseModel):
    min_choice_confidence: float
    min_noul_probability: float
    abstain_score_band: list[int]
    llm_fallback_size_scale: float
    heuristic_size_scale: float


class PipelineStatus(BaseModel):
    debate_rounds: int
    max_tool_rounds_per_agent: int
    screener_min_score: float
    final_decision_min_prob: float
    execution_mode: str
    decision_fallback_order: list[str]


class AppConfigStatus(BaseModel):
    provider: Provider
    model_name: str
    news_feed_url: str
    news_memory_ttl_days: int
    api_keys: list[ApiKeyStatus]
    model_route_configured: bool
    updated_at: datetime | None
    tiers: dict[str, TierConfigStatus]
    agent_routing: dict[str, AgentRouteStatus]
    decision_policy: DecisionPolicyStatus
    pipeline: PipelineStatus
    decision_route: DecisionRoute
    configured_tiers: list[str]
    decision_preview: str


class TierConfigUpdate(BaseModel):
    provider: Provider | None = None
    model: str = Field(default="", max_length=MAX_MODEL_NAME_CHARS, strict=True)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=MAX_TOKENS_LIMIT, strict=True)
    timeout_ms: int = Field(
        default=60000, ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS, strict=True
    )
    max_attempts: int = Field(default=3, ge=1, le=MAX_ATTEMPTS, strict=True)
    fallback_tier: TierName | None = None


class AgentRouteUpdate(BaseModel):
    tier: TierName
    provider: Provider | None = None
    model: str | None = Field(default=None, max_length=MAX_MODEL_NAME_CHARS)


class DecisionPolicyUpdate(BaseModel):
    min_choice_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    min_noul_probability: float = Field(default=0.60, ge=0.0, le=1.0)
    abstain_score_band: tuple[int, int] = (2, 3)
    llm_fallback_size_scale: float = Field(default=0.75, ge=0.0, le=1.0)
    heuristic_size_scale: float = Field(default=0.50, ge=0.0, le=1.0)


class PipelineUpdate(BaseModel):
    debate_rounds: int = Field(default=1, ge=1, le=3, strict=True)
    max_tool_rounds_per_agent: int = Field(default=8, ge=1, le=20, strict=True)
    screener_min_score: float = Field(default=0.60, ge=0.0, le=1.0)
    final_decision_min_prob: float = Field(default=0.65, ge=0.0, le=1.0)
    execution_mode: str = Field(default="PAPER", max_length=16, strict=True)
    decision_fallback_order: list[DecisionFallback] = Field(
        default_factory=lambda: [
            DecisionFallback.FLASH,
            DecisionFallback.REASONING,
            DecisionFallback.HEURISTIC,
        ]
    )


class ModelsUpdate(BaseModel):
    tiers: dict[str, TierConfigUpdate]
    agent_routing: dict[str, AgentRouteUpdate] = Field(
        default_factory=dict, max_length=MAX_AGENT_ROUTING_ENTRIES
    )
    decision_policy: DecisionPolicyUpdate = DecisionPolicyUpdate()
    pipeline: PipelineUpdate = PipelineUpdate()
    decision_route: DecisionRoute = DecisionRoute.AUTO


class AppConfigUpdate(BaseModel):
    provider: Provider
    model_name: str = Field(max_length=MAX_SETTING_CHARS, strict=True)
    news_feed_url: str = Field(max_length=MAX_SETTING_CHARS, strict=True)
    news_memory_ttl_days: int = Field(strict=True)


class ApiKeyUpdate(BaseModel):
    api_key: str = Field(min_length=1, max_length=MAX_API_KEY_CHARS, strict=True)


class SettingsOverview(BaseModel):
    """Read-only settings surface: what is configured, and what is not."""

    tasks: list[ConfiguredTask]
    task_count: int
    schedule_count: int
    scheduler_running: bool
    model_usage_groups: int
    provider_connected: bool
    api_keys_configured: bool
    agent_configuration_state: Literal["not_configured", "configured"]
    config: AppConfigStatus


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


def _app_config_status(config: AppConfig) -> AppConfigStatus:
    return AppConfigStatus(
        provider=config.provider,
        model_name=config.model_name,
        news_feed_url=config.news_feed_url,
        news_memory_ttl_days=config.news_memory_ttl_days,
        api_keys=[
            ApiKeyStatus(
                provider=key.provider,
                configured=key.configured,
                masked=key.masked,
            )
            for key in config.api_keys
        ],
        model_route_configured=config.model_route_configured,
        updated_at=config.updated_at,
        tiers={
            name.value: TierConfigStatus(
                provider=tier.provider,
                model=tier.model,
                temperature=tier.temperature,
                max_tokens=tier.max_tokens,
                timeout_ms=tier.timeout_ms,
                max_attempts=tier.max_attempts,
                fallback_tier=tier.fallback_tier,
                configured=config.tier_configured(name),
            )
            for name, tier in config.tiers
        },
        agent_routing={
            agent_id: AgentRouteStatus(
                tier=route.tier,
                provider=route.provider,
                model=route.model,
            )
            for agent_id, route in config.agent_routing
        },
        decision_policy=DecisionPolicyStatus(
            min_choice_confidence=config.decision_policy.min_choice_confidence,
            min_noul_probability=config.decision_policy.min_noul_probability,
            abstain_score_band=list(config.decision_policy.abstain_score_band),
            llm_fallback_size_scale=config.decision_policy.llm_fallback_size_scale,
            heuristic_size_scale=config.decision_policy.heuristic_size_scale,
        ),
        pipeline=PipelineStatus(
            debate_rounds=config.pipeline.debate_rounds,
            max_tool_rounds_per_agent=config.pipeline.max_tool_rounds_per_agent,
            screener_min_score=config.pipeline.screener_min_score,
            final_decision_min_prob=config.pipeline.final_decision_min_prob,
            execution_mode=config.pipeline.execution_mode,
            decision_fallback_order=[
                item.value for item in config.pipeline.decision_fallback_order
            ],
        ),
        decision_route=config.decision_route,
        configured_tiers=[name.value for name in config.configured_tiers],
        decision_preview=config.decision_preview,
    )


def _analysis_pipeline(
    database_path: Path, config: AppConfig, settings_store: SettingsStore
) -> AnalysisPipeline:
    """Build a ready-to-run pipeline for this configuration.

    One builder so the single and batch routes cannot drift apart in how they
    wire the debate, or in whether they attach a model at all.
    """
    router = build_analysis_router(database_path, config)
    if config.any_tier_configured:
        model_router = ModelRouter(settings_store)
        caller = RoutedModelCaller(model_router)
        return AnalysisPipeline(
            router,
            debate=Debate(caller),
            risk_analyst=RiskAnalyst(caller),
            routing=caller,
        )
    service = LLMService(settings_store) if config.model_route_configured else None
    return AnalysisPipeline(
        router,
        debate=Debate(service) if service is not None else None,
        risk_analyst=RiskAnalyst(service) if service is not None else None,
    )


def _tool_spec_status(spec: ToolSpec) -> ToolSpecStatus:
    return ToolSpecStatus(
        name=spec.name,
        summary=spec.summary,
        mutating=spec.mutating,
        arguments=[argument.name for argument in spec.arguments],
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
                "/api/v1/news/",
                "/api/v1/schedules/",
                "/api/v1/settings/",
                "/api/v1/paper/",
                "/api/v1/tools/",
                "/api/v1/agent/",
                "/api/v1/analysis/",
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
        settings = SettingsStore(database_path)
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
                live_provider=(
                    settings.load().provider.value.lower()
                    if settings.load().api_keys_configured
                    else "not_configured"
                ),
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
                provider_connected=settings.load().api_keys_configured,
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
            config = settings.load()
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
                provider_connected=config.model_route_configured,
                api_keys_configured=config.api_keys_configured,
                agent_configuration_state=(
                    "configured" if config.model_route_configured else "not_configured"
                ),
                config=_app_config_status(config),
            )

        @app.get("/api/v1/settings/config")
        def get_settings_config(request: Request) -> AppConfigStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return _app_config_status(settings.load())

        @app.put("/api/v1/settings/config")
        def update_settings_config(
            request: Request, body: AppConfigUpdate
        ) -> AppConfigStatus:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            try:
                config = settings.save_config(
                    provider=body.provider,
                    model_name=body.model_name,
                    news_feed_url=body.news_feed_url,
                    news_memory_ttl_days=body.news_memory_ttl_days,
                )
            except SettingsError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _app_config_status(config)

        @app.put("/api/v1/settings/models")
        def update_settings_models(
            request: Request, body: ModelsUpdate
        ) -> AppConfigStatus:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            try:
                tiers = {
                    TierName(name): TierConfig(
                        provider=item.provider,
                        model=item.model,
                        temperature=item.temperature,
                        max_tokens=item.max_tokens,
                        timeout_ms=item.timeout_ms,
                        max_attempts=item.max_attempts,
                        fallback_tier=item.fallback_tier,
                    )
                    for name, item in body.tiers.items()
                }
                routing = {
                    agent_id: AgentRoute(
                        tier=item.tier,
                        provider=item.provider,
                        model=item.model,
                    )
                    for agent_id, item in body.agent_routing.items()
                }
                policy = DecisionPolicyConfig(
                    min_choice_confidence=body.decision_policy.min_choice_confidence,
                    min_noul_probability=body.decision_policy.min_noul_probability,
                    abstain_score_band=body.decision_policy.abstain_score_band,
                    llm_fallback_size_scale=body.decision_policy.llm_fallback_size_scale,
                    heuristic_size_scale=body.decision_policy.heuristic_size_scale,
                )
                pipeline = PipelineConfig(
                    debate_rounds=body.pipeline.debate_rounds,
                    max_tool_rounds_per_agent=body.pipeline.max_tool_rounds_per_agent,
                    screener_min_score=body.pipeline.screener_min_score,
                    final_decision_min_prob=body.pipeline.final_decision_min_prob,
                    execution_mode=body.pipeline.execution_mode,
                    decision_fallback_order=tuple(
                        body.pipeline.decision_fallback_order
                    ),
                )
                config = settings.save_models(
                    tiers=tiers,
                    agent_routing=routing,
                    pipeline=pipeline,
                    decision_route=body.decision_route,
                    decision_policy=policy,
                )
            except (SettingsError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _app_config_status(config)

        @app.put("/api/v1/settings/api-keys/{provider}")
        def set_provider_api_key(
            request: Request, provider: Provider, body: ApiKeyUpdate
        ) -> AppConfigStatus:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            try:
                settings.set_api_key(provider, body.api_key)
            except SettingsError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _app_config_status(settings.load())

        @app.delete("/api/v1/settings/api-keys/{provider}")
        def clear_provider_api_key(
            request: Request, provider: Provider
        ) -> AppConfigStatus:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            settings.clear_api_key(provider)
            return _app_config_status(settings.load())

        @app.get("/api/v1/news/digest")
        def news_digest(request: Request) -> NewsDigest:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            store = NewsStore(database_path)
            items = store.recent(limit=20)
            memories = store.memories(kind=MemoryKind.NEWS_LONG_TERM, limit=20)
            return NewsDigest(
                source_id=PULSE_SOURCE_ID,
                item_count=len(items),
                items=[
                    NewsItemStatus(
                        news_id=item.news_id,
                        source_id=item.source_id,
                        url=item.url,
                        title=item.title,
                        summary=item.summary,
                        category=item.category.value,
                        long_term_impact=item.long_term_impact,
                        published_at=item.published_at,
                    )
                    for item in items
                ],
                memory_count=len(memories),
                memories=[
                    NewsMemoryStatus(
                        memory_id=item.memory_id,
                        category=item.category.value,
                        title=item.title,
                        body=item.body,
                        known_at=item.known_at,
                    )
                    for item in memories
                ],
            )

        @app.post("/api/v1/news/ingest")
        def news_ingest(request: Request) -> NewsIngestResult:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            config = settings.load()
            agent = NewsResearchAgent(
                database_path,
                feed_url=config.news_feed_url,
                source_id=PULSE_SOURCE_ID,
            )
            try:
                report = agent.run(memory_ttl_days=config.news_memory_ttl_days)
            except NewsError as exc:
                raise HTTPException(
                    status_code=502, detail=f"News feed unavailable: {exc}"
                ) from exc
            return NewsIngestResult(
                source_id=report.source_id,
                fetched=report.fetched,
                stored=report.stored,
                duplicates=report.duplicates,
                long_term_memories=report.long_term_memories,
            )

        @app.get("/api/v1/tools/research")
        def research_tools(request: Request) -> ToolCatalog:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            agent = ResearchAgent(database_path, feed_url=settings.load().news_feed_url)
            runner = agent.runner()
            return ToolCatalog(
                agent_id="research",
                read_only=not runner.policy.allows_mutation,
                tools=[_tool_spec_status(spec) for spec in runner.permitted_specs()],
            )

        @app.get("/api/v1/agent/status")
        def agent_status(request: Request) -> AgentStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            config = settings.load()
            agent = ResearchAgent(database_path, feed_url=config.news_feed_url)
            return AgentStatus(
                agent_id="research",
                configured=config.model_route_configured,
                provider=config.provider.value,
                model=config.model_name,
                tools=[
                    _tool_spec_status(spec) for spec in agent.runner().permitted_specs()
                ],
            )

        @app.post("/api/v1/agent/run")
        def agent_run(request: Request, body: AgentRunRequest) -> AgentRunResponse:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            config = settings.load()
            agent = ResearchAgent(database_path, feed_url=config.news_feed_url)
            try:
                run = agent.run(body.task, max_turns=body.max_turns)
            except LLMNotConfigured as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except LLMClientError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except NewsError as exc:
                raise HTTPException(
                    status_code=502, detail=f"News feed unavailable: {exc}"
                ) from exc
            return AgentRunResponse(
                task=run.task,
                final_text=run.final_text,
                turns=run.turns,
                stopped=run.stopped,
                tool_calls=list(run.tool_calls),
                steps=[
                    AgentStepStatus(
                        turn=step.turn,
                        kind=step.kind,
                        tool=step.tool,
                        arguments=dict(step.arguments),
                        output=step.output,
                    )
                    for step in run.steps
                ],
                provider=run.provider,
                model=run.model,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
            )

        @app.post("/api/v1/tools/research/run")
        def research_tool_run(
            request: Request, body: ToolRunRequest
        ) -> ToolRunResponse:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            config = settings.load()
            agent = ResearchAgent(database_path, feed_url=config.news_feed_url)
            runner = agent.runner()
            try:
                result = runner.run(body.name, body.arguments)
            except ToolBlocked as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ToolError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except NewsError as exc:
                raise HTTPException(
                    status_code=502, detail=f"News feed unavailable: {exc}"
                ) from exc
            return ToolRunResponse(
                tool=str(result["tool"]),
                message=str(result["message"]),
                break_loop=bool(result["break_loop"]),
            )

        @app.post("/api/v1/llm/complete")
        def llm_complete(
            request: Request, body: LLMCompleteRequest
        ) -> LLMCompleteResponse:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            service = LLMService(settings)
            try:
                result = service.complete(prompt=body.prompt, system=body.system)
            except LLMNotConfigured as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except LLMClientError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return LLMCompleteResponse(
                provider=result.provider.value,
                model=result.model,
                text=result.text,
                reasoning=result.reasoning,
                finish_reason=result.finish_reason,
                truncated=result.result.truncated,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        # -- analysis ---------------------------------------------------
        #
        # Owner-only, `no-store` (the middleware above covers the prefix), and
        # CSRF + origin checked on the POST. A run is a real model spend, so the
        # body is validated before anything is called.

        @app.get("/api/v1/analysis/vendors")
        def analysis_vendors(request: Request) -> VendorsResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            config = settings.load()
            router = build_analysis_router(database_path, config)
            return VendorsResponse(
                kite_enabled=config.kite_enabled,
                chains=[
                    VendorChainStatus(
                        domain=domain.value,
                        vendors=[vendor.name for vendor in router.chain(domain)],
                    )
                    for domain in DataDomain
                ],
            )

        @app.post("/api/v1/analysis/run")
        def analysis_run(
            request: Request, body: AnalysisRunRequest
        ) -> AnalysisRunResponse:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")

            config = settings.load()
            try:
                symbol = resolve_symbol(body.symbol)
            except SymbolError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if not symbol.tradeable:
                raise HTTPException(
                    status_code=422,
                    detail="Indices can be analysed as context but not traded.",
                )

            pipeline = _analysis_pipeline(database_path, config, settings)
            try:
                result = pipeline.run(
                    symbol=symbol,
                    timeframe=body.timeframe,
                    equity_paise=body.equity_paise,
                    available_cash_paise=body.available_cash_paise,
                    risk_basis_points=body.risk_basis_points,
                )
            except PipelineError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            payload = result.to_dict()
            store = AnalysisRunStore(database_path)
            stored = store.record(
                symbol=result.symbol,
                timeframe=result.timeframe,
                as_of=result.as_of,
                started_at=result.as_of,
                vendor_quotes=result.vendor_quotes,
                vendor_daily=result.vendor_daily,
                is_realtime=result.is_realtime,
                data_freshness=(
                    "realtime_kite" if result.is_realtime else "delayed_yfinance"
                ),
                bar_count=result.report.bar_count,
                facts=result.report.facts(),
                composite=result.report.composite,
                debate_attempted=result.bull is not None or result.bear is not None,
                debate_rounds=result.debate_rounds,
                mode=result.mode,
                provider=config.provider.value,
                model=config.model_name,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                signal=("HOLD" if result.plan is None else result.plan.signal.value),
                plan=None if result.plan is None else result.plan.to_dict(),
                vetoes=result.vetoes,
                detail=payload,
            )
            return AnalysisRunResponse(
                run_id=stored.run_id,
                symbol=result.symbol,
                timeframe=result.timeframe,
                mode=result.mode,
                as_of=result.as_of.isoformat(),
                data_provenance=AnalysisProvenance(
                    vendor_daily=result.vendor_daily,
                    vendor_quotes=result.vendor_quotes,
                    is_realtime=result.is_realtime,
                    data_freshness=(
                        "realtime_kite" if result.is_realtime else "delayed_yfinance"
                    ),
                ),
                report=payload["report"],  # type: ignore[arg-type]
                plan=None if result.plan is None else result.plan.to_dict(),
                verdict=None if result.verdict is None else result.verdict.to_dict(),
                risk=None if result.risk is None else result.risk.to_dict(),
                vetoes=list(result.vetoes),
                notes=list(result.notes),
                skipped_nodes=list(result.skipped_nodes),
            )

        @app.post("/api/v1/analysis/batch")
        def analysis_batch(
            request: Request, body: BatchAnalysisRequest
        ) -> BatchAnalysisResponse:
            """Analyse several symbols in one submitted batch.

            Each symbol is independent: one failure is reported against that
            symbol and the rest still run, so a mistyped ticker does not waste the
            whole batch. Duplicates are collapsed before any vendor call.
            """
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")

            try:
                symbols = parse_symbol_list(body.symbols)
            except BatchError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            config = settings.load()
            pipeline = _analysis_pipeline(database_path, config, settings)
            try:
                batch = BatchAnalyst(pipeline).run(
                    symbols=symbols,
                    timeframe=body.timeframe,
                    equity_paise=body.equity_paise,
                    available_cash_paise=body.available_cash_paise,
                    risk_basis_points=body.risk_basis_points,
                )
            except BatchError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            store = AnalysisRunStore(database_path)
            items: list[BatchAnalysisItem] = []
            for item in batch.items:
                result = item.result
                # Only a successful analysis carries a payload to persist and
                # report; a failed item has an error string instead.
                analysis = None if result is None else result.to_dict()
                if result is not None:
                    assert analysis is not None
                    # Persist each successful analysis so the history stays
                    # complete even when the batch ran from the new tab.
                    stored = store.record(
                        symbol=result.symbol,
                        timeframe=result.timeframe,
                        as_of=result.as_of,
                        started_at=result.as_of,
                        vendor_quotes=result.vendor_quotes,
                        vendor_daily=result.vendor_daily,
                        is_realtime=result.is_realtime,
                        data_freshness=(
                            "realtime_kite"
                            if result.is_realtime
                            else "delayed_yfinance"
                        ),
                        bar_count=result.report.bar_count,
                        facts=result.report.facts(),
                        composite=result.report.composite,
                        debate_attempted=(
                            result.bull is not None or result.bear is not None
                        ),
                        debate_rounds=result.debate_rounds,
                        mode=result.mode,
                        provider=config.provider.value,
                        model=config.model_name,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        signal=(
                            "HOLD" if result.plan is None else result.plan.signal.value
                        ),
                        plan=None if result.plan is None else result.plan.to_dict(),
                        vetoes=result.vetoes,
                        detail=analysis,
                    )
                    # The pipeline's payload has no `run_id` — only the store
                    # mints one. Attach it so the batch item is the same shape
                    # as a single-run response and the UI can link to history.
                    analysis = {**analysis, "run_id": stored.run_id}
                items.append(
                    BatchAnalysisItem(
                        requested=item.requested,
                        symbol=item.symbol,
                        ok=item.ok,
                        error=item.error,
                        analysis=analysis,
                    )
                )
            return BatchAnalysisResponse(
                timeframe=batch.timeframe,
                as_of=batch.as_of.isoformat(),
                requested=len(batch.items),
                succeeded=batch.succeeded,
                failed=batch.failed,
                items=items,
            )

        @app.get("/api/v1/analysis/runs")
        def analysis_runs(
            request: Request, limit: int = 20, symbol: str | None = None
        ) -> AnalysisRunList:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            if not 1 <= limit <= 100:
                raise HTTPException(status_code=422, detail="Limit must be 1-100.")
            store = AnalysisRunStore(database_path)
            runs = store.recent(limit, symbol=symbol)
            return AnalysisRunList(
                runs=[
                    AnalysisRunSummary(
                        run_id=run.run_id,
                        symbol=run.symbol,
                        timeframe=run.timeframe,
                        as_of=run.as_of.isoformat(),
                        mode=run.mode,
                        signal=run.signal,
                        data_freshness=run.data_freshness,
                        is_realtime=run.is_realtime,
                        composite=run.composite,
                    )
                    for run in runs
                ]
            )

        @app.get("/api/v1/analysis/runs/{run_id}")
        def analysis_run_detail(request: Request, run_id: str) -> dict[str, object]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                return AnalysisRunStore(database_path).get(run_id).to_dict()
            except AnalysisRunError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.post("/api/v1/data/download")
        def data_download(
            request: Request, body: DataDownloadRequest
        ) -> DataDownloadResponse:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
            client = YahooDataClient(database_path)
            try:
                result = client.download(
                    body.symbol, range_=body.range, interval=body.interval
                )
            except YahooError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return DataDownloadResponse(
                import_id=result.import_id,
                symbol=result.symbol,
                record_count=result.record_count,
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
