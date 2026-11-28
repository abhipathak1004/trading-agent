"""v2 LLM layer — provider protocol, prompt cache, model registry.

The concrete provider transports are not vendored: BharatQuant injects its own
`LLMClient` (see `bq/aihf/bridge/llm.py`), so this package is the protocol, the
disk prompt cache, and the model-id registry alone.
"""

from bq.aihf.engine.llm.cache import PromptCache, prompt_key
from bq.aihf.engine.llm.protocol import (
    DEFAULT_MODEL,
    LLMCallError,
    LLMClient,
    LLMParseError,
    extract_json,
)
from bq.aihf.engine.llm.registry import (
    PROVIDER_ENV_VARS,
    SUPPORTED_PROVIDERS,
    env_var_for,
    is_supported,
    load_api_models,
    provider_for,
)
from bq.aihf.engine.llm.watch import ThesisStream

__all__ = [
    "DEFAULT_MODEL",
    "LLMCallError",
    "LLMClient",
    "LLMParseError",
    "PROVIDER_ENV_VARS",
    "PromptCache",
    "SUPPORTED_PROVIDERS",
    "ThesisStream",
    "env_var_for",
    "extract_json",
    "is_supported",
    "load_api_models",
    "prompt_key",
    "provider_for",
]
