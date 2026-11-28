"""Agent tooling for BharatQuant.

The pieces are deliberately small and explicit:

* ``base`` - the Tool contract, specs, bounded validation and result type.
* ``registry`` - explicit registration plus a per-agent access policy.
* ``parsing`` - a strict extractor for a tool call in model text.
* ``control`` - the ``response`` terminator and ``unknown`` fallback.
* ``news`` - the tools the news research agent exposes.
* ``market`` - live quote/download and web-fetch tools.

Design provenance: the *structure* is adapted from Agent Zero (a Tool base that
returns a message and a loop-break flag, explicit tool policy separate from
discovery, a response tool that ends the loop, an unknown-tool fallback). The
code is original and written to BharatQuant conventions: explicit registration
instead of import-by-path, strict bounded validation, and no silent repair.
"""

from bq.tools.base import (
    Tool,
    ToolArgument,
    ToolResult,
    ToolSpec,
    ToolValidationError,
    render_tools_prompt,
)
from bq.tools.control import ResponseTool, UnknownTool
from bq.tools.market import (
    StockDownloadTool,
    StockQuoteTool,
    WebFetchTool,
    market_tools,
)
from bq.tools.news import (
    NewsDigestTool,
    NewsIngestTool,
    NewsMemoryTool,
    NewsSearchTool,
    news_tools,
)
from bq.tools.registry import AgentToolPolicy, ToolRegistry, ToolRunner

__all__ = [
    "AgentToolPolicy",
    "NewsDigestTool",
    "NewsIngestTool",
    "NewsMemoryTool",
    "NewsSearchTool",
    "ResponseTool",
    "StockDownloadTool",
    "StockQuoteTool",
    "Tool",
    "ToolArgument",
    "ToolRegistry",
    "ToolResult",
    "ToolRunner",
    "ToolSpec",
    "ToolValidationError",
    "UnknownTool",
    "WebFetchTool",
    "market_tools",
    "news_tools",
    "render_tools_prompt",
]
