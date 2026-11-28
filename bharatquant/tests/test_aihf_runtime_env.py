"""The AI Hedge Fund's paths and its key-free runtime projection.

These tests are hermetic: nothing calls a vendor or the network. The environment
half is gone (ADR-053) — the engine runs in-process, so a key is read from
Settings at the call site and never assembled into an env dict. What is left to
test is that the projection carries no secret, and that a fund name cannot escape
its directory.
"""

from pathlib import Path

import pytest

from bq.aihf.config import (
    AiHfConfigError,
    AiHfPaths,
    installed_version,
    resolve_paths,
    validate_name,
)
from bq.aihf.engine import paths as engine_paths
from bq.aihf.runtime_env import BQ_TO_AIHF_PROVIDER, build_runtime
from bq.infra.database import migrate
from bq.settings import Provider, SettingsStore


@pytest.fixture()
def store(tmp_path: Path) -> SettingsStore:
    database = tmp_path / "bq.db"
    migrate(database, create=True)
    return SettingsStore(database)


@pytest.fixture()
def paths(tmp_path: Path) -> AiHfPaths:
    return resolve_paths(database_path=tmp_path / "bq.db", repo_root=tmp_path)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_names_reject_traversal() -> None:
    for bad in ("../etc", "a/b", ".", "..", "", "-lead", "a" * 65):
        with pytest.raises(AiHfConfigError):
            validate_name(bad)
    assert validate_name("example-fund") == "example-fund"


def test_a_fund_and_mandate_path_are_flat(paths: AiHfPaths) -> None:
    assert paths.fund_dir("alpha") == paths.paper / "alpha"
    assert paths.mandate_path("alpha") == paths.mandates / "alpha.yaml"
    with pytest.raises(AiHfConfigError):
        paths.fund_dir("../escape")


def test_paths_anchor_under_the_database_parent(
    paths: AiHfPaths, tmp_path: Path
) -> None:
    assert paths.root == tmp_path / "aihf"


def test_resolving_paths_points_the_engine_at_the_same_root(paths: AiHfPaths) -> None:
    """The engine writes a fund's ledger and cache through its own `paths`
    module. If the two roots disagreed, a preview and an approval would look at
    different directories and nothing would say so."""
    assert engine_paths.root() == paths.root
    assert engine_paths.paper_dir() == paths.paper


def test_the_engine_never_reads_a_user_home() -> None:
    assert ".hedge-fund" not in str(engine_paths.root())


# ---------------------------------------------------------------------------
# The runtime projection
# ---------------------------------------------------------------------------


def test_the_engine_reports_its_version() -> None:
    # No sidecar dist-info exists any more; the version is this app's own.
    assert installed_version() == "in-process"


def test_no_key_configured_reports_no_llm(store: SettingsStore) -> None:
    runtime = build_runtime(store)
    assert runtime.provider is None
    assert runtime.describe()["has_llm_key"] is False


def test_a_configured_provider_reports_an_llm(store: SettingsStore) -> None:
    store.set_api_key(Provider.GEMINI, "test-key-12345678")
    runtime = build_runtime(store)
    assert runtime.provider == "Google"
    assert runtime.describe()["has_llm_key"] is True


def test_the_projection_carries_no_key_material(store: SettingsStore) -> None:
    """The status card must be able to say "configured" without holding a secret."""
    store.set_api_key(Provider.GEMINI, "test-key-12345678")
    projection = build_runtime(store).describe()
    rendered = repr(projection)
    assert "test-key-12345678" not in rendered
    assert "GOOGLE_API_KEY" in rendered  # the variable *name* may appear


def test_the_bridge_maps_only_real_providers() -> None:
    """Every provider a persona can route to needs a name the engine knows; the
    map must not claim a provider this app cannot serve."""
    for provider in BQ_TO_AIHF_PROVIDER:
        assert isinstance(provider, Provider)
    assert "Anthropic" not in BQ_TO_AIHF_PROVIDER.values()


def test_a_keyless_local_provider_is_configured_without_a_key(
    store: SettingsStore,
) -> None:
    """A local Ollama endpoint has no key and must still count as configured —
    this is the keyless local-model path the app already supports."""
    store.save_custom_endpoint("http://127.0.0.1:11434/v1")
    store.save_config(
        provider="CUSTOM",
        model_name="local-model",
        news_feed_url="https://example.test/feed.xml",
        news_memory_ttl_days=7,
    )
    assert store.load().provider is Provider.CUSTOM
    # The keyless placeholder is supplied by the router, not stored: a local
    # endpoint has no key and must not need one.
    from bq.model_router import ModelRouter

    assert ModelRouter(store)._key(Provider.CUSTOM) == "local"
