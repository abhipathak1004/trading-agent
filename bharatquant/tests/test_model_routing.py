from dataclasses import replace

import pytest

from bq.model_routing import (
    DataClassification,
    LocalEvaluation,
    ModelCandidate,
    ModelTier,
    RouteUnavailable,
    RoutingError,
    TaskClass,
    TaskPolicy,
    select_model,
)

PUBLIC = frozenset({DataClassification.PUBLIC})
PRIVATE = frozenset({DataClassification.PRIVATE})
ALL_DATA = PUBLIC | PRIVATE
ALL_TIERS = frozenset(ModelTier)
FINGERPRINT = "a" * 64


def task(task_class: TaskClass) -> TaskPolicy:
    return TaskPolicy("synthetic", "v1", task_class, ALL_DATA, ALL_TIERS)


def model(model_id: str, tier: ModelTier, *, evaluated: bool = True) -> ModelCandidate:
    return ModelCandidate(
        model_id=model_id,
        tier=tier,
        available=True,
        capability_verified=True,
        terms_reviewed=True,
        supported_classes=frozenset(TaskClass),
        approved_data=ALL_DATA,
        model_sha256=FINGERPRINT if tier is ModelTier.LOCAL else None,
        evaluations=(
            frozenset({LocalEvaluation("synthetic", "v1", FINGERPRINT)})
            if evaluated and tier is ModelTier.LOCAL
            else frozenset()
        ),
    )


def test_local_model_requires_matching_small_task_evaluation() -> None:
    local = model("local", ModelTier.LOCAL)
    cloud = model("cloud", ModelTier.PAID)
    assert (
        select_model(
            task(TaskClass.SMALL), data_classes=PUBLIC, candidates=(local, cloud)
        )
        == local
    )
    assert (
        select_model(
            task(TaskClass.REASONING), data_classes=PUBLIC, candidates=(local, cloud)
        )
        == cloud
    )
    assert (
        select_model(
            task(TaskClass.MULTIMODAL), data_classes=PUBLIC, candidates=(local, cloud)
        )
        == cloud
    )
    with pytest.raises(RouteUnavailable, match="local_task_class_disallowed"):
        select_model(
            task(TaskClass.REASONING), data_classes=PUBLIC, candidates=(local,)
        )
    for candidate in (
        replace(local, evaluations=frozenset()),
        replace(local, model_sha256="b" * 64),
    ):
        with pytest.raises(RouteUnavailable, match="local_evaluation_missing"):
            select_model(
                task(TaskClass.SMALL), data_classes=PUBLIC, candidates=(candidate,)
            )
    with pytest.raises(RouteUnavailable, match="local_evaluation_missing"):
        select_model(
            replace(task(TaskClass.SMALL), version="v2"),
            data_classes=PUBLIC,
            candidates=(local,),
        )


def test_private_data_never_routes_to_free_and_requires_explicit_approval() -> None:
    free = model("free", ModelTier.FREE)
    paid = model("paid", ModelTier.PAID)
    assert (
        select_model(
            task(TaskClass.REASONING), data_classes=PRIVATE, candidates=(free, paid)
        )
        == paid
    )
    with pytest.raises(RouteUnavailable, match="private_data_disallowed_on_free_tier"):
        select_model(
            task(TaskClass.REASONING), data_classes=PRIVATE, candidates=(free,)
        )
    with pytest.raises(RouteUnavailable, match="data_handling_unapproved"):
        select_model(
            task(TaskClass.REASONING),
            data_classes=PRIVATE,
            candidates=(replace(paid, approved_data=PUBLIC),),
        )
    with pytest.raises(RoutingError, match="not allowed"):
        select_model(
            replace(task(TaskClass.REASONING), allowed_data=PUBLIC),
            data_classes=PRIVATE,
            candidates=(paid,),
        )
    with pytest.raises(RoutingError, match="classification is required"):
        select_model(
            task(TaskClass.REASONING), data_classes=frozenset(), candidates=(paid,)
        )


def test_capability_terms_tier_and_availability_fail_closed() -> None:
    paid = model("paid", ModelTier.PAID)
    for changed, reason in (
        (replace(paid, available=False), "unavailable"),
        (replace(paid, capability_verified=False), "capability_unverified"),
        (
            replace(paid, supported_classes=frozenset({TaskClass.SMALL})),
            "capability_unverified",
        ),
        (replace(paid, terms_reviewed=False), "terms_unreviewed"),
    ):
        with pytest.raises(RouteUnavailable, match=reason):
            select_model(
                task(TaskClass.MULTIMODAL), data_classes=PUBLIC, candidates=(changed,)
            )
    with pytest.raises(RouteUnavailable, match="tier_disallowed"):
        select_model(
            replace(
                task(TaskClass.REASONING), allowed_tiers=frozenset({ModelTier.FREE})
            ),
            data_classes=PUBLIC,
            candidates=(paid,),
        )
    with pytest.raises(RouteUnavailable, match="no candidates"):
        select_model(task(TaskClass.REASONING), data_classes=PUBLIC, candidates=())
    with pytest.raises(RoutingError, match="unique"):
        select_model(
            task(TaskClass.REASONING), data_classes=PUBLIC, candidates=(paid, paid)
        )
