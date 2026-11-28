import pytest

from bq.llm_client import LLMClientError, OpenAICompatClient


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:11434/v1",
        "http://127.0.0.1:11434/v1/",
        "http://127.0.0.1:11434/v1/chat/completions",
        "http://localhost:8080/v1",
        "https://api.example.com/v1",
    ],
)
def test_base_or_full_url_resolves_to_one_completions_path(endpoint: str) -> None:
    client = OpenAICompatClient(endpoint)
    assert client.endpoint.endswith("/chat/completions")
    assert "//chat" not in client.endpoint
    assert client.endpoint.count("/chat/completions") == 1


def test_loopback_http_is_allowed_but_public_http_is_not() -> None:
    OpenAICompatClient("http://127.0.0.1:11434/v1")  # loopback is fine
    OpenAICompatClient("http://localhost:11434/v1")
    with pytest.raises(LLMClientError):
        OpenAICompatClient("http://example.com/v1")
