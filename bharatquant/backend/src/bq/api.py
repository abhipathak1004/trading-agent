import asyncio
import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from hmac import compare_digest
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal, Protocol, runtime_checkable

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from bq.agent_catalogue import catalogue_for
from bq.agent_prompts import (
    MAX_PROMPT_CHARS,
    PromptError,
    PromptNotFound,
    PromptStore,
)
from bq.agent_run_store import AgentRunError, AgentRunStore
from bq.analysis.batch import BatchAnalyst, BatchError, parse_symbol_list
from bq.analysis.builder import build_analysis_pipeline
from bq.analysis.decision import TradePlan, TradePlanError, trade_plan_from_json
from bq.analysis.pipeline import AnalysisPipeline, AnalysisResult, PipelineError
from bq.analysis.run_settings import AnalysisRunError, AnalysisRunStore
from bq.analysis.technical import TechnicalReport
from bq.analysis.wiring import build_analysis_router
from bq.auth import (
    SESSION_SECONDS,
    InvalidCredentials,
    LoginLocked,
    OwnerAlreadyConfigured,
    OwnerAuth,
    OwnerNotConfigured,
)
from bq.backtest import BacktestEngineName, BacktestError, run_backtest
from bq.campaign import CampaignError, CampaignNotFound, CampaignService
from bq.campaign_config import CampaignConfig, CampaignConfigError
from bq.confidence import ConfidenceLevel
from bq.core import CurrencyPaise, Quantity, canonical_digest, paise_from_price
from bq.data.calendar import (
    ExchangeCalendar,
    SessionClass,
    SessionState,
    validate_coverage_query,
)
from bq.data.equity_master import EquityMasterError, EquityMasterStore
from bq.data.freshness import (
    DelayedDataMode,
    FreshnessStatus,
    freshness_policy_from_seconds,
    inspect_snapshot_freshness,
)
from bq.data.records import DataRecordStore, MissingDataRecord, QualityStatus
from bq.data.symbols import Symbol, SymbolError
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import BarInterval, DataDomain
from bq.data.yahoo import YahooDataClient, YahooError
from bq.decision.battery import SCREENER
from bq.decision.state import build_decision_state
from bq.evals.config import (
    EvalConfigError,
    parse_promptfoo_config,
    read_categories,
    resolve_paths,
    tool_availability,
    write_run_config,
)
from bq.evals.runner import EvalLaunch, EvalRunner
from bq.evals.scoring import ScoringError
from bq.evals.store import (
    EvalModelScore,
    EvalRun,
    EvalRunActive,
    EvalRunNotFound,
    EvalStore,
)
from bq.execution import (
    ExecutionAdapter,
    ExecutionContextError,
    ExecutionDisabled,
    ExecutionMode,
    ExecutionOutcome,
    ExecutionSnapshot,
    PaperExecutorError,
    build_execution_adapter,
    build_execution_context,
)
from bq.fundamentals import (
    FundamentalError,
    FundamentalSnapshot,
    FundamentalSource,
    ValuationAssumptions,
)
from bq.infra.equity_listing import ListingFetchError
from bq.infra.job_catalogue import AVAILABLE_JOBS, JOB_CATALOGUE
from bq.infra.job_handlers import MEMORY_RESOLVE_JOB, build_job_handlers
from bq.infra.job_worker import JobWorker, JobWorkerLoop
from bq.infra.jobs import JobRunStore
from bq.infra.model_attempts import ModelAttemptStore, TokenUsage
from bq.infra.scheduler import SchedulerLoop, SchedulerRunner
from bq.infra.schedules import ScheduleStore
from bq.infra.task_registry import TaskRegistry
from bq.infra.tunnel import TunnelManager, TunnelState, TunnelStatus
from bq.llm_client import LLMClientError, LLMNotConfigured
from bq.llm_service import LLMService
from bq.local_models import (
    LocalModel,
    LocalModelError,
    ModelPullError,
    ModelPullRunner,
    PullState,
    create_local_model,
    installed_ollama_models,
    model_is_installed,
    seed_from_provider_specs,
)
from bq.logbook import (
    DEFAULT_FLUSH_MINUTES,
    DEFAULT_RETENTION_DAYS,
    MAX_FLUSH_MINUTES,
    MAX_RETENTION_DAYS,
    MIN_FLUSH_MINUTES,
    MIN_RETENTION_DAYS,
    Logbook,
    LoggingConfig,
    LoggingConfigError,
    NullLogbook,
    configure_logbook,
    get_logbook,
)
from bq.memory import (
    EpisodicMemoryStore,
    MissingPlaybook,
    MissingWorkingMemory,
    OutcomeResolver,
    PlaybookStore,
    ProceduralError,
    ResolutionError,
    SemanticError,
    SemanticMemoryStore,
    WorkingMemoryError,
    WorkingMemoryStore,
)
from bq.memory.procedural import MAX_CONTENT_CHARS
from bq.model_router import (
    DecisionUnavailable,
    ModelRouter,
    decision_policy_for,
)
from bq.news import PULSE_SOURCE_ID, MemoryKind, NewsError, NewsStore
from bq.news_agent import NewsResearchAgent
from bq.opportunities import (
    OpportunityError,
    OpportunityHorizon,
    OpportunityLifecycle,
    OpportunitySide,
    OpportunityStore,
)
from bq.paper_accounts import (
    PaperAccountBalance,
    PaperAccountError,
    PaperAccountStatus,
    PaperAccountStore,
)
from bq.paper_square_off import PaperSquareOff
from bq.paper_stops import PaperStop, PaperStopError, PaperStopStore
from bq.paper_trading import (
    PaperApproval,
    PaperOrder,
    PaperOrderType,
    PaperProduct,
    PaperSide,
    PaperTradingError,
    PaperTradingStore,
)
from bq.reports.engine import ReportEngine, ReportEngineError
from bq.reports.store import ReportError, ReportStore
from bq.research import ResearchError, ResearchStore, Thesis, ThesisHorizon
from bq.research_agent import ResearchAgent
from bq.research_expansion import (
    FundamentalRecord,
    NoteSource,
    ResearchClaim,
    ResearchExpansionError,
    ResearchExpansionStore,
    ResearchNote,
    ThesisEventRecord,
)
from bq.risk.policy import KillSwitchLevel, RiskLimits
from bq.runtime import inspect_runtime, metrics_text
from bq.schedules import (
    DailySchedule,
    IntervalSchedule,
    ScheduleError,
    WakeDecision,
    WakePolicy,
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
from bq.strategies import StrategyError as LifecycleStrategyError
from bq.strategies import StrategyLifecycle
from bq.strategy_dsl import from_dict as strategy_from_dict
from bq.strategy_store import (
    BacktestRun,
    BacktestStore,
    StrategyRecord,
    StrategyStore,
    StrategyStoreError,
)
from bq.super_agent import StrategyError, SuperAgent
from bq.tools.base import ToolBlocked, ToolError, ToolSpec
from bq.triggers import TriggerEngine

WEB_ROOT = Path(__file__).with_name("static")
COOKIE_NAME = "bq_session"
ALLOWED_ORIGIN = "http://127.0.0.1:8000"


class Health(BaseModel):
    status: str


class RuntimeJobsStatus(BaseModel):
    pending: int
    running: int
    failed: int
    stale: int
    failed_24h: int
    last_event_at: str | None


class RuntimeCountsStatus(BaseModel):
    reports: int
    research_notes: int
    paper_orders: int
    predictions: int


class RuntimeStatus(BaseModel):
    status: str
    database: str
    database_bytes: int
    wal_bytes: int
    free_disk_bytes: int
    migration_count: int
    integrity: str
    jobs: RuntimeJobsStatus
    counts: RuntimeCountsStatus
    issues: list[str]
    checked_at: datetime


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


class PaperAccountListResponse(BaseModel):
    accounts: list[PaperAccountStatusResponse]


class PaperAccountCreateRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=128, strict=True)
    name: str = Field(min_length=1, max_length=128, strict=True)
    opening_balance_paise: int = Field(gt=0, le=10**13, strict=True)


class PaperPositionStatus(BaseModel):
    instrument_id: str
    quantity: int


class PaperOrderStatus(BaseModel):
    order_id: str
    account_id: str
    instrument_id: str
    client_order_id: str
    side: str
    order_type: str
    quantity: int
    limit_price_paise: str | None
    status: str
    filled_quantity: int
    created_at: datetime
    product: str
    approval_status: str


class PaperBookResponse(BaseModel):
    account: PaperAccountStatusResponse
    positions: list[PaperPositionStatus]
    orders: list[PaperOrderStatus]


class PaperFillRequest(BaseModel):
    quantity: int = Field(gt=0, le=10**9, strict=True)
    price_paise: int = Field(gt=0, le=10**13, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128, strict=True)


class PaperExitRequest(BaseModel):
    quantity: int = Field(gt=0, le=10**9, strict=True)
    price_paise: int = Field(gt=0, le=10**13, strict=True)
    client_order_id: str = Field(min_length=1, max_length=128, strict=True)
    product: Literal["CAS", "MIS"] = "MIS"


class PaperFillStatus(BaseModel):
    fill_id: str
    order_id: str
    quantity: int
    price_paise: str
    filled_at: datetime
    idempotency_key: str
    realized_pnl_paise: str | None
    gross_paise: str
    charges_paise: str
    net_pnl_paise: str | None


class PaperFillResponse(BaseModel):
    order: PaperOrderStatus
    fill: PaperFillStatus


class PaperReconciliationStatus(BaseModel):
    account_id: str
    order_count: int
    fill_count: int
    ledger_debit_paise: str
    ledger_credit_paise: str
    position_count: int
    charges_paise: str


class PaperApprovalStatus(BaseModel):
    approval_id: str
    order_id: str
    account_id: str
    status: str
    reason: str
    created_at: datetime
    decided_at: datetime | None
    decision_note: str | None


class PaperApprovalDecision(BaseModel):
    approved: bool = Field(strict=True)
    note: str = Field(default="", max_length=512, strict=True)


class PaperProductPositionStatus(BaseModel):
    instrument_id: str
    product: str
    quantity: int


class PaperTaxLotStatus(BaseModel):
    lot_id: str
    instrument_id: str
    product: str
    quantity: int
    remaining_quantity: int
    settles_on: str
    cost_paise: str
    charges_paise: str


class PaperStopStatus(BaseModel):
    stop_id: str
    account_id: str
    instrument_id: str
    product: str
    quantity: int
    stop_price_paise: str
    target_price_paise: str | None
    status: str
    trigger_order_id: str | None
    trigger_kind: str | None
    created_at: datetime
    triggered_at: datetime | None


class PaperStopCreateRequest(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=128, strict=True)
    quantity: int = Field(gt=0, le=10**9, strict=True)
    stop_price_paise: int = Field(gt=0, le=10**13, strict=True)
    target_price_paise: int | None = Field(default=None, gt=0, le=10**13)
    product: Literal["CAS", "MIS"] = "CAS"


class PaperStopEvaluationStatus(BaseModel):
    examined: int
    triggered: int
    notes: list[str]


class PaperSquareOffStatus(BaseModel):
    examined: int
    squared_off: int
    notes: list[str]


class PaperRealismStatus(BaseModel):
    account_id: str
    product_positions: list[PaperProductPositionStatus]
    tax_lots: list[PaperTaxLotStatus]
    approvals: list[PaperApprovalStatus]
    stops: list[PaperStopStatus]
    charges_paise: str


class StrategyRecordStatus(BaseModel):
    strategy_id: str
    version: str
    name: str
    kind: str
    lifecycle: str
    definition: dict[str, object]
    content_hash: str
    is_seed: bool
    created_at: datetime


class StrategyListResponse(BaseModel):
    strategies: list[StrategyRecordStatus]


class StrategyCreateRequest(BaseModel):
    definition: dict[str, object]
    lifecycle: Literal["DRAFT", "BACKTESTED", "PAPER_SHADOW", "PAPER_ACTIVE"] = "DRAFT"


class StrategyBacktestRequest(BaseModel):
    start: datetime
    end: datetime
    engine: Literal["FAST", "EVENT"] = "FAST"
    initial_capital_paise: int = Field(gt=0, le=10**13, strict=True)


class BacktestReviewRequest(BaseModel):
    decision: Literal["ACCEPTED", "REJECTED"]
    note: str = Field(default="", max_length=512, strict=True)


class BacktestStatus(BaseModel):
    run_id: str
    strategy_id: str
    version: str
    engine: str
    start: datetime
    end: datetime
    initial_capital_paise: str
    metrics: dict[str, object]
    trades: list[dict[str, object]]
    content_hash: str
    created_at: datetime
    review_decision: str | None
    review_note: str | None


class BacktestListResponse(BaseModel):
    runs: list[BacktestStatus]


class ResearchNoteCreateRequest(BaseModel):
    instrument_id: str | None = Field(default=None, max_length=128)
    source_type: Literal["NEWS", "YOUTUBE", "XBRL", "FILING", "OWNER"]
    source_id: str = Field(min_length=1, max_length=128, strict=True)
    url: str | None = Field(default=None, max_length=2_048)
    title: str = Field(min_length=1, max_length=512, strict=True)
    body: str = Field(min_length=1, max_length=20_000, strict=True)
    published_at: datetime
    known_at: datetime | None = None


class ResearchNoteStatus(BaseModel):
    note_id: str
    instrument_id: str | None
    source_type: str
    source_id: str
    url: str | None
    title: str
    body: str
    published_at: datetime
    known_at: datetime
    note_hash: str
    created_at: datetime


class ResearchNoteListResponse(BaseModel):
    notes: list[ResearchNoteStatus]


class NewsNoteRequest(BaseModel):
    instrument_id: str | None = Field(default=None, max_length=128)


class FundamentalSnapshotRequest(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=128, strict=True)
    source: Literal["XBRL", "OWNER"]
    period_end: datetime
    known_at: datetime
    revenue_paise: int = Field(ge=0, le=10**16, strict=True)
    ebitda_paise: int = Field(ge=0, le=10**16, strict=True)
    net_income_paise: int = Field(ge=-(10**16), le=10**16, strict=True)
    total_debt_paise: int = Field(ge=0, le=10**16, strict=True)
    cash_paise: int = Field(ge=0, le=10**16, strict=True)
    shares_outstanding: int = Field(gt=0, le=10**15, strict=True)
    book_value_paise: int = Field(gt=0, le=10**16, strict=True)
    fcf_paise: int = Field(ge=-(10**16), le=10**16, strict=True)


class FundamentalSnapshotStatus(BaseModel):
    snapshot_id: str
    instrument_id: str
    source: str
    period_end: datetime
    known_at: datetime
    revenue_paise: str
    ebitda_paise: str
    net_income_paise: str
    total_debt_paise: str
    cash_paise: str
    shares_outstanding: int
    book_value_paise: str
    fcf_paise: str
    snapshot_hash: str
    created_at: datetime


class FundamentalListResponse(BaseModel):
    instrument_id: str
    snapshots: list[FundamentalSnapshotStatus]


class DossierResponse(BaseModel):
    instrument_id: str
    as_of: datetime
    notes: list[ResearchNoteStatus]
    filings: list[dict[str, object]]
    theses: list[dict[str, object]]
    fundamentals: list[FundamentalSnapshotStatus]
    ratios: dict[str, object] | None
    dcf: dict[str, object] | None
    claims: list[dict[str, object]]


class ResearchClaimCreateRequest(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=128, strict=True)
    statement: str = Field(min_length=1, max_length=2_000, strict=True)
    note_id: str | None = Field(default=None, max_length=128)


