"""Shared result models. Every number shown to the user carries its source and date."""

from __future__ import annotations

from datetime import UTC, date, datetime

from pydantic import BaseModel, Field, field_validator

__all__ = ["Sourced", "SourcedList", "utcnow"]


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Sourced(BaseModel):
    """Base for any result: where it came from, when it was fetched, what date it describes."""

    source: str
    source_url: str
    retrieved_at: datetime
    as_of: date | datetime | None
    stale: bool = False
    notes: list[str] = Field(default_factory=list)

    @field_validator("retrieved_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class SourcedList[T](Sourced):
    """A list result. `truncated` is True when more items existed than `limit`."""

    items: list[T]
    limit: int | None = None
    truncated: bool = False
