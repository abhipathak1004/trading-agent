import re
from dataclasses import dataclass
from enum import StrEnum

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class RoutingError(ValueError):
    pass


class RouteUnavailable(RoutingError):
    pass


class TaskClass(StrEnum):
    SMALL = "SMALL"
    REASONING = "REASONING"
    MULTIMODAL = "MULTIMODAL"


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class ModelTier(StrEnum):
    LOCAL = "LOCAL"
    FREE = "FREE"
    PAID = "PAID"


def _identifier(value: str) -> None:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise RoutingError("Invalid routing identifier.")


def _enum_set(value: object, enum_type: type[StrEnum], label: str) -> None:
    if not isinstance(value, frozenset) or any(
        not isinstance(item, enum_type) for item in value
    ):
        raise RoutingError(f"Invalid {label}.")


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    task_id: str
    version: str
    task_class: TaskClass
    allowed_data: frozenset[DataClassification]
    allowed_tiers: frozenset[ModelTier]

    def __post_init__(self) -> None:
        _identifier(self.task_id)
        _identifier(self.version)
        if not isinstance(self.task_class, TaskClass):
            raise RoutingError("Invalid task class.")
        _enum_set(self.allowed_data, DataClassification, "task data classifications")
        _enum_set(self.allowed_tiers, ModelTier, "task model tiers")
        if not self.allowed_data or not self.allowed_tiers:
            raise RoutingError("Task must permit data classifications and model tiers.")


@dataclass(frozen=True, slots=True)
class LocalEvaluation:
    task_id: str
    task_version: str
    model_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.task_id)
        _identifier(self.task_version)
        if (
            not isinstance(self.model_sha256, str)
            or SHA256.fullmatch(self.model_sha256) is None
        ):
            raise RoutingError("Invalid evaluated model fingerprint.")


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    model_id: str
    tier: ModelTier
    available: bool
    capability_verified: bool
    terms_reviewed: bool
    supported_classes: frozenset[TaskClass]
    approved_data: frozenset[DataClassification]
    model_sha256: str | None
    evaluations: frozenset[LocalEvaluation]

    def __post_init__(self) -> None:
        _identifier(self.model_id)
        if not isinstance(self.tier, ModelTier):
            raise RoutingError("Invalid model tier.")
        if not all(
            isinstance(value, bool)
            for value in (self.available, self.capability_verified, self.terms_reviewed)
        ):
            raise RoutingError("Model availability and review flags must be booleans.")
        _enum_set(self.supported_classes, TaskClass, "model capabilities")
        _enum_set(self.approved_data, DataClassification, "model data approvals")
        if self.model_sha256 is not None and (
            not isinstance(self.model_sha256, str)
            or SHA256.fullmatch(self.model_sha256) is None
        ):
            raise RoutingError("Invalid model fingerprint.")
        if not isinstance(self.evaluations, frozenset) or any(
            not isinstance(evaluation, LocalEvaluation)
            for evaluation in self.evaluations
        ):
            raise RoutingError("Invalid local evaluations.")


def select_model(
    task: TaskPolicy,
    *,
    data_classes: frozenset[DataClassification],
    candidates: tuple[ModelCandidate, ...],
) -> ModelCandidate:
    if not isinstance(task, TaskPolicy):
        raise RoutingError("Task policy is required.")
    _enum_set(data_classes, DataClassification, "input data classifications")
    if not data_classes:
        raise RoutingError("Input data classification is required.")
    if not data_classes <= task.allowed_data:
        raise RoutingError("Input data is not allowed for this task.")
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, ModelCandidate) for candidate in candidates
    ):
        raise RoutingError("Model candidates must be a tuple.")
    if len({candidate.model_id for candidate in candidates}) != len(candidates):
        raise RoutingError("Model candidate identifiers must be unique.")

    reasons: list[str] = []
    for candidate in candidates:
        if not candidate.available:
            reason = "unavailable"
        elif (
            not candidate.capability_verified
            or task.task_class not in candidate.supported_classes
        ):
            reason = "capability_unverified"
        elif candidate.tier not in task.allowed_tiers:
            reason = "tier_disallowed"
        elif (
            candidate.tier is ModelTier.LOCAL and task.task_class is not TaskClass.SMALL
        ):
            reason = "local_task_class_disallowed"
        elif not candidate.terms_reviewed:
            reason = "terms_unreviewed"
        elif candidate.tier is ModelTier.LOCAL and (
            candidate.model_sha256 is None
            or LocalEvaluation(task.task_id, task.version, candidate.model_sha256)
            not in candidate.evaluations
        ):
            reason = "local_evaluation_missing"
        elif (
            DataClassification.PRIVATE in data_classes
            and candidate.tier is ModelTier.FREE
        ):
            reason = "private_data_disallowed_on_free_tier"
        elif not data_classes <= candidate.approved_data:
            reason = "data_handling_unapproved"
        else:
            return candidate
        reasons.append(f"{candidate.model_id}:{reason}")
    detail = ", ".join(reasons) if reasons else "no candidates"
    raise RouteUnavailable(f"No eligible model: {detail}.")
