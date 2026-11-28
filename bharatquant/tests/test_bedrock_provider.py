"""AWS Bedrock as a provider: the Converse client, the region, and the key.

Bedrock is additive (ADR-051): the five existing providers, their stored keys
and the active selection are unchanged. What is new is one provider member, one
widening of the ``provider_api_keys`` CHECK, one client, and a region setting.

Every HTTP assertion here runs against a mocked transport, so no AWS call is
made and no credential is needed.
"""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from bq.infra.database import migrate
from bq.llm_client import (
    BEDROCK_DEFAULT_REGION,
    BEDROCK_ENDPOINT,
    BedrockClient,
    LLMClientError,
    OpenAICompatClient,
    client_for,
)
from bq.settings import (
    Provider,
    SettingsError,
    SettingsStore,
    validate_bedrock_region,
)

CONVERSE_REPLY = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [{"text": "Stick to the plan."}],
        }
    },
    "stopReason": "end_turn",
    "usage": {"inputTokens": 120, "outputTokens": 9, "totalTokens": 129},
}


def _store(tmp_path: Path) -> SettingsStore:
    database = tmp_path / "bq.db"
    migrate(database, create=True)
    return SettingsStore(database)


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# the provider is additive
# ---------------------------------------------------------------------------


def test_existing_providers_are_untouched() -> None:
    """The five providers that existed before Bedrock must all still be there."""
    assert {p.value for p in Provider} == {
        "GEMINI",
        "DEEPSEEK",
        "OPENAI",
        "CUSTOM",
        "TYPESAFE_JEV",
        "BEDROCK",
    }


def test_migration_widens_the_check_without_losing_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A key stored before Bedrock existed still reads back after migration 041.
    store.set_api_key(Provider.GEMINI, "gemini-key-1234")
    assert store.api_key(Provider.GEMINI) == "gemini-key-1234"

    # And the new provider is admitted by the CHECK.
    store.set_api_key(Provider.BEDROCK, "bedrock-bearer-key-1234")
    assert store.api_key(Provider.BEDROCK) == "bedrock-bearer-key-1234"

    with sqlite3.connect(tmp_path / "bq.db") as connection:
        constraint = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='provider_api_keys'"
        ).fetchone()[0]
    assert "BEDROCK" in constraint
    assert "GEMINI" in constraint


def test_bedrock_key_is_masked_in_the_config_projection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_api_key(Provider.BEDROCK, "bedrock-bearer-key-1234")
    keys = {entry.provider: entry for entry in store.load().api_keys}
    assert keys[Provider.BEDROCK].configured is True
    assert keys[Provider.BEDROCK].masked == "********1234"


def test_bedrock_region_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load().bedrock_region is None
    store.save_bedrock_region("us-west-2")
    assert store.load().bedrock_region == "us-west-2"
    store.save_bedrock_region("")
    assert store.load().bedrock_region is None


def test_bedrock_region_is_normalised_and_validated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_bedrock_region("  EU-WEST-1 ")
    assert store.load().bedrock_region == "eu-west-1"
    for bad in ("not-a-region", "us_east_1", "us-east", "a" * 40):
        with pytest.raises(SettingsError):
            store.save_bedrock_region(bad)


def test_a_region_cannot_choose_the_host(tmp_path: Path) -> None:
    """The region lands in a hostname, so injection must be impossible."""
    for bad in ("evil.com/", "us-east-1.evil.com", "us-east-1:443@evil.com"):
        with pytest.raises((SettingsError, LLMClientError)):
            validate_bedrock_region(bad)
        with pytest.raises(LLMClientError):
            BedrockClient(region=bad)


# ---------------------------------------------------------------------------
# client selection
# ---------------------------------------------------------------------------


def test_client_for_returns_a_bedrock_client_with_the_region() -> None:
    client = client_for(Provider.BEDROCK, bedrock_region="ap-south-1")
    assert isinstance(client, BedrockClient)
    assert client.region == "ap-south-1"


def test_client_for_defaults_the_bedrock_region() -> None:
    client = client_for(Provider.BEDROCK)
    assert isinstance(client, BedrockClient)
    assert client.region == BEDROCK_DEFAULT_REGION


