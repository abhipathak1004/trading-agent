# External-source verification gates

Last checked: 2026-11-26. These notes are not a substitute for checking current,
effective-dated rules and terms when the affected feature is implemented.

| Area | Primary source checked | Result | Feature gate |
| --- | --- | --- | --- |
| NSE market timings | [NSE market timings](https://www.nseindia.com/static/market-data/market-timings) | Request timed out; no timing rules verified. | Do not seed sessions or permit session-dependent entries until current authoritative rules and dates are checked. |
| NSE holidays | [NSE holiday calendar](https://www.nseindia.com/static/market-data/holiday-calendar) | Request timed out; no holiday calendar verified. | Uncovered dates must not be treated as trading sessions. |
| NSE data permissions | Official NSE data terms not yet reviewed. | Data permissions and permitted use remain unknown. | No live provider adapter until applicable terms and permissions are verified. |
| Gemini API | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) | Access was denied by the current network policy. Pricing, capabilities, privacy and quota semantics were not verified. | Do not enable a Gemini adapter or assume free-tier privacy or quota behavior. Obtain an authorized review of the current documentation. |
| DeepSeek API | [DeepSeek API pricing](https://api-docs.deepseek.com/quick_start/pricing) | Request returned HTTP 403. Pricing, capabilities, privacy and quota semantics were not verified. | Do not enable a DeepSeek adapter until its current documentation and terms can be reviewed through authorized access. |
| TypeSafe Jev API | [Jev API reference](https://docs.typesafe.ai/introduction) and the field-by-field [API guide](https://typesafe-jev.com/en/guides/api/) | Docs reachable on 2026-11-26. Endpoint `POST https://api.typesafe.ai/v1/systemone`, Bearer auth, `jev-latest`/`jev-1.14.0`, primitives Choice/Score/Noul, bounded request sizes and 400/401/413/415/422/429/529 errors were read. Pricing, privacy and terms were **not** reviewed. | Jev is optional (ADR-017). Live traffic is owner-key opt-in; without a key the decision tier falls back to the configured LLM or `RULES_ONLY`. Do not present Jev calibration as verified until terms and recorded responses are reviewed. |
| SEBI and settlement | [SEBI](https://www.sebi.gov.in/) | Homepage reachable, but no applicable rule or effective date was reviewed. | Do not implement settlement, auction or charge rules using unverified assumptions. |

For each feature, retain the authoritative document, its publication and
effective dates, the specific applicable claim, the review date and any
limitations. A successful website request alone does not verify a rule or
grant permission to use data.
