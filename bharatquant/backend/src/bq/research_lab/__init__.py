"""The Research Lab: provenance contracts, frozen inputs, evidence and replay.

Every module here is additive. SQLite remains the single transactional
authority; the Polars frames and Parquet slices this package produces are
derived, read-only artefacts (ADR-054).
"""

__all__: list[str] = []