def test_existing_client_routing_is_unchanged() -> None:
    """Adding a provider must not divert any existing one."""
    assert isinstance(client_for(Provider.OPENAI), OpenAICompatClient)
    assert isinstance(client_for(Provider.DEEPSEEK), OpenAICompatClient)
    assert isinstance(
        client_for(Provider.CUSTOM, custom_endpoint="http://127.0.0.1:11434/v1"),
        OpenAICompatClient,
    )


# ---------------------------------------------------------------------------
# the Converse request and response
# ---------------------------------------------------------------------------


def test_converse_request_shape_and_parsing() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=CONVERSE_REPLY)

    client = BedrockClient(region="us-east-1", transport=_mock(handler))
    result = client.complete(
        model="us.anthropic.claude-sonnet-4-6",
        api_key="bedrock-bearer-key-1234",
        system="You are a risk manager.",
        prompt="Should I add to AAPL?",
    )

    assert seen["url"] == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/"
        "us.anthropic.claude-sonnet-4-6/converse"
    )
    # A Bedrock API key is a bearer token, never a SigV4 signature.
    assert seen["auth"] == "Bearer bedrock-bearer-key-1234"
    body = seen["body"]
    assert body["messages"] == [
        {"role": "user", "content": [{"text": "Should I add to AAPL?"}]}
    ]
    assert body["system"] == [{"text": "You are a risk manager."}]
    assert body["inferenceConfig"] == {"temperature": 0.2}

    assert result.text == "Stick to the plan."
    assert result.finish_reason == "stop"  # Bedrock's `end_turn`
    assert result.input_tokens == 120
    assert result.output_tokens == 9


def test_converse_omits_the_system_block_when_empty() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=CONVERSE_REPLY)

    client = BedrockClient(transport=_mock(handler))
    client.complete(model="m", api_key="k" * 16, system="", prompt="hi")
    assert "system" not in seen["body"]


def test_converse_maps_max_tokens_to_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {"message": {"content": [{"text": "cut off"}]}},
                "stopReason": "max_tokens",
                "usage": {"inputTokens": 1, "outputTokens": 2},
            },
        )

    result = BedrockClient(transport=_mock(handler)).complete(
        model="m", api_key="k" * 16, system="", prompt="hi"
    )
    assert result.finish_reason == "max_tokens"


def test_a_model_id_cannot_escape_the_path() -> None:
    """A slash would silently address a different Bedrock resource."""
    client = BedrockClient(transport=_mock(lambda r: httpx.Response(200, json={})))
    for bad in ("../../admin", "model/with/slash", "", "a b"):
        with pytest.raises(LLMClientError, match="model id"):
            client.complete(model=bad, api_key="k" * 16, system="", prompt="hi")


def test_a_non_200_is_a_typed_error_without_echoing_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "key fragment bedrock-bearer-key-1234 rejected"},
        )

    client = BedrockClient(transport=_mock(handler))
    with pytest.raises(LLMClientError) as excinfo:
        client.complete(
            model="m", api_key="bedrock-bearer-key-1234", system="", prompt="hi"
        )
    assert "403" in str(excinfo.value)
    assert "bedrock-bearer-key-1234" not in str(excinfo.value)


def test_an_empty_answer_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output": {"message": {"content": []}}, "stopReason": "end_turn"},
        )

    with pytest.raises(LLMClientError, match="empty text"):
        BedrockClient(transport=_mock(handler)).complete(
            model="m", api_key="k" * 16, system="", prompt="hi"
        )


def test_a_malformed_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(LLMClientError):
        BedrockClient(transport=_mock(handler)).complete(
            model="m", api_key="k" * 16, system="", prompt="hi"
        )


def test_the_endpoint_template_names_the_region_and_model() -> None:
    assert (
        BEDROCK_ENDPOINT.format(region="us-east-1", model="m")
        == "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse"
    )


# ---------------------------------------------------------------------------
# the HTTP surface
# ---------------------------------------------------------------------------


