# External-source verification gates

Last checked: 2026-11-21. These notes are not a substitute for checking current,
effective-dated rules and terms when the affected feature is implemented.

| Area | Primary source checked | Result | Feature gate |
| --- | --- | --- | --- |
| NSE market timings | [NSE market timings](https://www.nseindia.com/static/market-data/market-timings) | Request timed out; no timing rules verified. | Do not seed sessions or permit session-dependent entries until current authoritative rules and dates are checked. |
| NSE holidays | [NSE holiday calendar](https://www.nseindia.com/static/market-data/holiday-calendar) | Request timed out; no holiday calendar verified. | Uncovered dates must not be treated as trading sessions. |
| NSE data permissions | Official NSE data terms not yet reviewed. | Data permissions and permitted use remain unknown. | No live provider adapter until applicable terms and permissions are verified. |
| Gemini API | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) | Access was denied by the current network policy. Pricing, capabilities, privacy and quota semantics were not verified. | Do not enable a Gemini adapter or assume free-tier privacy or quota behavior. Obtain an authorized review of the current documentation. |
| DeepSeek API | [DeepSeek API pricing](https://api-docs.deepseek.com/quick_start/pricing) | Request returned HTTP 403. Pricing, capabilities, privacy and quota semantics were not verified. | Do not enable a DeepSeek adapter until its current documentation and terms can be reviewed through authorized access. |
| SEBI and settlement | [SEBI](https://www.sebi.gov.in/) | Homepage reachable, but no applicable rule or effective date was reviewed. | Do not implement settlement, auction or charge rules using unverified assumptions. |

For each feature, retain the authoritative document, its publication and
effective dates, the specific applicable claim, the review date and any
limitations. A successful website request alone does not verify a rule or
grant permission to use data.
