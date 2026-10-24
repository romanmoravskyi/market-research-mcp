import pytest

from market_research_mcp import tickers
from market_research_mcp.errors import BadInput
from market_research_mcp.sources import edgar, prices


@pytest.mark.parametrize("raw", ["BRK/B", "brk.b", " brk-b ", "Brk.B"])
@pytest.mark.parametrize(
    "fn", [tickers.normalize_ticker, edgar.normalize_ticker, prices.normalize_ticker]
)
def test_normalize_share_class(fn, raw):
    assert fn(raw) == "BRK-B"


@pytest.mark.parametrize("bad", ["", "  ", "MS FT", "-ABC", "A" * 20, "MSFT;"])
@pytest.mark.parametrize(
    "fn", [tickers.normalize_ticker, edgar.normalize_ticker, prices.normalize_ticker]
)
def test_normalize_invalid_is_bad_input(fn, bad):
    with pytest.raises(BadInput):
        fn(bad)


def test_one_shared_implementation():
    assert edgar.normalize_ticker is tickers.normalize_ticker
    assert prices.normalize_ticker is tickers.normalize_ticker
