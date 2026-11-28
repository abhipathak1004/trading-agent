"""The AI Hedge Fund sidecar: paths, the scoped environment, and leaks.

These tests are deliberately hermetic: nothing here calls the real aihf
install or the network. Where a test needs the sidecar it skips.
"""

import json
import os
from pathlib import Path

import pytest

from bq.aihf.config import (
    AiHfConfigError,
    AiHfPaths,
    installed_version,
    resolve_paths,
    validate_name,
)
from bq.aihf.runtime_env import (
    AIHF_ENV_VARS,
    BQ_TO_AIHF_PROVIDER,
    DATA_KEY_ENV,
    MODEL_ENV,
    OWNED_VARS,
    build_runtime,
    get_aihf_runtime_env,
    resolve_model,
)
from bq.infra.database import migrate
from bq.settings import AiHfSecrets, SettingsStore


@pytest.fixture()
def store(tmp_path: Path) -> SettingsStore:
    database = tmp_path / "bq.db"
    migrate(database, create=True)
    return SettingsStore(database)


@pytest.fixture()
def paths(tmp_path: Path) -> AiHfPaths:
    return resolve_paths(database_path=tmp_path / "bq.db", repo_root=tmp_path)


def test_names_reject_traversal() -> None:
    for bad in ("../etc", "a/b", ".", "..", "", "-lead", "a" * 65):
        with pytest.raises(AiHfConfigError):
            validate_name(bad)
    assert validate_name("example-fund") == "example-fund"


def test_fund_and_mandate_paths_are_flat(paths: AiHfPaths) -> None:
    assert paths.fund_dir("alpha") == paths.paper / "alpha"
    assert paths.mandate_path("deep-value") == paths.mandates / "deep-value.yaml"


def test_paths_anchor_under_the_database_parent(tmp_path: Path) -> None:
    resolved = resolve_paths(
        database_path=tmp_path / "nested" / "bq.db", repo_root=tmp_path
    )
    assert resolved.root == tmp_path / "nested" / "aihf"
    assert resolved.user_dir == resolved.root / "home" / ".hedge-fund"
    assert resolved.mandates == resolved.user_dir / "mandates"


def test_installed_version_is_none_without_a_sidecar(tmp_path: Path) -> None:
    missing = AiHfPaths(root=tmp_path, venv=tmp_path / "absent")
    assert missing.available is False
    assert installed_version(missing) is None


def test_env_injects_mapped_keys_and_is_pure(
    store: SettingsStore, paths: AiHfPaths
) -> None:
    store.set_api_key("GEMINI", "gemini-key-1234")
    store.set_api_key("DEEPSEEK", "deepseek-key-1234")
    inherited = {
        "PATH": "/usr/bin",
        "GOOGLE_API_KEY": "stale-google",
        "ANTHROPIC_API_KEY": "leaked-anthropic",
        "HOME": "/real/home",
    }

    env = get_aihf_runtime_env(store, environ=inherited, paths=paths)

    assert env["GOOGLE_API_KEY"] == "gemini-key-1234"
    assert env["DEEPSEEK_API_KEY"] == "deepseek-key-1234"
    # Owned variables never survive from the inherited environment.
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    # Runs are confined to the project's own aihf home.
    assert env["HOME"] == str(paths.home)
    # Must not mutate the caller's environment.
    assert inherited["GOOGLE_API_KEY"] == "stale-google"


def test_env_omits_unconfigured_keys(store: SettingsStore, paths: AiHfPaths) -> None:
    env = get_aihf_runtime_env(store, environ={}, paths=paths)
    assert not any(name in env for name in AIHF_ENV_VARS.values())
    assert DATA_KEY_ENV not in env


def test_env_does_not_touch_os_environ(store: SettingsStore, paths: AiHfPaths) -> None:
    before = dict(os.environ)
    store.set_api_key("OPENAI", "openai-key-1234")
    store.save_aihf_secrets(financial_datasets_api_key="fd-key-1234")
    get_aihf_runtime_env(store, environ=os.environ, paths=paths)
    assert dict(os.environ) == before


def test_data_key_comes_from_settings(store: SettingsStore, paths: AiHfPaths) -> None:
    store.save_aihf_secrets(financial_datasets_api_key="fd-key-1234")
    runtime = build_runtime(
        store, environ={"FINANCIAL_DATASETS_API_KEY": "leak"}, paths=paths
    )
    assert runtime.env[DATA_KEY_ENV] == "fd-key-1234"
    assert DATA_KEY_ENV in runtime.configured_vars
    assert "has_data_key" in runtime.describe()


