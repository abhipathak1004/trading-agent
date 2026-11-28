"""AI Hedge Fund v2 engine, vendored and ported into BharatQuant.

Upstream: the `hedge_fund` package from `aihf==2.7.2`
(https://github.com/virattt/ai-hedge-fund, v2), MIT licensed — see LICENSE.
Read PATCHES.md for every deviation from upstream and why.

A fund runs one verb, `advance(fund, state, session, broker, data_client,
universe)`, once per completed session. A backtest loops it over history; a
paper fund calls it once per due session. The engine takes its market data and
its LLM through the two protocols in `data.protocol` and `llm.protocol`, so it
has no vendor client and no model id of its own: BharatQuant injects both (see
`bq/aihf/bridge/`).

The application surface the service calls is `engine.api`.
"""