def test_region_and_key_routes_round_trip(tmp_path: Path) -> None:
    import asyncio

    from bq.api import ALLOWED_ORIGIN, create_app
    from bq.auth import OwnerAuth

    database = tmp_path / "bq.db"
    migrate(database, create=True)
    app = create_app(database_path=database)
    password = "synthetic owner password"

    async def check() -> None:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        )
        try:
            OwnerAuth(database).setup(password)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": password},
            )
            csrf = login.json()["csrf_token"]
            write = {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf}

            # Ungated writes are refused.
            assert (
                await client.put(
                    "/api/v1/settings/bedrock-region",
                    headers={"Origin": ALLOWED_ORIGIN},
                    json={"region": "us-east-1"},
                )
            ).status_code == 403

            saved = await client.put(
                "/api/v1/settings/bedrock-region",
                headers=write,
                json={"region": "us-west-2"},
            )
            assert saved.status_code == 200
            assert saved.json()["bedrock_region"] == "us-west-2"

            bad = await client.put(
                "/api/v1/settings/bedrock-region",
                headers=write,
                json={"region": "nope"},
            )
            assert bad.status_code == 422

            key = await client.put(
                "/api/v1/settings/api-keys/BEDROCK",
                headers=write,
                json={"api_key": "bedrock-bearer-key-1234"},
            )
            assert key.status_code == 200
            entry = next(
                item for item in key.json()["api_keys"] if item["provider"] == "BEDROCK"
            )
            assert entry["configured"] is True
            assert entry["masked"] == "********1234"
            assert "bedrock-bearer-key-1234" not in key.text

            # Clearing the region leaves the key intact.
            cleared = await client.put(
                "/api/v1/settings/bedrock-region", headers=write, json={"region": ""}
            )
            assert cleared.json()["bedrock_region"] is None
            assert any(
                item["provider"] == "BEDROCK" for item in cleared.json()["api_keys"]
            )
        finally:
            await client.aclose()

    asyncio.run(check())


def test_applying_bedrock_routes_text_agents_and_keeps_decision(tmp_path: Path) -> None:
    """The mutation must move the text tiers and leave the decision tier alone."""
    import asyncio

    from bq.api import ALLOWED_ORIGIN, create_app
    from bq.auth import OwnerAuth
    from bq.settings import TierName

    database = tmp_path / "bq.db"
    migrate(database, create=True)
    store = SettingsStore(database)
    seed = store.load()
    store.save_models(
        tiers=dict(seed.tiers),
        agent_routing=dict(seed.agent_routing),
        pipeline=seed.pipeline,
        decision_route=seed.decision_route,
        decision_policy=seed.decision_policy,
    )
    app = create_app(database_path=database)
    password = "synthetic owner password"

    async def check() -> None:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        )
        try:
            OwnerAuth(database).setup(password)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": password},
            )
            write = {
                "Origin": ALLOWED_ORIGIN,
                "X-CSRF-Token": login.json()["csrf_token"],
            }
            response = await client.post(
                "/api/v1/settings/bedrock/apply",
                headers=write,
                json={"model": "us.anthropic.claude-sonnet-4-6", "region": "us-east-1"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["tiers"]["reasoning"]["provider"] == "BEDROCK"
            assert body["tiers"]["flash"]["provider"] == "BEDROCK"
            assert (
                body["tiers"]["reasoning"]["model"] == "us.anthropic.claude-sonnet-4-6"
            )
            # The decision tier is TypeSafe Jev only, and must be untouched.
            assert body["tiers"]["decision"]["provider"] != "BEDROCK"
            assert body["bedrock_region"] == "us-east-1"

            saved = SettingsStore(database).load()
            assert saved.tier(TierName.REASONING).provider is Provider.BEDROCK
            assert saved.tier(TierName.FLASH).provider is Provider.BEDROCK

            # Every non-decision agent role now resolves to Bedrock, and the
            # decision roles are untouched: that is the whole contract.
            from bq.agent_catalogue import AGENT_CATALOGUE

            for entry in AGENT_CATALOGUE:
                route = saved.route_for(entry.agent_id)
                provider = saved.tier(route.tier).provider
                if route.tier is TierName.DECISION:
                    assert provider is not Provider.BEDROCK, entry.agent_id
                else:
                    assert provider is Provider.BEDROCK, entry.agent_id
        finally:
            await client.aclose()

    asyncio.run(check())