def test_describe_never_carries_key_material(
    store: SettingsStore, paths: AiHfPaths
) -> None:
    store.set_api_key("GEMINI", "super-secret-gemini-value")
    store.save_aihf_secrets(financial_datasets_api_key="super-secret-datasets")
    runtime = build_runtime(store, environ={}, paths=paths)
    blob = json.dumps(runtime.describe())
    assert "super-secret" not in blob
    assert set(runtime.describe()) == {
        "provider",
        "model",
        "configured_vars",
        "missing",
        "has_llm_key",
        "has_data_key",
    }


def test_active_bridge_follows_the_selected_provider(
    store: SettingsStore, paths: AiHfPaths
) -> None:
    store.set_api_key("GEMINI", "gemini-key-1234")
    runtime = build_runtime(store, environ={}, paths=paths)
    assert runtime.provider == "Google"

    # The selected provider has no aihf counterpart, so nothing is bridged
    # even though the key is stored.
    store.save_config(
        provider="CUSTOM",
        model_name="local-model",
        news_feed_url="https://example.test/feed.xml",
        news_memory_ttl_days=7,
    )
    runtime = build_runtime(store, environ={}, paths=paths)
    assert runtime.provider is None
    assert runtime.describe()["has_llm_key"] is False


def test_owned_vars_cover_every_aihf_key() -> None:
    assert set(OWNED_VARS) == {
        *AIHF_ENV_VARS.values(),
        DATA_KEY_ENV,
        MODEL_ENV,
        "BASE_WALLET_KEY",
    }


def test_provider_bridge_only_maps_real_bq_providers() -> None:
    assert set(BQ_TO_AIHF_PROVIDER) == {"GEMINI", "OPENAI", "DEEPSEEK", "TYPESAFE_JEV"}
    # BharatQuant stores no Anthropic/xAI/Kimi key today; asking for one must
    # fail loudly rather than silently run unauthenticated.
    assert "Anthropic" not in BQ_TO_AIHF_PROVIDER.values()


def test_model_override_needs_a_registry_and_never_guesses(tmp_path: Path) -> None:
    # Resolving an override is a question for aihf's own registry, so it needs
    # the real sidecar. Skip rather than pretend when it is not provisioned.
    real = resolve_paths(database_path=tmp_path / "bq.db")
    if not real.available:
        pytest.skip("aihf sidecar not installed")

    assert resolve_model(None, real) == (None, None)
    assert resolve_model("  ", real) == (None, None)
    # aihf silently sends an unlisted id to Anthropic; we refuse instead.
    with pytest.raises(ValueError, match="does not know the model"):
        resolve_model("some-unlisted-model", real)
    # A registered id resolves to the provider aihf would actually use.
    model, provider = resolve_model("claude-opus-5-5", real)
    assert (model, provider) == ("claude-opus-5-5", "Anthropic")


def test_secrets_round_trip_and_clear(store: SettingsStore) -> None:
    saved = store.save_aihf_secrets(financial_datasets_api_key="  fd-key-1234  ")
    assert saved.financial_datasets_api_key == "fd-key-1234"
    assert store.aihf_secrets().configured is True
    assert store.aihf_secrets().masked == "********1234"

    cleared = store.save_aihf_secrets(financial_datasets_api_key="")
    assert cleared == AiHfSecrets()
    assert store.aihf_secrets().configured is False
    assert store.aihf_secrets().masked is None


def test_secrets_projection_is_masked_only(store: SettingsStore) -> None:
    store.save_aihf_secrets(financial_datasets_api_key="fd-key-1234")
    projection = store.aihf_secrets().to_projection()
    assert projection == {
        "financial_datasets": {"configured": True, "masked": "********1234"}
    }


def test_secrets_reject_malformed_values(store: SettingsStore) -> None:
    from bq.settings import SettingsError

    for bad in ("short", "has space in it", "x" * 300):
        with pytest.raises(SettingsError):
            store.save_aihf_secrets(financial_datasets_api_key=bad)


def test_malformed_stored_row_reads_as_unconfigured(store: SettingsStore) -> None:
    from bq.settings import load_aihf_secrets

    assert load_aihf_secrets(None) == AiHfSecrets()
    assert load_aihf_secrets("{not json") == AiHfSecrets()
    assert load_aihf_secrets('["a"]') == AiHfSecrets()
    assert load_aihf_secrets('{"financial_datasets_api_key": 42}') == AiHfSecrets()
