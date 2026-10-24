from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from market_research_mcp.errors import (
    BadInput,
    ConfigError,
    NotFound,
    ProviderDown,
    RateLimited,
    ToolError,
    as_tool_error,
)
from market_research_mcp.models import Sourced, SourcedList, utcnow


def test_utcnow_is_aware_utc():
    assert utcnow().utcoffset() == timedelta(0)


def test_sourced_requires_source_url():
    with pytest.raises(ValidationError):
        Sourced(source="SEC EDGAR", retrieved_at=utcnow(), as_of=None)


def test_sourced_defaults_and_utc_normalisation():
    plus3 = timezone(timedelta(hours=3))
    s = Sourced(
        source="FRED",
        source_url="https://fred.stlouisfed.org/series/DGS10",
        retrieved_at=datetime(2026, 10, 24, 15, 0, tzinfo=plus3),
        as_of=date(2026, 10, 23),
    )
    assert s.retrieved_at == datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert s.retrieved_at.utcoffset() == timedelta(0)
    assert s.stale is False
    assert s.notes == []


def test_naive_retrieved_at_assumed_utc():
    s = Sourced(source="x", source_url="u", retrieved_at=datetime(2026, 1, 1), as_of=None)
    assert s.retrieved_at.tzinfo == UTC


def test_notes_not_shared_between_instances():
    a = Sourced(source="x", source_url="u", retrieved_at=utcnow(), as_of=None)
    a.notes.append("n")
    b = Sourced(source="x", source_url="u", retrieved_at=utcnow(), as_of=None)
    assert b.notes == []


def test_sourced_list_generic():
    lst = SourcedList[int](
        source="x",
        source_url="u",
        retrieved_at=utcnow(),
        as_of=None,
        items=[1, 2],
        limit=2,
        truncated=True,
    )
    assert lst.items == [1, 2] and lst.truncated
    with pytest.raises(ValidationError):
        SourcedList[int](
            source="x", source_url="u", retrieved_at=utcnow(), as_of=None, items=["nope"]
        )
    dumped = lst.model_dump(mode="json")
    assert dumped["source"] == "x" and dumped["limit"] == 2


def test_errors_messages_and_as_tool_error():
    assert issubclass(NotFound, ToolError)
    assert str(NotFound("CIK 123", "edgar")) == "Not found at edgar: CIK 123."
    assert "45 seconds" in str(RateLimited("fred", 45))
    pd = ProviderDown("tiingo", "HTTP 503", alternative="finnhub")
    assert "tiingo is unavailable (HTTP 503)" in str(pd) and "finnhub" in str(pd)
    assert str(BadInput("ticker must be letters")).startswith("Invalid input:")
    assert as_tool_error(ToolError("bad ?token=SECRET")) == "bad ?token=***"
    assert "FRED_API_KEY" in as_tool_error(ConfigError("Missing FRED_API_KEY"))
    msg = as_tool_error(ValueError("https://x?api_key=SECRET"))
    assert "SECRET" not in msg and "ValueError" in msg
