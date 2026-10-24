# market-research-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude read-only access to public
market data: SEC EDGAR filings, FRED macro series and end-of-day prices.

- **Not financial advice.** The server returns data with its source and retrieval date. It never
  produces buy/sell recommendations or price targets.
- **Read-only by construction.** There is no code that places, modifies or cancels orders or
  moves money.
- **Bring your own keys.** Keys are read from environment variables or a local `.env` file.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12+ for you).

```sh
git clone <repo-url> market-research-mcp
cd market-research-mcp
uv sync
cp .env.example .env   # then fill in your keys
uv run pytest -m "not live"
```

## Register in Claude Code

```sh
claude mcp add market-research -- uv --directory /path/to/market-research-mcp run market-research-mcp
```

## Environment variables

| Variable | Required for | Default |
|---|---|---|
| `SEC_USER_AGENT` | SEC EDGAR (for example `Your Name you@example.com`) | none |
| `FRED_API_KEY` | FRED macro data | none |
| `PRICE_PROVIDER` | Price source: `tiingo` or `finnhub` | `tiingo` |
| `TIINGO_API_KEY` | Prices via Tiingo | none |
| `FINNHUB_API_KEY` | Prices via Finnhub | none |
| `CACHE_PATH` | SQLite cache location | `~/.market-research/cache.db` |
| `LOG_LEVEL` | Log verbosity (logs go to stderr) | `INFO` |

Missing keys do not stop the server; a tool that needs a key returns an error naming it.

## License

MIT
