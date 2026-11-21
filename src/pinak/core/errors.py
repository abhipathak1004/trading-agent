"""Typed errors shared across providers and agents."""

from __future__ import annotations


class PinakError(RuntimeError):
    """Base class for every recoverable Pinak failure."""


class DataUnavailableError(PinakError):
    """A source had nothing for this day (holiday, not yet published, 404)."""


class DataQualityError(PinakError):
    """A source returned data, but it cannot be trusted."""