class ResearchClaimStatus(BaseModel):
    claim_id: str
    instrument_id: str
    statement: str
    note_id: str | None
    claim_hash: str
    created_at: datetime
    status: str
    review_note: str | None


class ResearchClaimReviewRequest(BaseModel):
    decision: Literal["ACCEPTED", "REJECTED"]
    note: str = Field(default="", max_length=512, strict=True)


class ResearchClaimListResponse(BaseModel):
    claims: list[ResearchClaimStatus]


class ThesisCreateRequest(BaseModel):
    thesis_id: str = Field(min_length=1, max_length=128, strict=True)
    instrument_id: str = Field(min_length=1, max_length=128, strict=True)
    horizon: Literal["SHORT_TERM", "SWING", "MEDIUM", "LONG"]


class ThesisStatus(BaseModel):
    thesis_id: str
    instrument_id: str
    horizon: str
    state: str
    created_at: datetime
    thesis_hash: str


class ThesisEventCreateRequest(BaseModel):
    kind: Literal["EVIDENCE", "INVALIDATION", "REACTIVATION", "NOTE"]
    statement: str = Field(min_length=1, max_length=2_000, strict=True)
    note_id: str | None = Field(default=None, max_length=128)
    occurred_at: datetime | None = None


class ThesisEventStatus(BaseModel):
    event_id: str
    thesis_id: str
    kind: str
    statement: str
    note_id: str | None
    occurred_at: datetime
    event_hash: str
    created_at: datetime


class ExecutionLimitsRequest(BaseModel):
    max_symbol_exposure_paise: int = Field(ge=0, le=10**13, strict=True)
    max_open_positions: int = Field(ge=0, le=1_000, strict=True)
    max_daily_loss_paise: int = Field(ge=0, le=10**13, strict=True)
    max_weekly_loss_paise: int = Field(ge=0, le=10**13, strict=True)
    max_monthly_loss_paise: int = Field(ge=0, le=10**13, strict=True)
    max_order_rate_per_minute: int = Field(ge=1, le=1_000, strict=True)


class ExecutionRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=128, strict=True)
    plan: dict[str, object]
    mode: Literal["PAPER", "KITE_SANDBOX", "LIVE_APPROVAL"] = "PAPER"
    limits: ExecutionLimitsRequest
    kill_switch: Literal[
        "NONE",
        "PAUSE_STRATEGY",
        "FLATTEN_STRATEGY",
        "PAUSE_HORIZON",
        "HALT_ACCOUNT",
        "FLATTEN_ALL",
    ] = "NONE"
    session_open_override: bool | None = None


class ExecutionPlaceRequest(ExecutionRequest):
    client_order_id: str = Field(min_length=1, max_length=128, strict=True)
    product: Literal["CAS", "MIS"] = "MIS"
    require_approval: bool = False


class ExecutionFreshnessStatus(BaseModel):
    usable: bool
    label: str
    age_seconds: float


class ExecutionContextStatus(BaseModel):
    account_id: str
    account_name: str
    available_cash_paise: str
    balance_paise: str
    open_positions: int
    symbol_exposure_paise: str
    daily_loss_paise: str
    weekly_loss_paise: str
    monthly_loss_paise: str
    orders_this_minute: int
    kill_switch: str
    instrument_valid: bool
    session_open: bool
    session_state: str
    session_source: str
    notes: list[str]
    positions: list[PaperPositionStatus]


class ExecutionResponse(BaseModel):
    mode: str
    state: str
    vetoes: list[str]
    freshness: ExecutionFreshnessStatus
    context: ExecutionContextStatus
    order: PaperOrderStatus | None


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


class JobRunView(BaseModel):
    run_id: str
    job_id: str
    occurrence_key: str
    scheduled_at: datetime
    status: str
    attempts: int
    max_attempts: int
    failure_code: str | None


class JobDefinitionStatus(BaseModel):
    job_id: str
    label: str
    description: str
    resource_class: str
    timezone: str
    local_time: str
    window_start: str
    window_end: str
    enabled_by_default: bool
    max_attempts: int
    wake_policy: str


class JobCatalogueResponse(BaseModel):
    jobs: list[JobDefinitionStatus]


class ScheduleConfigStatus(BaseModel):
    schedule_id: str
    job_id: str
    enabled: bool
    timezone: str
    local_time: str
    window_start: str
    window_end: str
    wake_policy: str
    wake_within_seconds: int | None
    max_attempts: int
    interval_minutes: int | None
    weekdays: list[int]
    dependencies: list[str]
    version: int
    next_occurrences: list[datetime]


class ScheduleConfigList(BaseModel):
    version: int
    schedules: list[ScheduleConfigStatus]


class ScheduleUpdateRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128, strict=True)
    enabled: bool
    timezone: str = Field(min_length=1, max_length=64, strict=True)
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", strict=True)
    window_start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", strict=True)
    window_end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", strict=True)
    wake_policy: Literal["RUN_ON_WAKE_WITHIN", "SKIP", "ALWAYS_RUN"] = "ALWAYS_RUN"
    wake_within_seconds: int | None = Field(default=None, ge=1, le=86_400)
    max_attempts: int = Field(default=2, ge=1, le=10, strict=True)
    interval_minutes: int | None = Field(default=None, ge=1, le=1_440)
    weekdays: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4, 5, 6], max_length=7
    )
    dependencies: list[str] = Field(default_factory=list, max_length=16)


class ScheduleEnableRequest(BaseModel):
    enabled: bool


class ScheduleRunRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128, strict=True)


class ScheduleDryRunResponse(BaseModel):
    valid: bool
    schedule_id: str
    job_id: str


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
    job_run_count: int
    job_runs: list[JobRunView]


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


class AgentRunHistoryItem(BaseModel):
    run_id: str
    agent_id: str
    task: str
    final_text: str
    turns: int
    stopped: bool
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    started_at: str
    finished_at: str


class AgentRunListResponse(BaseModel):
    runs: list[AgentRunHistoryItem]


class AgentRunDetailResponse(BaseModel):
    run_id: str
    agent_id: str
    task: str
    final_text: str
    turns: int
    stopped: bool
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    started_at: str
    finished_at: str
    steps: list[AgentStepStatus]


class AgentStatus(BaseModel):
    agent_id: str
    configured: bool
    provider: str
    model: str
    tools: list[ToolSpecStatus]


class AgentCatalogueRoute(BaseModel):
    tier: str
    provider: str | None
    model: str | None
    configured: bool


class AgentCatalogueItem(BaseModel):
    agent_id: str
    kind: str
    label: str
    description: str
    tier: str
    prompt_id: str | None
    tools: list[str]
    read_only: bool
    route: AgentCatalogueRoute


class AgentCatalogueResponse(BaseModel):
    agents: list[AgentCatalogueItem]


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


class CampaignResearchRequest(BaseModel):
    as_of: str | None = None
    force: bool = False
    top_n: int | None = Field(default=None, ge=1, le=10)


class CampaignScanRequest(BaseModel):
    as_of: str | None = None


class CampaignSettingsRequest(BaseModel):
    config: dict[str, object]


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


class DecisionPreviewRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32, strict=True)
    timeframe: Literal["intraday", "swing"] = "swing"


class DecisionJudgmentStatus(BaseModel):
    name: str
    kind: str
    choice: str | None
    score: int | None
    noul: float | None
    confidence: float | None


class DecisionScreenerStatus(BaseModel):
    verdict: str
    reason: str
    quality: float
    worth: float
    confidence: float


class DecisionRouteStatus(BaseModel):
    tier: str
    provider: str
    model: str
    decision_source: str
    fallback_used: bool
    fallback_from: str | None
    latency_ms: float | None
    request_id: str | None
    model_mismatch: bool
    state_hash: str | None


class DecisionPreviewResponse(BaseModel):
    symbol: str
    timeframe: str
    as_of: str
    decision_preview: str
    data_provenance: AnalysisProvenance
    state: dict[str, object]
    judgments: list[DecisionJudgmentStatus]
    screener: DecisionScreenerStatus
    route: DecisionRouteStatus
    notes: list[str]


class PromptStatus(BaseModel):
    agent_id: str
    label: str
    description: str
    system: str
    template: str
    required_fields: list[str]
    version: int
    updated_at: str | None
    is_default: bool


class PromptList(BaseModel):
    prompts: list[PromptStatus]


class PromptUpdate(BaseModel):
    system: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS, strict=True)
    template: str = Field(default="", max_length=MAX_PROMPT_CHARS, strict=True)


class StrategyRunRequest(BaseModel):
    strategy: str = Field(min_length=1, max_length=2_000, strict=True)
    symbols: str = Field(min_length=1, max_length=512, strict=True)
    playbook_id: str | None = Field(default=None, max_length=64, strict=True)
    timeframe: Literal["intraday", "swing"] = "swing"
    equity_paise: int = Field(gt=0, le=10**13, strict=True)
    available_cash_paise: int = Field(ge=0, le=10**13, strict=True)
    risk_basis_points: int = Field(default=100, ge=0, le=10_000, strict=True)
    max_positions: int = Field(default=2, ge=1, le=10, strict=True)


class MemoryCalibrationBucket(BaseModel):
    decision_source: str
    model: str
    count: int
    mean_brier: float | None
    mean_log_loss: float | None
    expected_calibration_error: float | None
    outcome_rate: float | None


class MemoryCalibrationResponse(BaseModel):
    as_of: str
    buckets: list[MemoryCalibrationBucket]


class SemanticHitStatus(BaseModel):
    doc_id: str
    symbol: str
    source_url: str
    title: str
    snippet: str
    published_at: str
    known_at: str
    score: float


class SemanticSearchResponse(BaseModel):
    query: str
    as_of: str
    hits: list[SemanticHitStatus]


class SemanticIngestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=64, strict=True)
    source_url: str = Field(min_length=1, max_length=2048, strict=True)
    title: str = Field(min_length=1, max_length=512, strict=True)
    body: str = Field(min_length=1, max_length=16_384, strict=True)
    published_at: datetime
    known_at: datetime | None = None


class SemanticDocumentStatus(BaseModel):
    doc_id: str
    symbol: str
    source_url: str
    title: str
    snippet: str
    published_at: datetime
    known_at: datetime
    recorded_at: datetime
    content_hash: str


class MemoryResolveRequest(BaseModel):
    horizon_days: int = Field(default=5, ge=1, le=60, strict=True)
    max_episodes: int = Field(default=50, ge=1, le=200, strict=True)


class MemoryResolveResponse(BaseModel):
    examined: int
    resolved: int
    skipped: int
    failed: int
    notes: list[str]


class MemoryScheduleRequest(BaseModel):
    enabled: bool


class MemoryScheduleStatus(BaseModel):
    configured: bool
    enabled: bool
    schedule_id: str | None
    version: int | None
    local_time: str | None
    timezone: str | None


class PlaybookStatus(BaseModel):
    playbook_id: str
    name: str
    description: str
    content: str
    version: int
    updated_at: str | None
    is_default: bool


class PlaybookList(BaseModel):
    playbooks: list[PlaybookStatus]


class PlaybookUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS, strict=True)


class StrategyRunResponse(BaseModel):
    run_id: str | None = None
    strategy: str
    timeframe: str
    as_of: str
    spec: dict[str, object]
    symbols: list[dict[str, object]]
    decision: dict[str, object]
    accepted: list[dict[str, object]]
    rejected: list[dict[str, object]]
    notes: list[str]


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
    specialists: list[dict[str, object]]
    evidence: dict[str, object] | None
    final_gate: dict[str, object] | None
    confidence: dict[str, object] | None = None


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


class WorkingMemoryStatus(BaseModel):
    run_id: str
    symbol: str
    source: str
    as_of: datetime
    schema_version: int
    state: dict[str, object]
    state_hash: str
    recorded_at: datetime


class WorkingMemoryResponse(BaseModel):
    run_id: str
    memories: list[WorkingMemoryStatus]


class OpportunityStatus(BaseModel):
    opportunity_id: str
    instrument_id: str
    horizon: str
    side: str
    created_at: datetime
    probability: float
    level: str
    disposition: str
    confirming_families: int
    base_rate_samples: int
    calibrated: bool
    ev_r_after_costs: float
    vetoes: list[str]
    assessment_hash: str
    score: float
    run_id: str | None
    source: str | None
    setup_id: str | None
    confidence: dict[str, object] | None
    plan: dict[str, object] | None
    lifecycle: str


class OpportunityListResponse(BaseModel):
    opportunities: list[OpportunityStatus]


class OpportunityTransitionRequest(BaseModel):
    lifecycle: str
    note: str | None = Field(default=None, max_length=500, strict=True)


class ShadowSummaryStatus(BaseModel):
    verdict: str
    suggested_count: int
    unsuggested_count: int
    suggested_win_rate: float | None
    unsuggested_win_rate: float | None
    suggested_interval: list[float] | None
    unsuggested_interval: list[float] | None


class ReportStatus(BaseModel):
    report_id: str
    report_type: str
    report_date: str
    status: str
    title: str
    body_md: str
    body: dict[str, object]
    content_hash: str
    file_path: str | None
    created_at: datetime


class ReportListResponse(BaseModel):
    reports: list[ReportStatus]


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
    custom_endpoint: str | None = None


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


class LocalModelPullStatus(BaseModel):
    status: str
    output: str = ""


class LocalModelStatus(BaseModel):
    id: str
    label: str
    kind: str
    model: str
    endpoint: str | None
    installed: bool | None
    pull: LocalModelPullStatus | None


class LocalModelList(BaseModel):
    models: list[LocalModelStatus]


class LocalModelCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80, strict=True)
    kind: Literal["ollama", "openai_compatible"]
    model: str = Field(min_length=1, max_length=200, strict=True)
    endpoint: str | None = Field(default=None, max_length=4096)
    num_ctx: int = Field(default=16_384, strict=True)
    num_predict: int = Field(default=512, strict=True)
    temperature: float = 0.0
    think: bool = False


class LocalModelAction(BaseModel):
    model_id: str = Field(min_length=1, max_length=300, strict=True)


class EvalProviderStatus(BaseModel):
    id: str
    label: str
    model: str


class EvalConfigStatus(BaseModel):
    providers: list[EvalProviderStatus]
    categories: list[str]
    tools: dict[str, bool]
    config_path: str
    dataset_path: str
    active_run_id: str | None
    custom_endpoint: str | None


class EvalModelScoreStatus(BaseModel):
    model: str
    total_cases: int
    overall_pass_rate: float
    accuracy_pass_rate: float
    json_parse_rate: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    brier_score: float | None
    ece_5bin: float | None
    brier_n: int
    category_rates: dict[str, float | None]


class EvalRunStatus(BaseModel):
    id: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    status: str
    models: list[str]
    categories: list[str]
    config_path: str
    results_json_path: str | None
    error_message: str | None
    scores: list[EvalModelScoreStatus] = Field(default_factory=list)


class EvalRunList(BaseModel):
    runs: list[EvalRunStatus]


class EvalCaseStatus(BaseModel):
    description: str
    model: str
    category: str
    passed: bool
    input: str
    raw_output: str
    confidence: float | None
    latency_ms: float | None
    failure_reasons: list[str]


class EvalCaseList(BaseModel):
    run_id: str
    model: str | None
    category: str | None
    status: str
    total: int
    cases: list[EvalCaseStatus]


class EvalRunCreate(BaseModel):
    providers: list[str] = Field(default_factory=list, max_length=32)
    categories: list[str] = Field(default_factory=list, max_length=32)
    no_cache: bool = False


class EvalPromoteRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200, strict=True)
    provider: Provider = Provider.CUSTOM
    agent_id: str = Field(min_length=1, max_length=64, strict=True)
    endpoint: str | None = Field(default=None, max_length=2048)


class EvalPromoteDiff(BaseModel):
    agent_id: str
    before: dict[str, str | None]
    after: dict[str, str | None]
    yaml_before: str
    yaml_after: str


class LogFileStatus(BaseModel):
    name: str
    size_bytes: int
    modified_at: datetime


class LogEntryStatus(BaseModel):
    ts: str
    kind: str
    level: str
    truncated: bool
    data: dict[str, object]


class LoggingStatus(BaseModel):
    enabled: bool
    retention_days: int
    flush_minutes: int
    directory: str
    pending_bytes: int
    dropped: int
    files: list[LogFileStatus]
    recent: list[LogEntryStatus]


class LoggingUpdate(BaseModel):
    enabled: bool = Field(strict=True)
    retention_days: int = Field(
        ge=MIN_RETENTION_DAYS, le=MAX_RETENTION_DAYS, strict=True
    )
    flush_minutes: int = Field(ge=MIN_FLUSH_MINUTES, le=MAX_FLUSH_MINUTES, strict=True)


class LogFlushResult(BaseModel):
    written: int
    files: list[str]
    pruned: list[str]
    status: LoggingStatus


class TunnelStatusView(BaseModel):
    state: TunnelState
    url: str | None
    started_at: datetime | None
    error: str | None
    binary_path: str | None
    recent_log: list[str]


def _tunnel_status(status: TunnelStatus) -> TunnelStatusView:
    return TunnelStatusView(
        state=status.state,
        url=status.url,
        started_at=status.started_at,
        error=status.error,
        binary_path=status.binary_path,
        recent_log=list(status.recent_log),
    )


