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

## Install as a Claude Code plugin

The repository is also a Claude Code plugin marketplace. The `market-research` plugin starts
this MCP server and adds the `analyze-ticker` skill (a sourced one-page brief per ticker, checked
by `skills/analyze-ticker/scripts/check_sources.py`).

1. Install [uv](https://docs.astral.sh/uv/). The plugin runs the server with `uv run`.
2. Put your keys in the per-user settings file (create it from `.env.example`):
   `%APPDATA%/market-research-mcp/.env` on Windows, `~/.config/market-research-mcp/.env`
   elsewhere. Real environment variables also work and take precedence. Do not put a `.env`
   in the plugin folder: it is replaced on every plugin update and is never read.
   - `SEC_USER_AGENT`: your name and email, required by the
     [SEC fair access policy](https://www.sec.gov/os/accessing-edgar-data). No key needed.
   - `FRED_API_KEY`: free key from [FRED](https://fred.stlouisfed.org/docs/api/api_key.html).
   - `TIINGO_API_KEY` (from [Tiingo](https://www.tiingo.com/account/api/token)) or
     `FINNHUB_API_KEY` (from [Finnhub](https://finnhub.io/dashboard)) with `PRICE_PROVIDER`.
   - Optional `JOURNAL_DIR`: briefs go to `<JOURNAL_DIR>/analyses/`, otherwise `./analyses/`.
3. In Claude Code, add the marketplace (replace `<owner>` with the GitHub owner of this
   repository): `/plugin marketplace add <owner>/market-research-mcp`
4. Install the plugin: `/plugin install market-research@market-research-mcp`, then restart
   Claude Code or run `/reload-plugins`.
5. Ask "analyze MSFT" or run `/market-research:analyze-ticker MSFT`. MCP clients without
   skills can use the server's `analyze_ticker` prompt, which returns the same procedure.

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
| `JOURNAL_DIR` | Output folder for `analyze-ticker` briefs | `./analyses/` |
| `MRM_ENV_FILE` | Path of a `.env` file to use instead of the per-user one | none |

Where settings come from, highest priority first:

1. Real environment variables.
2. The file named by `MRM_ENV_FILE`, if set. Otherwise the per-user file
   `%APPDATA%/market-research-mcp/.env` (Windows) or `~/.config/market-research-mcp/.env`
   (elsewhere; `$XDG_CONFIG_HOME` is honoured).
3. `.env` in the current working directory (for a clone, the repository root).

A `.env` inside the installed plugin folder (`CLAUDE_PLUGIN_ROOT`) is never read.

Missing keys do not stop the server; a tool that needs a key returns an error naming it.

## License

MIT