class TunnelHostMiddleware:
    """Allow loopback plus an active tunnel host; reject everything else.

    Replaces the static ``TrustedHostMiddleware`` so the tunnel host is admitted
    only while the manager reports it as live. With no tunnel the allowlist is
    loopback-only, identical to before this feature existed.
    """

    def __init__(self, app: ASGIApp, manager: TunnelManager | None) -> None:
        self.app = app
        self.manager = manager

    def _allowed(self) -> frozenset[str]:
        hosts = {"127.0.0.1"}
        if self.manager is not None:
            hosts |= self.manager.allowed_hosts()
        return frozenset(hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        host_header = headers.get(b"host", b"").decode("latin-1")
        host = _bare_host(host_header)
        if host not in self._allowed():
            response = JSONResponse({"detail": "Invalid host header"}, status_code=400)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _bare_host(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end > 0 else text
    return text.rsplit(":", 1)[0] if ":" in text else text


class RiskLimitsStatus(BaseModel):
    configured: bool
    max_symbol_exposure_paise: int | None
    max_open_positions: int | None
    max_daily_loss_paise: int | None
    max_weekly_loss_paise: int | None
    max_monthly_loss_paise: int | None
    max_order_rate_per_minute: int | None


class RiskLimitsUpdate(BaseModel):
    max_symbol_exposure_paise: int = Field(ge=0, le=10**13, strict=True)
    max_open_positions: int = Field(ge=0, le=1_000, strict=True)
    max_daily_loss_paise: int = Field(ge=0, le=10**13, strict=True)
    max_weekly_loss_paise: int = Field(ge=0, le=10**13, strict=True)
    max_monthly_loss_paise: int = Field(ge=0, le=10**13, strict=True)
    max_order_rate_per_minute: int = Field(ge=1, le=1_000, strict=True)


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


def _logging_status(book: Logbook | NullLogbook) -> LoggingStatus:
    snapshot = book.status()
    files: list[LogFileStatus] = []
    raw_files = snapshot.get("files")
    if isinstance(raw_files, list):
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            try:
                modified_at = datetime.fromisoformat(
                    str(item.get("modified_at", "")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            size = item.get("size_bytes")
            files.append(
                LogFileStatus(
                    name=str(item.get("name", "")),
                    size_bytes=size if isinstance(size, int) else 0,
                    modified_at=modified_at,
                )
            )
    recent: list[LogEntryStatus] = []
    raw_recent = snapshot.get("recent")
    if isinstance(raw_recent, list):
        for item in raw_recent:
            if not isinstance(item, dict):
                continue
            data = item.get("data")
            recent.append(
                LogEntryStatus(
                    ts=str(item.get("ts", "")),
                    kind=str(item.get("kind", "system")),
                    level=str(item.get("level", "info")),
                    truncated=bool(item.get("truncated", False)),
                    data=data if isinstance(data, dict) else {},
                )
            )
    enabled = snapshot.get("enabled")
    retention = snapshot.get("retention_days")
    flush_minutes = snapshot.get("flush_minutes")
    pending = snapshot.get("pending_bytes")
    dropped = snapshot.get("dropped")
    directory = snapshot.get("directory")
    return LoggingStatus(
        enabled=enabled if isinstance(enabled, bool) else True,
        retention_days=(
            retention if isinstance(retention, int) else DEFAULT_RETENTION_DAYS
        ),
        flush_minutes=(
            flush_minutes if isinstance(flush_minutes, int) else DEFAULT_FLUSH_MINUTES
        ),
        directory=directory if isinstance(directory, str) else "",
        pending_bytes=pending if isinstance(pending, int) else 0,
        dropped=dropped if isinstance(dropped, int) else 0,
        files=files,
        recent=recent,
    )


def _risk_limits_status(limits: RiskLimits | None) -> RiskLimitsStatus:
    if limits is None:
        return RiskLimitsStatus(
            configured=False,
            max_symbol_exposure_paise=None,
            max_open_positions=None,
            max_daily_loss_paise=None,
            max_weekly_loss_paise=None,
            max_monthly_loss_paise=None,
            max_order_rate_per_minute=None,
        )
    return RiskLimitsStatus(
        configured=True,
        max_symbol_exposure_paise=limits.max_symbol_exposure_paise,
        max_open_positions=limits.max_open_positions,
        max_daily_loss_paise=limits.max_daily_loss_paise,
        max_weekly_loss_paise=limits.max_weekly_loss_paise,
        max_monthly_loss_paise=limits.max_monthly_loss_paise,
        max_order_rate_per_minute=limits.max_order_rate_per_minute,
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
        custom_endpoint=config.custom_endpoint,
    )


def _eval_score_status(score: EvalModelScore) -> EvalModelScoreStatus:
    return EvalModelScoreStatus(
        model=score.model,
        total_cases=score.total_cases,
        overall_pass_rate=score.overall_pass_rate,
        accuracy_pass_rate=score.accuracy_pass_rate,
        json_parse_rate=score.json_parse_rate,
        p50_ms=score.p50_ms,
        p95_ms=score.p95_ms,
        max_ms=score.max_ms,
        brier_score=score.brier_score,
        ece_5bin=score.ece_5bin,
        brier_n=score.brier_n,
        category_rates=score.category_rates,
    )


def _eval_run_status(run: EvalRun, scores: list[EvalModelScore]) -> EvalRunStatus:
    return EvalRunStatus(
        id=run.id,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        status=run.status,
        models=list(run.models),
        categories=list(run.categories),
        config_path=run.config_path,
        results_json_path=run.results_json_path,
        error_message=run.error_message,
        scores=[_eval_score_status(score) for score in scores],
    )


def _agent_settings_yaml(config: AppConfig) -> str:
    """A readable projection of the SQLite routing; never read back as truth."""
    lines = [
        "# Projection of the SQLite agent routing (SettingsStore).",
        "# Editing this file does not change runtime behaviour.",
        "agents:",
    ]
    for agent_id, route in config.agent_routing:
        lines.append(f"  {agent_id}:")
        lines.append(f"    tier: {route.tier.value}")
        if route.provider is not None:
            lines.append(f"    provider: {route.provider.value}")
        if route.model is not None:
            lines.append(f"    model: {route.model}")
    return "\n".join(lines) + "\n"


def _technical_report_for(
    database_path: Path, config: AppConfig, symbol: Symbol, timeframe: str
) -> tuple[TechnicalReport, bool, str, str, tuple[str, ...]]:
    """Run only the deterministic front half of the pipeline for a preview."""
    router = build_analysis_router(database_path, config)
    result = AnalysisPipeline(router).run(
        symbol=symbol,
        timeframe=timeframe,
        equity_paise=100_000_00,
        available_cash_paise=50_000_00,
        risk_basis_points=100,
    )
    return (
        result.report,
        result.is_realtime,
        result.vendor_daily,
        result.vendor_quotes,
        tuple(result.notes),
    )


def _regime_label(result: AnalysisResult) -> str | None:
    for brief in result.specialists:
        if brief.agent != "market_regime":
            continue
        for fact in brief.facts:
            if fact.startswith("Regime:"):
                value = fact[len("Regime:") :].strip().rstrip(".")
                return value[:32] if value else None
    return None


MEMORY_SCHEDULE_ID = "memory-resolve-daily"


def _default_memory_schedule(*, enabled: bool) -> DailySchedule:
    return DailySchedule(
        schedule_id=MEMORY_SCHEDULE_ID,
        job_id=MEMORY_RESOLVE_JOB,
        enabled=enabled,
        timezone="Asia/Kolkata",
        local_time=time(18, 0),
        window_start=time(17, 0),
        window_end=time(19, 0),
        wake_policy=WakePolicy.ALWAYS_RUN,
        wake_within_seconds=None,
        max_attempts=2,
    )


def _memory_schedule_status(
    schedules: tuple[DailySchedule, ...], *, version: int
) -> MemoryScheduleStatus:
    schedule = next(
        (item for item in schedules if item.job_id == MEMORY_RESOLVE_JOB), None
    )
    if schedule is None:
        return MemoryScheduleStatus(
            configured=False,
            enabled=False,
            schedule_id=None,
            version=version,
            local_time=None,
            timezone=None,
        )
    return MemoryScheduleStatus(
        configured=True,
        enabled=schedule.enabled,
        schedule_id=schedule.schedule_id,
        version=version,
        local_time=schedule.local_time.isoformat(timespec="minutes"),
        timezone=schedule.timezone,
    )


def _record_working_memory(
    database_path: Path, run_id: str, result: AnalysisResult, *, source: str
) -> None:
    """Persist the typed working state without breaking the analysis response."""
    try:
        WorkingMemoryStore(database_path).record(result, run_id=run_id, source=source)
    except Exception:  # noqa: BLE001 - memory is an audit side effect
        return


def _record_opportunity(
    database_path: Path, run_id: str, result: AnalysisResult, *, source: str
) -> None:
    """Persist a confidence candidate and its shadow trade without breaking a run."""
    if result.confidence is None or result.plan is None:
        return
    try:
        confidence = result.confidence
        plan = result.plan
        horizon = (
            OpportunityHorizon.INTRADAY
            if result.timeframe == "intraday"
            else OpportunityHorizon.SWING
        )
        opportunity_id = (
            "opp_"
            + canonical_digest(
                {
                    "run_id": run_id,
                    "symbol": result.symbol,
                    "as_of": result.as_of.isoformat(),
                    "setup": confidence.base_rate.setup_id,
                }
            )[:24]
        )
        store = OpportunityStore(database_path)
        opportunity = store.freeze(
            opportunity_id=opportunity_id,
            instrument_id=result.symbol,
            horizon=horizon,
            side=OpportunitySide.BUY,
            assessment=confidence.assessment,
            run_id=run_id,
            source=source,
            setup_id=confidence.base_rate.setup_id,
            confidence=confidence.to_dict(),
            plan=plan.to_dict(),
        )
        store.record_shadow(
            opportunity,
            as_of=result.as_of,
            entry_price_paise=paise_from_price(plan.entry_price).value,
            stop_loss_paise=paise_from_price(plan.stop_loss).value,
            target_price_paise=paise_from_price(plan.target_price).value,
            quantity=plan.suggested_quantity,
            not_taken_reason=(
                "paper_not_executed"
                if confidence.assessment.disposition.value == "SUGGEST"
                else "not_suggested"
            ),
            plan=plan.to_dict(),
        )
    except Exception:  # noqa: BLE001 - opportunity tracking is an audit side effect
        return


def _record_analysis_episode(
    database_path: Path, run_id: str, result: AnalysisResult, *, source: str
) -> None:
    """Record a shadow episode without letting memory break the response."""
    plan = result.plan
    gate = result.final_gate
    routing = result.model_routing
    action = "BUY" if plan is not None and plan.signal.value == "BUY" else "HOLD"
    if gate is not None:
        decision_source = gate.outcome.source
        model = gate.outcome.model
        execute = gate.outcome.judgments.answer("execute_trade")
        probability = execute.noul
    elif routing:
        last = routing[-1]
        decision_source = last.decision_source
        model = last.model
        probability = None
    else:
        decision_source = "rules_only" if result.mode == "RULES_ONLY" else "llm_direct"
        model = "none"
        probability = None
    try:
        EpisodicMemoryStore(database_path).record_shadow(
            run_id=run_id,
            symbol=result.symbol,
            as_of=result.as_of,
            source=source,
            decision_source=decision_source,
            model=model or "none",
            action=action,
            plan=plan,
            predicted_probability=probability,
            regime=_regime_label(result),
            detail={
                "mode": result.mode,
                "signal": action,
                "vetoes": list(result.vetoes),
                "final_gate": None if gate is None else gate.to_dict(),
                "last_close": str(result.report.last_close),
                "source": source,
            },
        )
    except Exception:  # memory is an audit layer, not a reason to fail a run
        pass


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


async def _log_flush_loop(store: SettingsStore) -> None:
    """Flush the operational logbook at the configured cadence.

    Sleeping in <=60 s steps keeps shutdown responsive without re-reading SQLite
    on every tick. The first flush happens at startup so a spool left behind by a
    restart is picked up immediately.
    """
    while True:
        config = LoggingConfig()
        try:
            config = store.load().logging
            get_logbook().configure(config)
            await asyncio.to_thread(get_logbook().flush)
        except Exception:  # noqa: BLE001 - logging must never stop the server
            config = LoggingConfig()
        remaining = float(config.flush_minutes * 60)
        while remaining > 0:
            step = min(remaining, 60.0)
            await asyncio.sleep(step)
            remaining -= step


def create_app(
    web_root: Path | None = None,
    *,
    database_path: Path | None = None,
    tunnel_manager: TunnelManager | None = None,
) -> FastAPI:
    scheduler: SchedulerLoop | None = None
    worker_loop: JobWorkerLoop | None = None
    settings_store: SettingsStore | None = None
    eval_runner: EvalRunner | None = None
    flush_task: asyncio.Task[None] | None = None
    tunnel = tunnel_manager
    if tunnel is None and database_path is not None:
        # Built before add_middleware so the host gate sees this same instance.
        tunnel = TunnelManager(database_path.parent, database_path=database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal flush_task
        del app
        if scheduler is not None:
            scheduler.start()
        if worker_loop is not None:
            worker_loop.start()
        if settings_store is not None:
            flush_task = asyncio.create_task(_log_flush_loop(settings_store))
        try:
            yield
        finally:
            if flush_task is not None:
                flush_task.cancel()
                with suppress(asyncio.CancelledError):
                    await flush_task
            if eval_runner is not None:
                await eval_runner.stop()
            await pull_runner.stop()
            if tunnel is not None:
                await tunnel.stop()
            if worker_loop is not None:
                await worker_loop.stop()
            if scheduler is not None:
                await scheduler.stop()

    app = FastAPI(title="BharatQuant", lifespan=lifespan)
    app.add_middleware(TunnelHostMiddleware, manager=tunnel)

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
                "/api/v1/logs",
                "/api/v1/news/",
                "/api/v1/schedules",
                "/api/v1/settings/",
                "/api/v1/system/",
                "/api/v1/paper/",
                "/api/v1/tools/",
                "/api/v1/agent/",
                "/api/v1/analysis/",
                "/api/v1/decision/",
                "/api/v1/evals/",
                "/api/v1/strategy/",
                "/api/v1/research/",
                "/api/v1/strategies",
                "/api/v1/backtests",
                "/api/v1/memory/",
                "/api/v1/opportunities",
                "/api/v1/reports",
                "/api/v1/prompts/",
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
        tunnel_manager = tunnel
        auth = OwnerAuth(database_path)
        calendar = ExchangeCalendar(database_path)
        records = DataRecordStore(database_path)
        attempts = ModelAttemptStore(database_path)
        schedules = ScheduleStore(database_path, available_jobs=AVAILABLE_JOBS)
        paper_accounts = PaperAccountStore(database_path)
        paper_trading = PaperTradingStore(database_path)
        settings = SettingsStore(database_path)
        settings_store = settings
        try:
            configure_logbook(database_path.parent, settings.load().logging)
        except Exception:  # noqa: BLE001 - startup must not fail on logging
            configure_logbook(database_path.parent, LoggingConfig())
        job_store = JobRunStore(database_path)
        scheduler = SchedulerLoop(
            SchedulerRunner(
                database_path,
                schedule_store=schedules,
                job_store=job_store,
            )
        )
        worker_loop = JobWorkerLoop(
            JobWorker(job_store, build_job_handlers(database_path, settings))
        )
        evals = EvalStore(database_path)
        eval_runner = EvalRunner(evals, resolve_paths())
        pull_runner = ModelPullRunner()

        def effective_local_models() -> tuple[LocalModel, ...]:
            config = settings.load()
            if config.local_models_configured or config.local_models:
                return config.local_models
            try:
                text = resolve_paths().config.read_text(encoding="utf-8")
                specs = [
                    (provider.id, provider.label)
                    for provider in parse_promptfoo_config(text)
                ]
            except (EvalConfigError, OSError):
                return ()
            return seed_from_provider_specs(
                specs, default_endpoint=config.custom_endpoint
            )

        def local_model_status(
            model: LocalModel, installed: set[str] | None
        ) -> LocalModelStatus:
            pull = pull_runner.state(model.id)
            return LocalModelStatus(
                id=model.id,
                label=model.label,
                kind=model.kind,
                model=model.model,
                endpoint=model.endpoint,
                installed=model_is_installed(model, installed),
                pull=(
                    None
                    if pull is None
                    else LocalModelPullStatus(status=pull.status, output=pull.output)
                ),
            )

        def model_list_response() -> LocalModelList:
            installed = installed_ollama_models()
            return LocalModelList(
                models=[
                    local_model_status(model, installed)
                    for model in effective_local_models()
                ]
            )

        def verify_origin(request: Request) -> None:
            origin = request.headers.get("origin")
            allowed = {ALLOWED_ORIGIN}
            if tunnel_manager is not None:
                allowed |= tunnel_manager.allowed_origins()
            if origin is None or origin not in allowed:
                raise HTTPException(status_code=403, detail="Invalid request origin")

        def require_write(request: Request) -> None:
            csrf = auth.csrf_for_session(request.cookies.get(COOKIE_NAME))
            if csrf is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            verify_origin(request)
            if not compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                csrf.encode("ascii"),
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")

        def cookie_is_secure(request: Request) -> bool:
            """Mark the cookie Secure only when the login arrives over the tunnel."""
            if tunnel_manager is None:
                return False
            origin = request.headers.get("origin")
            return origin is not None and origin in tunnel_manager.allowed_origins()

        def account_status(balance: PaperAccountBalance) -> PaperAccountStatusResponse:
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

        def order_status(order: PaperOrder) -> PaperOrderStatus:
            return PaperOrderStatus(
                order_id=order.order_id,
                account_id=order.account_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                side=order.side.value,
                order_type=order.order_type.value,
                quantity=order.quantity,
                limit_price_paise=(
                    str(order.limit_price_paise)
                    if order.limit_price_paise is not None
                    else None
                ),
                status=order.status.value,
                filled_quantity=order.filled_quantity,
                created_at=order.created_at,
                product=order.product.value,
                approval_status=order.approval_status,
            )

        def execution_context_status(
            snapshot: ExecutionSnapshot,
        ) -> ExecutionContextStatus:
            return ExecutionContextStatus(
                account_id=snapshot.context.account_id,
                account_name=snapshot.account_name,
                available_cash_paise=str(snapshot.context.available_cash_paise),
                balance_paise=str(snapshot.balance_paise),
                open_positions=snapshot.context.open_positions,
                symbol_exposure_paise=str(snapshot.context.symbol_exposure_paise),
                daily_loss_paise=str(snapshot.context.daily_loss_paise),
                weekly_loss_paise=str(snapshot.context.weekly_loss_paise),
                monthly_loss_paise=str(snapshot.context.monthly_loss_paise),
                orders_this_minute=snapshot.context.orders_this_minute,
                kill_switch=snapshot.context.kill_switch.value,
                instrument_valid=snapshot.context.instrument_valid,
                session_open=snapshot.context.session_open,
                session_state=snapshot.session_state,
                session_source=snapshot.session_source,
                notes=list(snapshot.notes),
                positions=[
                    PaperPositionStatus(
                        instrument_id=item.instrument_id, quantity=item.quantity
                    )
                    for item in snapshot.positions
                ],
            )

        def execution_response(
            outcome: ExecutionOutcome, snapshot: ExecutionSnapshot
        ) -> ExecutionResponse:
            execution = outcome.execution
            return ExecutionResponse(
                mode=outcome.mode.value,
                state=execution.decision.state.value,
                vetoes=list(execution.decision.vetoes),
                freshness=ExecutionFreshnessStatus(
                    usable=execution.freshness.usable,
                    label=execution.freshness.label,
                    age_seconds=execution.freshness.age_seconds,
                ),
                context=execution_context_status(snapshot),
                order=order_status(execution.order) if execution.order else None,
            )

        def prepare_execution(
            body: ExecutionRequest,
        ) -> tuple[TradePlan, ExecutionAdapter, ExecutionSnapshot]:
            try:
                plan = trade_plan_from_json(body.plan)
            except TradePlanError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            limits = RiskLimits(
                max_symbol_exposure_paise=body.limits.max_symbol_exposure_paise,
                max_open_positions=body.limits.max_open_positions,
                max_daily_loss_paise=body.limits.max_daily_loss_paise,
                max_weekly_loss_paise=body.limits.max_weekly_loss_paise,
                max_monthly_loss_paise=body.limits.max_monthly_loss_paise,
                max_order_rate_per_minute=body.limits.max_order_rate_per_minute,
            )
            kill_switch = KillSwitchLevel(body.kill_switch)
            try:
                snapshot = build_execution_context(
                    database_path,
                    plan,
                    account_id=body.account_id,
                    now=datetime.now(UTC),
                    kill_switch=kill_switch,
                    session_open_override=body.session_open_override,
                )
            except ExecutionContextError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            try:
                adapter = build_execution_adapter(
                    ExecutionMode(body.mode), database_path, limits=limits
                )
            except ExecutionDisabled as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return plan, adapter, snapshot

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

        @app.get("/api/v1/system/status")
        def system_status(request: Request) -> RuntimeStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                payload = inspect_runtime(database_path)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return RuntimeStatus.model_validate(payload)

        @app.get("/api/v1/system/metrics")
        def system_metrics(request: Request) -> Response:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            payload = inspect_runtime(database_path)
            return Response(
                content=metrics_text(payload),
                media_type="text/plain; version=0.0.4",
            )

        @app.websocket("/api/v1/events")
        async def runtime_events(websocket: WebSocket) -> None:
            if auth.csrf_for_session(websocket.cookies.get(COOKIE_NAME)) is None:
                await websocket.close(code=1008)
                return
            await websocket.accept()
            last_payload: dict[str, object] | None = None
            last_sent: datetime | None = None
            try:
                while True:
                    payload = await asyncio.to_thread(inspect_runtime, database_path)
                    now = datetime.now(UTC)
                    if (
                        payload != last_payload
                        or last_sent is None
                        or now - last_sent >= timedelta(seconds=15)
                    ):
                        await websocket.send_json({"type": "runtime", "data": payload})
                        last_payload = payload
                        last_sent = now
                    try:
                        await asyncio.wait_for(websocket.receive_text(), timeout=5)
                    except TimeoutError:
                        continue
            except WebSocketDisconnect:
                return
            except Exception:  # noqa: BLE001 - closing a broken socket is enough
                await websocket.close(code=1011)

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

        @app.get("/api/v1/paper/accounts")
        def paper_account_list(request: Request) -> PaperAccountListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return PaperAccountListResponse(
                accounts=[
                    account_status(paper_accounts.get_balance(account.account_id))
                    for account in paper_accounts.list_accounts()
                ]
            )

        @app.post("/api/v1/paper/accounts")
        def create_paper_account(
            request: Request, body: PaperAccountCreateRequest
        ) -> PaperAccountStatusResponse:
            require_write(request)
            try:
                account = paper_accounts.create(
                    account_id=body.account_id,
                    name=body.name,
                    opening_balance_paise=CurrencyPaise(body.opening_balance_paise),
                )
            except PaperAccountError as exc:
                status = 409 if "already exists" in str(exc) else 422
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return account_status(paper_accounts.get_balance(account.account_id))

        @app.get("/api/v1/paper/accounts/{account_id}/book")
        def paper_account_book(request: Request, account_id: str) -> PaperBookResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                balance = paper_accounts.get_balance(account_id)
                positions = paper_trading.positions(account_id)
                orders = paper_trading.recent_orders(account_id, limit=20)
            except ValueError as exc:
                raise HTTPException(
                    status_code=404, detail="Paper account unavailable"
                ) from exc
            return PaperBookResponse(
                account=account_status(balance),
                positions=[
                    PaperPositionStatus(
                        instrument_id=item.instrument_id, quantity=item.quantity
                    )
                    for item in positions
                ],
                orders=[order_status(item) for item in orders],
            )

        @app.post("/api/v1/paper/orders/{order_id}/fill")
        def fill_paper_order(
            request: Request, order_id: str, body: PaperFillRequest
        ) -> PaperFillResponse:
            require_write(request)
            try:
                order, fill = paper_trading.fill(
                    order_id,
                    quantity=Quantity(body.quantity),
                    price_paise=CurrencyPaise(body.price_paise),
                    idempotency_key=body.idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return PaperFillResponse(
                order=order_status(order),
                fill=PaperFillStatus(
                    fill_id=fill.fill_id,
                    order_id=fill.order_id,
                    quantity=fill.quantity,
                    price_paise=str(fill.price_paise),
                    filled_at=fill.filled_at,
                    idempotency_key=fill.idempotency_key,
                    realized_pnl_paise=(
                        None
                        if fill.realized_pnl_paise is None
                        else str(fill.realized_pnl_paise)
                    ),
                    gross_paise=str(fill.gross_paise),
                    charges_paise=str(fill.charges_paise),
                    net_pnl_paise=(
                        None if fill.net_pnl_paise is None else str(fill.net_pnl_paise)
                    ),
                ),
            )

        @app.post("/api/v1/paper/orders/{order_id}/cancel")
        def cancel_paper_order(request: Request, order_id: str) -> PaperOrderStatus:
            require_write(request)
            try:
                order = paper_trading.cancel(order_id)
            except PaperTradingError as exc:
                status = 404 if "not found" in str(exc) else 409
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return order_status(order)

        @app.post("/api/v1/paper/accounts/{account_id}/positions/{instrument_id}/exit")
        def exit_paper_position(
            request: Request,
            account_id: str,
            instrument_id: str,
            body: PaperExitRequest,
        ) -> PaperOrderStatus:
            require_write(request)
            try:
                existing = paper_trading.order_for_client_id(
                    account_id, body.client_order_id
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=404, detail="Paper account unavailable"
                ) from exc
            if existing is not None:
                return order_status(existing)
            try:
                positions = paper_trading.positions(account_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=404, detail="Paper account unavailable"
                ) from exc
            held = next(
                (
                    item.quantity
                    for item in positions
                    if item.instrument_id == instrument_id
                ),
                0,
            )
            if held < body.quantity:
                raise HTTPException(
                    status_code=422,
                    detail="Exit quantity exceeds the held position.",
                )
            try:
                order = paper_trading.place_order(
                    account_id=account_id,
                    instrument_id=instrument_id,
                    client_order_id=body.client_order_id,
                    side=PaperSide.SELL,
                    order_type=PaperOrderType.LIMIT,
                    quantity=Quantity(body.quantity),
                    limit_price_paise=CurrencyPaise(body.price_paise),
                    product=PaperProduct(body.product),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return order_status(order)

        @app.get("/api/v1/paper/accounts/{account_id}/reconciliation")
        def paper_reconciliation(
            request: Request, account_id: str
        ) -> PaperReconciliationStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                result = paper_trading.reconcile(account_id)
            except PaperTradingError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=404, detail="Paper account unavailable"
                ) from exc
            return PaperReconciliationStatus(
                account_id=result.account_id,
                order_count=result.order_count,
                fill_count=result.fill_count,
                ledger_debit_paise=str(result.ledger_debit_paise),
                ledger_credit_paise=str(result.ledger_credit_paise),
                position_count=result.position_count,
                charges_paise=str(result.charges_paise),
            )

        def approval_status(item: PaperApproval) -> PaperApprovalStatus:
            return PaperApprovalStatus(
                approval_id=item.approval_id,
                order_id=item.order_id,
                account_id=item.account_id,
                status=item.status,
                reason=item.reason,
                created_at=item.created_at,
                decided_at=item.decided_at,
                decision_note=item.decision_note,
            )

        def stop_status(item: PaperStop) -> PaperStopStatus:
            return PaperStopStatus(
                stop_id=item.stop_id,
                account_id=item.account_id,
                instrument_id=item.instrument_id,
                product=item.product.value,
                quantity=item.quantity,
                stop_price_paise=str(item.stop_price_paise),
                target_price_paise=(
                    str(item.target_price_paise)
                    if item.target_price_paise is not None
                    else None
                ),
                status=item.status,
                trigger_order_id=item.trigger_order_id,
                trigger_kind=item.trigger_kind,
                created_at=item.created_at,
                triggered_at=item.triggered_at,
            )

        @app.get("/api/v1/paper/accounts/{account_id}/realism")
        def paper_realism(request: Request, account_id: str) -> PaperRealismStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                product_positions = paper_trading.product_positions(account_id)
                lots = paper_trading.tax_lots(account_id)
                approvals = paper_trading.approvals(account_id)
                stops = PaperStopStore(database_path).list(account_id)
                reconciliation = paper_trading.reconcile(account_id)
            except (PaperTradingError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return PaperRealismStatus(
                account_id=account_id,
                product_positions=[
                    PaperProductPositionStatus(
                        instrument_id=item.instrument_id,
                        product=item.product.value,
                        quantity=item.quantity,
                    )
                    for item in product_positions
                ],
                tax_lots=[
                    PaperTaxLotStatus(
                        lot_id=item.lot_id,
                        instrument_id=item.instrument_id,
                        product=item.product.value,
                        quantity=item.quantity,
                        remaining_quantity=item.remaining_quantity,
                        settles_on=item.settles_on.isoformat(),
                        cost_paise=str(item.cost_paise),
                        charges_paise=str(item.charges_paise),
                    )
                    for item in lots
                ],
                approvals=[approval_status(item) for item in approvals],
                stops=[stop_status(item) for item in stops],
                charges_paise=str(reconciliation.charges_paise),
            )

        @app.get("/api/v1/paper/accounts/{account_id}/approvals")
        def paper_approval_list(
            request: Request, account_id: str
        ) -> list[PaperApprovalStatus]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                items = paper_trading.approvals(account_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return [approval_status(item) for item in items]

        @app.post("/api/v1/paper/orders/{order_id}/approval")
        def decide_paper_approval(
            request: Request, order_id: str, body: PaperApprovalDecision
        ) -> PaperOrderStatus:
            require_write(request)
            try:
                order = paper_trading.decide_approval(
                    order_id, approved=body.approved, note=body.note
                )
            except PaperTradingError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return order_status(order)

        @app.get("/api/v1/paper/accounts/{account_id}/stops")
        def paper_stop_list(request: Request, account_id: str) -> list[PaperStopStatus]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                items = PaperStopStore(database_path).list(account_id)
            except PaperStopError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return [stop_status(item) for item in items]

        @app.post("/api/v1/paper/accounts/{account_id}/stops")
        def create_paper_stop(
            request: Request, account_id: str, body: PaperStopCreateRequest
        ) -> PaperStopStatus:
            require_write(request)
            try:
                item = PaperStopStore(database_path).create(
                    account_id=account_id,
                    instrument_id=body.instrument_id,
                    quantity=Quantity(body.quantity),
                    stop_price_paise=CurrencyPaise(body.stop_price_paise),
                    target_price_paise=(
                        CurrencyPaise(body.target_price_paise)
                        if body.target_price_paise is not None
                        else None
                    ),
                    product=PaperProduct(body.product),
                )
            except (PaperStopError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return stop_status(item)

        @app.post("/api/v1/paper/accounts/{account_id}/stops/{stop_id}/cancel")
        def cancel_paper_stop(
            request: Request, account_id: str, stop_id: str
        ) -> PaperStopStatus:
            require_write(request)
            del account_id
            try:
                item = PaperStopStore(database_path).cancel(stop_id)
            except PaperStopError as exc:
                status = 404 if "not found" in str(exc) else 409
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return stop_status(item)

        @app.post("/api/v1/paper/accounts/{account_id}/stops/evaluate")
        def evaluate_paper_stops(
            request: Request, account_id: str
        ) -> PaperStopEvaluationStatus:
            require_write(request)
            del account_id
            try:
                router = build_analysis_router(database_path, settings.load())
                result = PaperStopStore(database_path).evaluate(
                    router, as_of=datetime.now(UTC)
                )
            except Exception as exc:  # noqa: BLE001 - surface a bounded error
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return PaperStopEvaluationStatus(
                examined=result.examined,
                triggered=result.triggered,
                notes=list(result.notes),
            )

        @app.post("/api/v1/paper/accounts/{account_id}/square-off")
        def square_off_paper_mis(
            request: Request, account_id: str
        ) -> PaperSquareOffStatus:
            require_write(request)
            try:
                router = build_analysis_router(database_path, settings.load())
                result = PaperSquareOff(database_path).square_off(
                    router, as_of=datetime.now(UTC), account_id=account_id
                )
            except Exception as exc:  # noqa: BLE001 - surface a bounded error
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return PaperSquareOffStatus(
                examined=result.examined,
                squared_off=result.squared_off,
                notes=list(result.notes),
            )

        @app.post("/api/v1/execution/preview")
        def execution_preview(
            request: Request, body: ExecutionRequest
        ) -> ExecutionResponse:
            require_write(request)
            plan, adapter, snapshot = prepare_execution(body)
            try:
                outcome = adapter.preview(plan, context=snapshot.context)
            except ExecutionDisabled as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PaperExecutorError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return execution_response(outcome, snapshot)

        @app.post("/api/v1/execution/paper")
        def execution_paper(
            request: Request, body: ExecutionPlaceRequest
        ) -> ExecutionResponse:
            require_write(request)
            plan, adapter, snapshot = prepare_execution(body)
            try:
                outcome = adapter.place(
                    plan,
                    context=snapshot.context,
                    client_order_id=body.client_order_id,
                    product=PaperProduct(body.product),
                    require_approval=body.require_approval,
                )
            except ExecutionDisabled as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PaperExecutorError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return execution_response(outcome, snapshot)

        @app.get("/api/v1/schedules/memory-resolve")
        def memory_schedule(request: Request) -> MemoryScheduleStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            latest = schedules.load()
            return _memory_schedule_status(latest.schedules, version=latest.version)

        @app.put("/api/v1/schedules/memory-resolve")
        def update_memory_schedule(
            request: Request, body: MemoryScheduleRequest
        ) -> MemoryScheduleStatus:
            require_write(request)
            latest = schedules.load()
            existing = next(
                (
                    item
                    for item in latest.schedules
                    if item.job_id == MEMORY_RESOLVE_JOB
                ),
                None,
            )
            if existing is None:
                if not body.enabled:
                    return _memory_schedule_status(
                        latest.schedules, version=latest.version
                    )
                schedule = _default_memory_schedule(enabled=True)
                snapshot = schedules.save(
                    (*latest.schedules, schedule),
                    expected_version=latest.version,
                )
                return _memory_schedule_status(
                    snapshot.schedules, version=snapshot.version
                )
            updated = replace(existing, enabled=body.enabled)
            snapshot = schedules.save(
                tuple(
                    updated if item.schedule_id == existing.schedule_id else item
                    for item in latest.schedules
                ),
                expected_version=latest.version,
            )
            return _memory_schedule_status(snapshot.schedules, version=snapshot.version)

        @app.get("/api/v1/schedules/catalogue")
        def schedule_catalogue(request: Request) -> JobCatalogueResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return JobCatalogueResponse(
                jobs=[
                    JobDefinitionStatus(
                        job_id=item.job_id,
                        label=item.label,
                        description=item.description,
                        resource_class=item.resource_class,
                        timezone=item.timezone,
                        local_time=item.local_time,
                        window_start=item.window_start,
                        window_end=item.window_end,
                        enabled_by_default=item.enabled_by_default,
                        max_attempts=item.max_attempts,
                        wake_policy=item.wake_policy,
                    )
                    for item in JOB_CATALOGUE
                ]
            )

        def schedule_config(
            schedule: DailySchedule, *, version: int
        ) -> ScheduleConfigStatus:
            occurrences: tuple[datetime, ...] = ()
            try:
                if isinstance(schedule, IntervalSchedule):
                    occurrences = next_interval_occurrences(
                        schedule, after=datetime.now(UTC), count=5
                    )
                else:
                    occurrences = next_daily_occurrences(
                        schedule, after=datetime.now(UTC), count=5
                    )
            except ScheduleError:
                occurrences = ()
            return ScheduleConfigStatus(
                schedule_id=schedule.schedule_id,
                job_id=schedule.job_id,
                enabled=schedule.enabled,
                timezone=schedule.timezone,
                local_time=schedule.local_time.isoformat(timespec="minutes"),
                window_start=schedule.window_start.isoformat(timespec="minutes"),
                window_end=schedule.window_end.isoformat(timespec="minutes"),
                wake_policy=schedule.wake_policy.value,
                wake_within_seconds=schedule.wake_within_seconds,
                max_attempts=schedule.max_attempts,
                interval_minutes=(
                    schedule.interval_minutes
                    if isinstance(schedule, IntervalSchedule)
                    else None
                ),
                weekdays=list(schedule.weekdays),
                dependencies=list(schedule.dependencies),
                version=version,
                next_occurrences=list(occurrences),
            )

        def job_run_view(run: object) -> JobRunView:
            return JobRunView(
                run_id=run.run_id,  # type: ignore[attr-defined]
                job_id=run.job_id,  # type: ignore[attr-defined]
                occurrence_key=run.occurrence_key,  # type: ignore[attr-defined]
                scheduled_at=run.scheduled_at,  # type: ignore[attr-defined]
                status=run.status.value,  # type: ignore[attr-defined]
                attempts=run.attempts,  # type: ignore[attr-defined]
                max_attempts=run.max_attempts,  # type: ignore[attr-defined]
                failure_code=run.failure_code,  # type: ignore[attr-defined]
            )

        @app.get("/api/v1/schedules")
        def schedule_list(request: Request) -> ScheduleConfigList:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            snapshot = schedules.load()
            return ScheduleConfigList(
                version=snapshot.version,
                schedules=[
                    schedule_config(item, version=snapshot.version)
                    for item in snapshot.schedules
                ],
            )

        @app.put("/api/v1/schedules/{schedule_id}")
        def schedule_update(
            request: Request, schedule_id: str, body: ScheduleUpdateRequest
        ) -> ScheduleConfigStatus:
            require_write(request)
            if body.job_id not in AVAILABLE_JOBS:
                raise HTTPException(status_code=422, detail="Job is not implemented.")
            try:
                local_time = time.fromisoformat(body.local_time)
                window_start = time.fromisoformat(body.window_start)
                window_end = time.fromisoformat(body.window_end)
                weekdays = tuple(body.weekdays)
                if not weekdays or tuple(sorted(set(weekdays))) != weekdays:
                    raise ScheduleError("Weekdays must be distinct ordered days.")
                wake_policy = WakePolicy(body.wake_policy)
                wake_within = (
                    body.wake_within_seconds
                    if body.wake_policy == "RUN_ON_WAKE_WITHIN"
                    else None
                )
                if body.interval_minutes is None:
                    schedule = DailySchedule(
                        schedule_id=schedule_id,
                        job_id=body.job_id,
                        enabled=body.enabled,
                        timezone=body.timezone,
                        local_time=local_time,
                        window_start=window_start,
                        window_end=window_end,
                        wake_policy=wake_policy,
                        wake_within_seconds=wake_within,
                        max_attempts=body.max_attempts,
                        dependencies=tuple(body.dependencies),
                        weekdays=weekdays,
                    )
                else:
                    schedule = IntervalSchedule(
                        schedule_id=schedule_id,
                        job_id=body.job_id,
                        enabled=body.enabled,
                        timezone=body.timezone,
                        local_time=local_time,
                        window_start=window_start,
                        window_end=window_end,
                        wake_policy=wake_policy,
                        wake_within_seconds=wake_within,
                        max_attempts=body.max_attempts,
                        dependencies=tuple(body.dependencies),
                        weekdays=weekdays,
                        interval_minutes=body.interval_minutes,
                    )
                latest = schedules.load()
                updated = tuple(
                    schedule if item.schedule_id == schedule_id else item
                    for item in latest.schedules
                )
                if not any(
                    item.schedule_id == schedule_id for item in latest.schedules
                ):
                    updated = (*updated, schedule)
                snapshot = schedules.save(updated, expected_version=latest.version)
                saved = next(
                    item
                    for item in snapshot.schedules
                    if item.schedule_id == schedule_id
                )
            except (ScheduleError, ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return schedule_config(saved, version=snapshot.version)

        @app.post("/api/v1/schedules/{schedule_id}/enable")
        def schedule_enable(
            request: Request, schedule_id: str, body: ScheduleEnableRequest
        ) -> ScheduleConfigStatus:
            require_write(request)
            latest = schedules.load()
            existing = next(
                (item for item in latest.schedules if item.schedule_id == schedule_id),
                None,
            )
            if existing is None:
                raise HTTPException(status_code=404, detail="Schedule not found.")
            try:
                updated_schedule = replace(existing, enabled=body.enabled)
                snapshot = schedules.save(
                    tuple(
                        updated_schedule if item.schedule_id == schedule_id else item
                        for item in latest.schedules
                    ),
                    expected_version=latest.version,
                )
            except ScheduleError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            saved = next(
                item for item in snapshot.schedules if item.schedule_id == schedule_id
            )
            return schedule_config(saved, version=snapshot.version)

        @app.post("/api/v1/schedules/{schedule_id}/dry-run")
        def schedule_dry_run(
            request: Request, schedule_id: str
        ) -> ScheduleDryRunResponse:
            require_write(request)
            latest = schedules.load()
            existing = next(
                (item for item in latest.schedules if item.schedule_id == schedule_id),
                None,
            )
            if existing is None:
                raise HTTPException(status_code=404, detail="Schedule not found.")
            return ScheduleDryRunResponse(
                valid=True,
                schedule_id=schedule_id,
                job_id=existing.job_id,
            )

        @app.post("/api/v1/schedules/{schedule_id}/run")
        def schedule_run(
            request: Request, schedule_id: str, body: ScheduleRunRequest
        ) -> JobRunView:
            require_write(request)
            latest = schedules.load()
            existing = next(
                (item for item in latest.schedules if item.schedule_id == schedule_id),
                None,
            )
            if existing is None:
                raise HTTPException(status_code=404, detail="Schedule not found.")
            if existing.job_id not in AVAILABLE_JOBS:
                raise HTTPException(status_code=422, detail="Job is not implemented.")
            occurrence_key = f"run-now:{schedule_id}:{body.idempotency_key}"
            try:
                current = job_store.find(existing.job_id, occurrence_key)
                if current is not None:
                    return job_run_view(current)
                run = job_store.enqueue(
                    existing.job_id,
                    occurrence_key,
                    scheduled_at=datetime.now(UTC),
                    max_attempts=existing.max_attempts,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return job_run_view(run)

        @app.get("/api/v1/schedules/{schedule_id}/history")
        def schedule_history(request: Request, schedule_id: str) -> list[JobRunView]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            latest = schedules.load()
            existing = next(
                (item for item in latest.schedules if item.schedule_id == schedule_id),
                None,
            )
            if existing is None:
                raise HTTPException(status_code=404, detail="Schedule not found.")
            return [
                job_run_view(run)
                for run in job_store.recent(limit=100)
                if run.job_id == existing.job_id
            ]

        @app.get("/api/v1/schedules/status")
        def schedule_status(request: Request) -> ScheduleStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            overview = schedules.overview()
            effective = overview.effective
            active = effective.snapshot.schedules if effective is not None else ()
            recent_runs = job_store.recent(limit=20)
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
                job_run_count=len(recent_runs),
                job_runs=[
                    JobRunView(
                        run_id=run.run_id,
                        job_id=run.job_id,
                        occurrence_key=run.occurrence_key,
                        scheduled_at=run.scheduled_at,
                        status=run.status.value,
                        attempts=run.attempts,
                        max_attempts=run.max_attempts,
                        failure_code=run.failure_code,
                    )
                    for run in recent_runs
                ],
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

        @app.get("/api/v1/logs")
        def get_logs(request: Request) -> LoggingStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return _logging_status(get_logbook())

        @app.put("/api/v1/logs")
        def update_logs(request: Request, body: LoggingUpdate) -> LoggingStatus:
            require_write(request)
            try:
                config = settings.save_logging(
                    LoggingConfig(
                        enabled=body.enabled,
                        retention_days=body.retention_days,
                        flush_minutes=body.flush_minutes,
                    )
                )
            except (SettingsError, LoggingConfigError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _logging_status(
                configure_logbook(database_path.parent, config.logging)
            )

        @app.post("/api/v1/logs/flush")
        def flush_logs(request: Request) -> LogFlushResult:
            require_write(request)
            book = get_logbook()
            result = book.flush()
            return LogFlushResult(
                written=result.written,
                files=list(result.files),
                pruned=list(result.pruned),
                status=_logging_status(book),
            )

        @app.get("/api/v1/settings/risk-limits")
        def get_risk_limits(request: Request) -> RiskLimitsStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return _risk_limits_status(settings.load().risk_limits)

        @app.put("/api/v1/settings/risk-limits")
        def update_risk_limits(
            request: Request, body: RiskLimitsUpdate
        ) -> RiskLimitsStatus:
            require_write(request)
            try:
                config = settings.save_risk_limits(
                    RiskLimits(
                        max_symbol_exposure_paise=body.max_symbol_exposure_paise,
                        max_open_positions=body.max_open_positions,
                        max_daily_loss_paise=body.max_daily_loss_paise,
                        max_weekly_loss_paise=body.max_weekly_loss_paise,
                        max_monthly_loss_paise=body.max_monthly_loss_paise,
                        max_order_rate_per_minute=body.max_order_rate_per_minute,
                    )
                )
            except (SettingsError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _risk_limits_status(config.risk_limits)

        @app.delete("/api/v1/settings/risk-limits")
        def reset_risk_limits(request: Request) -> RiskLimitsStatus:
            require_write(request)
            return _risk_limits_status(settings.clear_risk_limits().risk_limits)

        @app.get("/api/v1/settings/tunnel")
        def get_tunnel(request: Request) -> TunnelStatusView:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            if tunnel_manager is None:
                raise HTTPException(status_code=503, detail="Tunnel unavailable")
            return _tunnel_status(tunnel_manager.snapshot())

        @app.post("/api/v1/settings/tunnel/start", status_code=202)
        async def start_tunnel(request: Request) -> TunnelStatusView:
            require_write(request)
            if tunnel_manager is None:
                raise HTTPException(status_code=503, detail="Tunnel unavailable")
            return _tunnel_status(await tunnel_manager.request_start())

        @app.post("/api/v1/settings/tunnel/stop")
        async def stop_tunnel(request: Request) -> TunnelStatusView:
            require_write(request)
            if tunnel_manager is None:
                raise HTTPException(status_code=503, detail="Tunnel unavailable")
            return _tunnel_status(await tunnel_manager.stop())

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
                local = {model.model: model for model in effective_local_models()}
                endpoints: set[str] = set()
                for tier in tiers.values():
                    if tier.provider is Provider.CUSTOM and tier.model in local:
                        endpoint = local[tier.model].resolved_endpoint
                        if endpoint:
                            endpoints.add(endpoint)
                for route in routing.values():
                    if (
                        route.provider is Provider.CUSTOM
                        and route.model is not None
                        and route.model in local
                    ):
                        endpoint = local[route.model].resolved_endpoint
                        if endpoint:
                            endpoints.add(endpoint)
                if len(endpoints) > 1:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "Only one local endpoint can be routed at a time; "
                            "select models that share an endpoint."
                        ),
                    )
                if endpoints:
                    settings.save_custom_endpoint(next(iter(endpoints)))
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

        @app.get("/api/v1/prompts")
        def list_agent_prompts(request: Request) -> PromptList:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            store = PromptStore(database_path)
            return PromptList(
                prompts=[
                    PromptStatus.model_validate(prompt.to_dict())
                    for prompt in store.list()
                ]
            )

        @app.put("/api/v1/prompts/{agent_id}")
        def update_agent_prompt(
            request: Request, agent_id: str, body: PromptUpdate
        ) -> PromptStatus:
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
                prompt = PromptStore(database_path).save(
                    agent_id, system=body.system, template=body.template
                )
            except PromptNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except PromptError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return PromptStatus.model_validate(prompt.to_dict())

        @app.post("/api/v1/prompts/{agent_id}/reset")
        def reset_agent_prompt(request: Request, agent_id: str) -> PromptStatus:
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
                prompt = PromptStore(database_path).reset(agent_id)
            except PromptNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except PromptError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return PromptStatus.model_validate(prompt.to_dict())

        @app.get("/api/v1/models")
        def list_local_models(request: Request) -> LocalModelList:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return model_list_response()

        @app.post("/api/v1/models", status_code=201)
        def add_local_model(
            request: Request, body: LocalModelCreate
        ) -> LocalModelStatus:
            require_write(request)
            try:
                model = create_local_model(**body.model_dump())
            except LocalModelError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            existing = effective_local_models()
            if any(item.id == model.id for item in existing):
                raise HTTPException(
                    status_code=409,
                    detail=f"Model {model.model!r} is already registered.",
                )
            try:
                settings.save_local_models([*existing, model])
            except (SettingsError, LocalModelError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return local_model_status(model, installed_ollama_models())

        @app.delete("/api/v1/models")
        def remove_local_model(request: Request, model_id: str) -> LocalModelList:
            require_write(request)
            existing = effective_local_models()
            kept = tuple(item for item in existing if item.id != model_id)
            if len(kept) == len(existing):
                raise HTTPException(status_code=404, detail="Model is not registered.")
            try:
                settings.save_local_models(kept)
            except (SettingsError, LocalModelError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return model_list_response()

        @app.post("/api/v1/models/pull")
        def pull_local_model(
            request: Request, body: LocalModelAction
        ) -> LocalModelPullStatus:
            require_write(request)
            model = next(
                (item for item in effective_local_models() if item.id == body.model_id),
                None,
            )
            if model is None:
                raise HTTPException(status_code=404, detail="Model is not registered.")
            try:
                state = pull_runner.start(model)
            except ModelPullError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return LocalModelPullStatus(status=state.status, output=state.output)

        @app.post("/api/v1/models/pull/cancel")
        async def cancel_local_model(
            request: Request, body: LocalModelAction
        ) -> LocalModelPullStatus:
            require_write(request)
            if not await pull_runner.cancel(body.model_id):
                raise HTTPException(
                    status_code=409, detail="No pull is running for that model."
                )
            state = pull_runner.state(body.model_id) or PullState("cancelled")
            return LocalModelPullStatus(status=state.status, output=state.output)

        @app.get("/api/v1/evals/config")
        def eval_config(request: Request) -> EvalConfigStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            paths = resolve_paths()
            try:
                categories = read_categories(paths.dataset)
            except (EvalConfigError, OSError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            active = evals.active_run()
            return EvalConfigStatus(
                providers=[
                    EvalProviderStatus(
                        id=model.id, label=model.label, model=model.model
                    )
                    for model in effective_local_models()
                ],
                categories=categories,
                tools=tool_availability(),
                config_path=str(paths.config),
                dataset_path=str(paths.dataset),
                active_run_id=None if active is None else active.id,
                custom_endpoint=settings.load().custom_endpoint,
            )

        @app.get("/api/v1/evals/runs")
        def list_eval_runs(request: Request) -> EvalRunList:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            runs = [
                _eval_run_status(run, evals.get_scores(run.id))
                for run in evals.list_runs()
            ]
            return EvalRunList(runs=runs)

        @app.post("/api/v1/evals/runs", status_code=201)
        async def create_eval_run(
            request: Request, body: EvalRunCreate
        ) -> EvalRunStatus:
            require_write(request)
            paths = resolve_paths()
            known = {model.id: model for model in effective_local_models()}
            if not known:
                raise HTTPException(
                    status_code=422, detail="No local models are registered."
                )
            try:
                categories = set(read_categories(paths.dataset))
                base_text = paths.config.read_text(encoding="utf-8")
            except (EvalConfigError, OSError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            selected = body.providers or list(known)
            unknown = [item for item in selected if item not in known]
            if unknown:
                raise HTTPException(
                    status_code=422, detail=f"Unknown provider(s): {', '.join(unknown)}"
                )
            bad_categories = [c for c in body.categories if c not in categories]
            if bad_categories:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown category(ies): {', '.join(bad_categories)}",
                )
            labels = tuple(known[item].label for item in selected)
            try:
                run = evals.create_run(
                    models=labels,
                    categories=tuple(body.categories),
                    config_path=str(paths.config),
                )
            except EvalRunActive as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            try:
                generated = write_run_config(
                    base_text,
                    [known[item] for item in selected],
                    paths.dataset,
                    evals.artifact_dir(run.id) / "promptfooconfig.yaml",
                )
                run = evals.set_config_path(run.id, str(generated))
            except (EvalConfigError, OSError) as exc:
                evals.mark_failed(run.id, f"could not write the run config: {exc}")
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            eval_runner.start(
                EvalLaunch(
                    run=run,
                    providers=tuple(selected),
                    no_cache=body.no_cache,
                )
            )
            return _eval_run_status(run, [])

        @app.get("/api/v1/evals/runs/{run_id}")
        def get_eval_run(request: Request, run_id: str) -> EvalRunStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                run = evals.get_run(run_id)
            except EvalRunNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return _eval_run_status(run, evals.get_scores(run_id))

        @app.get("/api/v1/evals/runs/{run_id}/cases")
        def get_eval_cases(
            request: Request,
            run_id: str,
            model: str | None = None,
            category: str | None = None,
            status: Literal["all", "failed", "passed"] = "all",
        ) -> EvalCaseList:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                cases = evals.read_cases(
                    run_id,
                    scorer_path=resolve_paths().scorer,
                    model=model,
                    category=category,
                    status=status,
                )
            except EvalRunNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (ScoringError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return EvalCaseList(
                run_id=run_id,
                model=model,
                category=category,
                status=status,
                total=len(cases),
                cases=[
                    EvalCaseStatus(
                        description=case.description,
                        model=case.model,
                        category=case.category,
                        passed=case.passed,
                        input=case.input,
                        raw_output=case.raw_output,
                        confidence=case.confidence,
                        latency_ms=case.latency_ms,
                        failure_reasons=list(case.failure_reasons),
                    )
                    for case in cases
                ],
            )

        @app.post("/api/v1/evals/runs/{run_id}/cancel")
        async def cancel_eval_run(request: Request, run_id: str) -> EvalRunStatus:
            require_write(request)
            try:
                run = evals.get_run(run_id)
            except EvalRunNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if not run.active:
                raise HTTPException(
                    status_code=409, detail=f"Eval run {run_id} is {run.status}."
                )
            cancelled = await eval_runner.cancel(run_id)
            if not cancelled:
                evals.mark_cancelled(run_id)
            return _eval_run_status(evals.get_run(run_id), evals.get_scores(run_id))

        @app.post("/api/v1/evals/promote")
        def promote_eval_model(
            request: Request, body: EvalPromoteRequest
        ) -> EvalPromoteDiff:
            require_write(request)
            paths = resolve_paths()
            models = {model.model: model for model in effective_local_models()}
            if body.model not in models:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown model {body.model!r}; not in the registry.",
                )
            selected = models[body.model]
            before = _app_config_status(settings.load())
            if body.agent_id not in before.agent_routing:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown agent role {body.agent_id!r}.",
                )
            try:
                endpoint = body.endpoint or selected.resolved_endpoint
                if endpoint:
                    settings.save_custom_endpoint(endpoint)
                config = settings.save_agent_model(
                    agent_id=body.agent_id,
                    provider=Provider.CUSTOM,
                    model=body.model,
                )
            except (SettingsError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            yaml_before = _agent_settings_yaml(
                replace(
                    settings.load(),
                    agent_routing=tuple(before.agent_routing.items()),
                )
            )
            yaml_after = _agent_settings_yaml(config)
            mirror = paths.agent_settings_mirror
            mirror.parent.mkdir(parents=True, exist_ok=True)
            tmp = mirror.with_suffix(".yaml.tmp")
            tmp.write_text(yaml_after, encoding="utf-8")
            tmp.replace(mirror)

            old_route = before.agent_routing[body.agent_id]
            new_route = config.route_for(body.agent_id)
            return EvalPromoteDiff(
                agent_id=body.agent_id,
                before={
                    "tier": old_route.tier.value,
                    "provider": (
                        None if old_route.provider is None else old_route.provider.value
                    ),
                    "model": old_route.model,
                },
                after={
                    "tier": new_route.tier.value,
                    "provider": (
                        None if new_route.provider is None else new_route.provider.value
                    ),
                    "model": new_route.model,
                },
                yaml_before=yaml_before,
                yaml_after=yaml_after,
            )

        @app.get("/api/v1/evals/runs/{run_id}/scorecard.csv")
        def download_eval_scorecard(request: Request, run_id: str) -> Response:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            scorecard = evals.artifact_dir(run_id) / "scorecard.csv"
            if not scorecard.is_file():
                raise HTTPException(status_code=404, detail="Scorecard is not ready.")
            return Response(
                content=scorecard.read_bytes(),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="scorecard-{run_id}.csv"'
                },
            )

        @app.post("/api/v1/decision/preview")
        def decision_preview(
            request: Request, body: DecisionPreviewRequest
        ) -> DecisionPreviewResponse:
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
                    detail="Indices can be used as context but not decided on.",
                )
            try:
                report, is_realtime, vendor_daily, vendor_quotes, notes = (
                    _technical_report_for(database_path, config, symbol, body.timeframe)
                )
            except PipelineError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            freshness = "realtime_kite" if is_realtime else "delayed_yfinance"
            state = build_decision_state(report, data_freshness=freshness)
            model_router = ModelRouter(settings, prompts=PromptStore(database_path))
            try:
                outcome = model_router.decide(
                    "watchlist_screener", state=state, battery=SCREENER
                )
            except DecisionUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            screener = decision_policy_for(config).compose_screener(outcome.judgments)
            use = model_router.routes_used[-1] if model_router.routes_used else None
            route = DecisionRouteStatus(
                tier=outcome.tier if use is None else use.tier,
                provider=outcome.provider if use is None else use.provider,
                model=outcome.model if use is None else use.model,
                decision_source=outcome.source if use is None else use.decision_source,
                fallback_used=(
                    outcome.fallback_used if use is None else use.fallback_used
                ),
                fallback_from=(
                    outcome.fallback_from if use is None else use.fallback_from
                ),
                latency_ms=None if use is None else use.latency_ms,
                request_id=None if use is None else use.request_id,
                model_mismatch=(
                    outcome.judgments.model_mismatch
                    if use is None
                    else use.model_mismatch
                ),
                state_hash=outcome.state_hash if use is None else use.state_hash,
            )
            return DecisionPreviewResponse(
                symbol=state.symbol,
                timeframe=state.timeframe,
                as_of=state.as_of,
                decision_preview=config.decision_preview,
                data_provenance=AnalysisProvenance(
                    vendor_daily=vendor_daily,
                    vendor_quotes=vendor_quotes,
                    is_realtime=is_realtime,
                    data_freshness=freshness,
                ),
                state=state.to_state_dict(),
                judgments=[
                    DecisionJudgmentStatus(
                        name=name,
                        kind=answer.kind.value,
                        choice=answer.choice,
                        score=answer.score,
                        noul=answer.noul,
                        confidence=answer.confidence,
                    )
                    for name, answer in outcome.judgments.answers
                ],
                screener=DecisionScreenerStatus(
                    verdict=screener.verdict.value,
                    reason=screener.reason,
                    quality=screener.quality,
                    worth=screener.worth,
                    confidence=screener.confidence,
                ),
                route=route,
                notes=list(notes),
            )

        def strategy_status(record: StrategyRecord) -> StrategyRecordStatus:
            return StrategyRecordStatus(
                strategy_id=record.definition.strategy_id,
                version=record.definition.version,
                name=record.definition.name,
                kind=record.definition.kind.value,
                lifecycle=record.lifecycle.value,
                definition=record.definition.to_dict(),
                content_hash=record.content_hash,
                is_seed=record.is_seed,
                created_at=record.created_at,
            )

        def backtest_status(run: BacktestRun) -> BacktestStatus:
            return BacktestStatus(
                run_id=run.run_id,
                strategy_id=run.strategy_id,
                version=run.version,
                engine=run.engine,
                start=run.start,
                end=run.end,
                initial_capital_paise=str(run.initial_capital_paise),
                metrics=run.metrics,
                trades=run.trades,
                content_hash=run.content_hash,
                created_at=run.created_at,
                review_decision=run.review_decision,
                review_note=run.review_note,
            )

        @app.get("/api/v1/strategies")
        def strategy_list(request: Request) -> StrategyListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            store = StrategyStore(database_path)
            store.ensure_seeds()
            return StrategyListResponse(
                strategies=[strategy_status(item) for item in store.list()]
            )

        @app.post("/api/v1/strategies")
        def strategy_create(
            request: Request, body: StrategyCreateRequest
        ) -> StrategyRecordStatus:
            require_write(request)
            try:
                definition = strategy_from_dict(body.definition)
                record = StrategyStore(database_path).save(
                    definition,
                    lifecycle=StrategyLifecycle(body.lifecycle),
                )
            except (StrategyStoreError, LifecycleStrategyError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return strategy_status(record)

        @app.get("/api/v1/strategies/{strategy_id}/{version}")
        def strategy_detail(
            request: Request, strategy_id: str, version: str
        ) -> StrategyRecordStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                record = StrategyStore(database_path).get(strategy_id, version)
            except StrategyStoreError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return strategy_status(record)

        @app.post("/api/v1/strategies/{strategy_id}/{version}/backtest")
        def strategy_backtest(
            request: Request,
            strategy_id: str,
            version: str,
            body: StrategyBacktestRequest,
        ) -> BacktestStatus:
            require_write(request)
            if body.start.utcoffset() is None or body.end.utcoffset() is None:
                raise HTTPException(
                    status_code=422,
                    detail="Backtest bounds must be timezone-aware.",
                )
            if body.end < body.start:
                raise HTTPException(
                    status_code=422, detail="Backtest end precedes its start."
                )
            try:
                record = StrategyStore(database_path).get(strategy_id, version)
                router = build_analysis_router(database_path, settings.load())
                bars_by_symbol = {}
                for instrument in record.definition.universe:
                    symbol = resolve_symbol(instrument)
                    result = router.bars(
                        symbol,
                        interval=BarInterval.DAY_1,
                        start=body.start - timedelta(days=400),
                        end=body.end,
                        domain=DataDomain.BARS_DAILY,
                    )
                    bars_by_symbol[instrument] = tuple(
                        bar for bar in result.data if bar.event_at <= body.end
                    )
                outcome = run_backtest(
                    record.definition,
                    bars_by_symbol,
                    start=body.start,
                    end=body.end,
                    initial_capital_paise=body.initial_capital_paise,
                    engine=BacktestEngineName(body.engine),
                )
                run = BacktestStore(database_path).record(outcome)
            except (
                StrategyStoreError,
                StrategyError,
                BacktestError,
                ValueError,
            ) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return backtest_status(run)

        @app.get("/api/v1/backtests")
        def backtest_list(
            request: Request, limit: int = 30, strategy_id: str | None = None
        ) -> BacktestListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                runs = BacktestStore(database_path).list(
                    limit=limit, strategy_id=strategy_id
                )
            except StrategyStoreError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return BacktestListResponse(runs=[backtest_status(item) for item in runs])

        @app.get("/api/v1/backtests/{run_id}")
        def backtest_detail(request: Request, run_id: str) -> BacktestStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                run = BacktestStore(database_path).get(run_id)
            except StrategyStoreError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return backtest_status(run)

        @app.post("/api/v1/backtests/{run_id}/review")
        def backtest_review(
            request: Request, run_id: str, body: BacktestReviewRequest
        ) -> BacktestStatus:
            require_write(request)
            try:
                store = BacktestStore(database_path)
                store.review(run_id, decision=body.decision, note=body.note)
                run = store.get(run_id)
            except StrategyStoreError as exc:
                status = 404 if "not found" in str(exc) else 422
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return backtest_status(run)

        def note_status(note: ResearchNote) -> ResearchNoteStatus:
            return ResearchNoteStatus(
                note_id=note.note_id,
                instrument_id=note.instrument_id,
                source_type=note.source_type.value,
                source_id=note.source_id,
                url=note.url,
                title=note.title,
                body=note.body,
                published_at=note.published_at,
                known_at=note.known_at,
                note_hash=note.note_hash,
                created_at=note.created_at,
            )

        def fundamental_status(record: FundamentalRecord) -> FundamentalSnapshotStatus:
            snapshot = record.snapshot
            return FundamentalSnapshotStatus(
                snapshot_id=record.snapshot_id,
                instrument_id=snapshot.instrument_id,
                source=snapshot.source.value,
                period_end=snapshot.period_end,
                known_at=snapshot.known_at,
                revenue_paise=str(snapshot.revenue_paise),
                ebitda_paise=str(snapshot.ebitda_paise),
                net_income_paise=str(snapshot.net_income_paise),
                total_debt_paise=str(snapshot.total_debt_paise),
                cash_paise=str(snapshot.cash_paise),
                shares_outstanding=snapshot.shares_outstanding,
                book_value_paise=str(snapshot.book_value_paise),
                fcf_paise=str(snapshot.fcf_paise),
                snapshot_hash=record.snapshot_hash,
                created_at=record.created_at,
            )

        def claim_status(claim: ResearchClaim) -> ResearchClaimStatus:
            return ResearchClaimStatus(
                claim_id=claim.claim_id,
                instrument_id=claim.instrument_id,
                statement=claim.statement,
                note_id=claim.note_id,
                claim_hash=claim.claim_hash,
                created_at=claim.created_at,
                status=claim.status,
                review_note=claim.review_note,
            )

        def thesis_status(thesis: Thesis) -> ThesisStatus:
            return ThesisStatus(
                thesis_id=thesis.thesis_id,
                instrument_id=thesis.instrument_id,
                horizon=thesis.horizon.value,
                state=thesis.state.value,
                created_at=thesis.created_at,
                thesis_hash=thesis.thesis_hash,
            )

        def thesis_event_status(event: ThesisEventRecord) -> ThesisEventStatus:
            return ThesisEventStatus(
                event_id=event.event_id,
                thesis_id=event.thesis_id,
                kind=event.kind,
                statement=event.statement,
                note_id=event.note_id,
                occurred_at=event.occurred_at,
                event_hash=event.event_hash,
                created_at=event.created_at,
            )

        @app.get("/api/v1/research/notes")
        def research_note_list(
            request: Request,
            instrument_id: str | None = None,
            as_of: datetime | None = None,
            limit: int = 50,
        ) -> ResearchNoteListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                notes = ResearchExpansionStore(database_path).notes(
                    instrument_id=instrument_id, as_of=as_of, limit=limit
                )
            except (ResearchExpansionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return ResearchNoteListResponse(notes=[note_status(item) for item in notes])

        @app.post("/api/v1/research/notes")
        def research_note_create(
            request: Request, body: ResearchNoteCreateRequest
        ) -> ResearchNoteStatus:
            require_write(request)
            try:
                note = ResearchExpansionStore(database_path).create_note(
                    source_type=NoteSource(body.source_type),
                    source_id=body.source_id,
                    title=body.title,
                    body=body.body,
                    published_at=body.published_at,
                    known_at=body.known_at,
                    instrument_id=body.instrument_id,
                    url=body.url,
                )
            except (ResearchExpansionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return note_status(note)

        @app.post("/api/v1/research/notes/from-news/{news_id}")
        def research_note_from_news(
            request: Request, news_id: str, body: NewsNoteRequest
        ) -> ResearchNoteStatus:
            require_write(request)
            try:
                item = NewsStore(database_path).get(news_id)
                note = ResearchExpansionStore(database_path).note_from_news(
                    item, instrument_id=body.instrument_id
                )
            except (NewsError, ResearchExpansionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return note_status(note)

        @app.post("/api/v1/research/fundamentals")
        def research_fundamentals_create(
            request: Request, body: FundamentalSnapshotRequest
        ) -> FundamentalSnapshotStatus:
            require_write(request)
            try:
                snapshot = FundamentalSnapshot(
                    instrument_id=body.instrument_id,
                    source=FundamentalSource(body.source),
                    period_end=body.period_end,
                    known_at=body.known_at,
                    revenue_paise=body.revenue_paise,
                    ebitda_paise=body.ebitda_paise,
                    net_income_paise=body.net_income_paise,
                    total_debt_paise=body.total_debt_paise,
                    cash_paise=body.cash_paise,
                    shares_outstanding=body.shares_outstanding,
                    book_value_paise=body.book_value_paise,
                    fcf_paise=body.fcf_paise,
                )
                record = ResearchExpansionStore(database_path).freeze_fundamentals(
                    snapshot
                )
            except (FundamentalError, ResearchExpansionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return fundamental_status(record)

        @app.get("/api/v1/research/fundamentals/{instrument_id}")
        def research_fundamentals_list(
            request: Request,
            instrument_id: str,
            as_of: datetime | None = None,
            limit: int = 20,
        ) -> FundamentalListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                records = ResearchExpansionStore(database_path).fundamentals(
                    instrument_id, as_of=as_of, limit=limit
                )
            except (ResearchExpansionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return FundamentalListResponse(
                instrument_id=instrument_id,
                snapshots=[fundamental_status(item) for item in records],
            )

        @app.get("/api/v1/research/instruments/{instrument_id}/dossier")
        def research_dossier(
            request: Request,
            instrument_id: str,
            as_of: datetime | None = None,
            price_paise: int | None = None,
            growth_bps: int = 1_000,
            discount_bps: int = 1_200,
            terminal_growth_bps: int = 400,
            years: int = 5,
        ) -> DossierResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                assumptions = ValuationAssumptions(
                    growth_bps=growth_bps,
                    discount_bps=discount_bps,
                    terminal_growth_bps=terminal_growth_bps,
                    years=years,
                )
                payload = ResearchExpansionStore(database_path).dossier(
                    instrument_id,
                    as_of=as_of,
                    price_paise=price_paise,
                    assumptions=assumptions,
                )
            except (FundamentalError, ResearchExpansionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return DossierResponse.model_validate(payload)

        @app.post("/api/v1/research/theses")
        def research_thesis_create(
            request: Request, body: ThesisCreateRequest
        ) -> ThesisStatus:
            require_write(request)
            try:
                thesis = ResearchStore(database_path).create_thesis(
                    thesis_id=body.thesis_id,
                    instrument_id=body.instrument_id,
                    horizon=ThesisHorizon(body.horizon),
                )
            except (ResearchError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return thesis_status(thesis)

        @app.get("/api/v1/research/theses/{thesis_id}")
        def research_thesis_detail(request: Request, thesis_id: str) -> ThesisStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                thesis = ResearchStore(database_path).get_thesis(thesis_id)
            except ResearchError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return thesis_status(thesis)

        @app.post("/api/v1/research/theses/{thesis_id}/events")
        def research_thesis_event(
            request: Request, thesis_id: str, body: ThesisEventCreateRequest
        ) -> ThesisEventStatus:
            require_write(request)
            try:
                event = ResearchExpansionStore(database_path).add_thesis_event(
                    thesis_id=thesis_id,
                    kind=body.kind,
                    statement=body.statement,
                    note_id=body.note_id,
                    occurred_at=body.occurred_at,
                )
            except (ResearchExpansionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return thesis_event_status(event)

        @app.get("/api/v1/research/theses/{thesis_id}/events")
        def research_thesis_events(
            request: Request, thesis_id: str
        ) -> list[ThesisEventStatus]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                events = ResearchExpansionStore(database_path).thesis_events(thesis_id)
            except ResearchExpansionError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return [thesis_event_status(item) for item in events]

        @app.get("/api/v1/research/claims")
        def research_claim_list(
            request: Request,
            instrument_id: str | None = None,
            status: str | None = None,
            limit: int = 50,
        ) -> ResearchClaimListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                claims = ResearchExpansionStore(database_path).claims(
                    instrument_id=instrument_id, status=status, limit=limit
                )
            except ResearchExpansionError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return ResearchClaimListResponse(
                claims=[claim_status(item) for item in claims]
            )

        @app.post("/api/v1/research/claims")
        def research_claim_create(
            request: Request, body: ResearchClaimCreateRequest
        ) -> ResearchClaimStatus:
            require_write(request)
            try:
                claim = ResearchExpansionStore(database_path).create_claim(
                    instrument_id=body.instrument_id,
                    statement=body.statement,
                    note_id=body.note_id,
                )
            except (ResearchExpansionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return claim_status(claim)

        @app.post("/api/v1/research/claims/{claim_id}/review")
        def research_claim_review(
            request: Request, claim_id: str, body: ResearchClaimReviewRequest
        ) -> ResearchClaimStatus:
            require_write(request)
            try:
                claim = ResearchExpansionStore(database_path).review_claim(
                    claim_id, decision=body.decision, note=body.note
                )
            except ResearchExpansionError as exc:
                status = 404 if "not found" in str(exc) else 409
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return claim_status(claim)

        @app.post("/api/v1/strategy/run")
        def run_strategy(
            request: Request, body: StrategyRunRequest
        ) -> StrategyRunResponse:
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
            prompt_store = PromptStore(database_path)
            agent = SuperAgent(
                pipeline_factory=lambda: build_analysis_pipeline(
                    database_path, config, settings
                ),
                router_factory=lambda: ModelRouter(settings, prompts=prompt_store),
                prompts=prompt_store,
                playbooks=PlaybookStore(database_path),
            )
            try:
                run = agent.run(
                    strategy=body.strategy,
                    symbols=body.symbols,
                    timeframe=body.timeframe,
                    equity_paise=body.equity_paise,
                    available_cash_paise=body.available_cash_paise,
                    risk_basis_points=body.risk_basis_points,
                    max_positions=body.max_positions,
                    playbook_id=body.playbook_id,
                )
            except StrategyError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            strategy_run_id = (
                "strategy:"
                + canonical_digest(
                    {
                        "strategy": body.strategy,
                        "symbols": body.symbols,
                        "as_of": run.as_of.isoformat(),
                    }
                )[:16]
            )
            for outcome in run.symbols:
                if outcome.ok and outcome.result is not None:
                    _record_analysis_episode(
                        database_path,
                        strategy_run_id,
                        outcome.result,
                        source="strategy",
                    )
                    _record_working_memory(
                        database_path,
                        strategy_run_id,
                        outcome.result,
                        source="strategy",
                    )
                    _record_opportunity(
                        database_path,
                        strategy_run_id,
                        outcome.result,
                        source="strategy",
                    )
            return StrategyRunResponse.model_validate(
                {**run.to_dict(), "run_id": strategy_run_id}
            )

        @app.post("/api/v1/reports/morning")
        def report_morning(request: Request) -> ReportStatus:
            require_write(request)
            try:
                router = build_analysis_router(database_path, settings.load())
                report = ReportEngine(database_path, router).morning()
            except (ReportError, ReportEngineError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return ReportStatus.model_validate(report.to_dict())

        @app.post("/api/v1/reports/evening")
        def report_evening(request: Request) -> ReportStatus:
            require_write(request)
            try:
                router = build_analysis_router(database_path, settings.load())
                report = ReportEngine(database_path, router).evening()
            except (ReportError, ReportEngineError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return ReportStatus.model_validate(report.to_dict())

        @app.get("/api/v1/reports")
        def report_list(request: Request, limit: int = 30) -> ReportListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                reports = ReportStore(database_path).list(limit=limit)
            except ReportError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return ReportListResponse(
                reports=[
                    ReportStatus.model_validate(item.to_dict()) for item in reports
                ]
            )

        @app.get("/api/v1/reports/{report_id}")
        def report_detail(request: Request, report_id: str) -> ReportStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                report = ReportStore(database_path).get(report_id)
            except ReportError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return ReportStatus.model_validate(report.to_dict())

        def _campaign_service() -> CampaignService:
            return CampaignService(database_path, settings)

        def _parse_instant(raw: str | None) -> datetime | None:
            if raw is None:
                return None
            try:
                value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail="Invalid as_of timestamp."
                ) from exc
            if value.utcoffset() is None:
                raise HTTPException(
                    status_code=422, detail="as_of must include a timezone."
                )
            return value

        @app.get("/api/v1/campaign/latest")
        def campaign_latest(request: Request) -> dict[str, object]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            service = _campaign_service()
            campaign = service.latest()
            master = EquityMasterStore(database_path).latest_as_of()
            return {
                "campaign": None if campaign is None else campaign.to_dict(),
                "config": service.config.to_dict(),
                "master_as_of": None if master is None else master.isoformat(),
            }

        @app.get("/api/v1/campaign/{campaign_id}")
        def campaign_detail(request: Request, campaign_id: str) -> dict[str, object]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                return _campaign_service().get(campaign_id).to_dict()
            except CampaignNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.get("/api/v1/campaign/{campaign_id}/review")
        def campaign_review_route(
            request: Request, campaign_id: str
        ) -> dict[str, object]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                return _campaign_service().review(campaign_id)
            except CampaignNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.post("/api/v1/campaign/research")
        def campaign_research(
            request: Request, body: CampaignResearchRequest
        ) -> dict[str, object]:
            require_write(request)
            try:
                campaign = _campaign_service().research(
                    as_of=_parse_instant(body.as_of),
                    force=body.force,
                    top_n=body.top_n,
                )
            except CampaignError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except (ListingFetchError, EquityMasterError, NewsError) as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return campaign.to_dict()

        @app.post("/api/v1/campaign/{campaign_id}/scan")
        def campaign_scan(
            request: Request, campaign_id: str, body: CampaignScanRequest
        ) -> dict[str, object]:
            require_write(request)
            try:
                _campaign_service().get(campaign_id)
            except CampaignNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return (
                TriggerEngine(database_path, settings)
                .scan(as_of=_parse_instant(body.as_of))
                .to_dict()
            )

        @app.post("/api/v1/campaign/{campaign_id}/cancel")
        def campaign_cancel(
            request: Request, campaign_id: str, body: CampaignScanRequest
        ) -> dict[str, object]:
            require_write(request)
            try:
                campaign = _campaign_service().cancel(
                    campaign_id, as_of=_parse_instant(body.as_of)
                )
            except CampaignNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return campaign.to_dict()

        @app.get("/api/v1/settings/campaign")
        def campaign_settings(request: Request) -> dict[str, object]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return {"config": settings.load().campaign.to_dict()}

        @app.put("/api/v1/settings/campaign")
        def update_campaign_settings(
            request: Request, body: CampaignSettingsRequest
        ) -> dict[str, object]:
            require_write(request)
            try:
                config = CampaignConfig.from_mapping(body.config)
            except CampaignConfigError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            saved = settings.save_campaign(config)
            return {"config": saved.campaign.to_dict()}

        @app.delete("/api/v1/settings/campaign")
        def reset_campaign_settings(request: Request) -> dict[str, object]:
            require_write(request)
            return {"config": settings.clear_campaign().campaign.to_dict()}

        @app.post("/api/v1/equity-master/refresh")
        def equity_master_refresh(request: Request) -> dict[str, object]:
            require_write(request)
            try:
                inserted = _campaign_service().refresh_master()
            except ListingFetchError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except EquityMasterError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            master = EquityMasterStore(database_path).latest_as_of()
            return {
                "inserted": inserted,
                "as_of": None if master is None else master.isoformat(),
            }

        @app.get("/api/v1/equity-master")
        def equity_master_search(
            request: Request, q: str = "", limit: int = 50
        ) -> dict[str, object]:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            if not q.strip():
                return {"rows": []}
            try:
                rows = EquityMasterStore(database_path).search(q, limit=limit)
            except EquityMasterError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {
                "rows": [
                    {
                        "symbol": row.symbol,
                        "company_name": row.company_name,
                        "series": row.series,
                        "isin": row.isin,
                    }
                    for row in rows
                ]
            }

        @app.get("/api/v1/opportunities")
        def opportunity_list(
            request: Request,
            limit: int = 50,
            horizon: str | None = None,
            level: str | None = None,
            lifecycle: str | None = None,
        ) -> OpportunityListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                items = OpportunityStore(database_path).list(
                    limit=limit,
                    horizon=OpportunityHorizon(horizon) if horizon else None,
                    level=ConfidenceLevel(level) if level else None,
                    lifecycle=OpportunityLifecycle(lifecycle) if lifecycle else None,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return OpportunityListResponse(
                opportunities=[
                    OpportunityStatus.model_validate(item.to_dict()) for item in items
                ]
            )

        @app.post("/api/v1/opportunities/{opportunity_id}/transition")
        def opportunity_transition(
            request: Request,
            opportunity_id: str,
            body: OpportunityTransitionRequest,
        ) -> OpportunityStatus:
            require_write(request)
            try:
                item = OpportunityStore(database_path).transition(
                    opportunity_id,
                    OpportunityLifecycle(body.lifecycle),
                    note=body.note,
                )
            except (OpportunityError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return OpportunityStatus.model_validate(item.to_dict())

        @app.get("/api/v1/opportunities/shadow/summary")
        def shadow_summary(request: Request) -> ShadowSummaryStatus:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            summary = OpportunityStore(database_path).filter_verdict()
            return ShadowSummaryStatus(
                verdict=summary.verdict,
                suggested_count=summary.suggested_count,
                unsuggested_count=summary.unsuggested_count,
                suggested_win_rate=summary.suggested_win_rate,
                unsuggested_win_rate=summary.unsuggested_win_rate,
                suggested_interval=(
                    list(summary.suggested_interval)
                    if summary.suggested_interval is not None
                    else None
                ),
                unsuggested_interval=(
                    list(summary.unsuggested_interval)
                    if summary.unsuggested_interval is not None
                    else None
                ),
            )

        @app.post("/api/v1/opportunities/shadow/resolve")
        def shadow_resolve(request: Request) -> dict[str, object]:
            require_write(request)
            store = OpportunityStore(database_path)
            router = build_analysis_router(database_path, settings.load())
            try:
                return store.resolve_shadows(
                    router, as_of=datetime.now(UTC), max_trades=50
                )
            except OpportunityError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @app.get("/api/v1/memory/calibration")
        def memory_calibration(request: Request) -> MemoryCalibrationResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            observed_at = datetime.now(UTC)
            buckets = EpisodicMemoryStore(database_path).calibration(as_of=observed_at)
            return MemoryCalibrationResponse(
                as_of=observed_at.isoformat(),
                buckets=[
                    MemoryCalibrationBucket(
                        decision_source=bucket.decision_source,
                        model=bucket.model,
                        count=bucket.summary.count,
                        mean_brier=bucket.summary.mean_brier,
                        mean_log_loss=bucket.summary.mean_log_loss,
                        expected_calibration_error=(
                            bucket.summary.expected_calibration_error
                        ),
                        outcome_rate=bucket.summary.outcome_rate,
                    )
                    for bucket in buckets
                ],
            )

        @app.post("/api/v1/memory/semantic")
        def semantic_ingest(
            request: Request, body: SemanticIngestRequest
        ) -> SemanticDocumentStatus:
            require_write(request)
            try:
                document = SemanticMemoryStore(database_path).ingest(
                    symbol=body.symbol,
                    source_url=body.source_url,
                    title=body.title,
                    body=body.body,
                    published_at=body.published_at,
                    known_at=body.known_at,
                )
            except SemanticError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return SemanticDocumentStatus(
                doc_id=document.doc_id,
                symbol=document.symbol,
                source_url=document.source_url,
                title=document.title,
                snippet=document.body[:400],
                published_at=document.published_at,
                known_at=document.known_at,
                recorded_at=document.recorded_at,
                content_hash=document.content_hash,
            )

        @app.get("/api/v1/memory/semantic")
        def semantic_search(
            request: Request,
            q: str,
            symbol: str | None = None,
            as_of: datetime | None = None,
            limit: int = 10,
        ) -> SemanticSearchResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            observed_at = datetime.now(UTC) if as_of is None else as_of
            try:
                hits = SemanticMemoryStore(database_path).search(
                    q, as_of=observed_at, symbol=symbol, limit=limit
                )
            except SemanticError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return SemanticSearchResponse(
                query=q,
                as_of=observed_at.isoformat(),
                hits=[
                    SemanticHitStatus(
                        doc_id=hit.document.doc_id,
                        symbol=hit.document.symbol,
                        source_url=hit.document.source_url,
                        title=hit.document.title,
                        snippet=hit.document.body[:400],
                        published_at=hit.document.published_at.isoformat(),
                        known_at=hit.document.known_at.isoformat(),
                        score=hit.score,
                    )
                    for hit in hits
                ],
            )

        @app.post("/api/v1/memory/resolve")
        def resolve_memory(
            request: Request, body: MemoryResolveRequest
        ) -> MemoryResolveResponse:
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
                resolver = OutcomeResolver(
                    EpisodicMemoryStore(database_path),
                    build_analysis_router(database_path, config),
                    horizon_days=body.horizon_days,
                    max_episodes=body.max_episodes,
                )
            except ResolutionError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            report = resolver.resolve_due()
            return MemoryResolveResponse(
                examined=report.examined,
                resolved=report.resolved,
                skipped=report.skipped,
                failed=report.failed,
                notes=list(report.notes),
            )

        @app.get("/api/v1/memory/playbooks")
        def list_playbooks(request: Request) -> PlaybookList:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return PlaybookList(
                playbooks=[
                    PlaybookStatus.model_validate(playbook.to_dict())
                    for playbook in PlaybookStore(database_path).list()
                ]
            )

        @app.put("/api/v1/memory/playbooks/{playbook_id}")
        def update_playbook(
            request: Request, playbook_id: str, body: PlaybookUpdate
        ) -> PlaybookStatus:
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
                playbook = PlaybookStore(database_path).save(
                    playbook_id, content=body.content
                )
            except MissingPlaybook as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ProceduralError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return PlaybookStatus.model_validate(playbook.to_dict())

        @app.post("/api/v1/memory/playbooks/{playbook_id}/reset")
        def reset_playbook(request: Request, playbook_id: str) -> PlaybookStatus:
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
                playbook = PlaybookStore(database_path).reset(playbook_id)
            except MissingPlaybook as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ProceduralError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return PlaybookStatus.model_validate(playbook.to_dict())

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

        def _research_agent() -> ResearchAgent:
            config = settings.load()
            prompts = PromptStore(database_path)
            return ResearchAgent(
                database_path,
                feed_url=config.news_feed_url,
                router=ModelRouter(settings, prompts=prompts),
                prompts=prompts,
                run_store=AgentRunStore(database_path),
            )

        @app.get("/api/v1/agents")
        def agents_catalogue(request: Request) -> AgentCatalogueResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            config = settings.load()
            return AgentCatalogueResponse(
                agents=[
                    AgentCatalogueItem(
                        agent_id=item.agent_id,
                        kind=item.kind,
                        label=item.label,
                        description=item.description,
                        tier=item.tier,
                        prompt_id=item.prompt_id,
                        tools=list(item.tools),
                        read_only=item.read_only,
                        route=AgentCatalogueRoute(
                            tier=item.route_tier,
                            provider=item.provider,
                            model=item.model,
                            configured=item.configured,
                        ),
                    )
                    for item in catalogue_for(config)
                ]
            )

        @app.get("/api/v1/tools/research")
        def research_tools(request: Request) -> ToolCatalog:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            agent = _research_agent()
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
            agent = _research_agent()
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
            agent = _research_agent()
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

        @app.get("/api/v1/agent/runs")
        def agent_run_history(request: Request) -> AgentRunListResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            runs = AgentRunStore(database_path).recent()
            return AgentRunListResponse(
                runs=[
                    AgentRunHistoryItem(
                        run_id=item.run_id,
                        agent_id=item.agent_id,
                        task=item.task,
                        final_text=item.final_text,
                        turns=item.turns,
                        stopped=item.stopped,
                        provider=item.provider,
                        model=item.model,
                        input_tokens=item.input_tokens,
                        output_tokens=item.output_tokens,
                        started_at=item.started_at.isoformat(),
                        finished_at=item.finished_at.isoformat(),
                    )
                    for item in runs
                ]
            )

        @app.get("/api/v1/agent/runs/{run_id}")
        def agent_run_detail(request: Request, run_id: str) -> AgentRunDetailResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            store = AgentRunStore(database_path)
            try:
                run = store.get(run_id)
                steps = store.steps(run_id)
            except AgentRunError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return AgentRunDetailResponse(
                run_id=run.run_id,
                agent_id=run.agent_id,
                task=run.task,
                final_text=run.final_text,
                turns=run.turns,
                stopped=run.stopped,
                provider=run.provider,
                model=run.model,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                started_at=run.started_at.isoformat(),
                finished_at=run.finished_at.isoformat(),
                steps=[
                    AgentStepStatus(
                        turn=step.turn,
                        kind=step.kind,
                        tool=step.tool,
                        arguments=dict(step.arguments),
                        output=step.output,
                    )
                    for step in steps
                ],
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
            agent = _research_agent()
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

            pipeline = build_analysis_pipeline(database_path, config, settings)
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
            _record_analysis_episode(
                database_path, stored.run_id, result, source="analysis"
            )
            _record_working_memory(
                database_path, stored.run_id, result, source="analysis"
            )
            _record_opportunity(database_path, stored.run_id, result, source="analysis")
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
                specialists=payload["specialists"],  # type: ignore[arg-type]
                evidence=payload["evidence"],  # type: ignore[arg-type]
                final_gate=payload["final_gate"],  # type: ignore[arg-type]
                confidence=payload["confidence"],  # type: ignore[arg-type]
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
            pipeline = build_analysis_pipeline(database_path, config, settings)
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
                    _record_analysis_episode(
                        database_path, stored.run_id, result, source="analysis"
                    )
                    _record_working_memory(
                        database_path, stored.run_id, result, source="analysis"
                    )
                    _record_opportunity(
                        database_path, stored.run_id, result, source="analysis"
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

        @app.get("/api/v1/analysis/runs/{run_id}/working-memory")
        def analysis_working_memory(
            request: Request, run_id: str
        ) -> WorkingMemoryResponse:
            if auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                memories = WorkingMemoryStore(database_path).get(run_id)
            except MissingWorkingMemory as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except WorkingMemoryError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return WorkingMemoryResponse(
                run_id=run_id,
                memories=[
                    WorkingMemoryStatus(
                        run_id=item.run_id,
                        symbol=item.symbol,
                        source=item.source,
                        as_of=item.as_of,
                        schema_version=item.schema_version,
                        state=item.state,
                        state_hash=item.state_hash,
                        recorded_at=item.recorded_at,
                    )
                    for item in memories
                ],
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
                secure=cookie_is_secure(request),
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
                secure=cookie_is_secure(request),
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
