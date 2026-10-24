# market-research-mcp

An [MCP](https://modelcontextprotocol.io) server with read-only research tools over public
market data: SEC EDGAR filings and XBRL fundamentals, FRED macro series, end-of-day prices and
earnings dates. It ships as a Claude Code plugin with an `analyze-ticker` skill.

- **Read-only by construction.** There is no code that places, modifies or cancels orders or
  moves money. No broker connection.
- **Not financial advice.** Tools return data with its source URL and date. Nothing produces
  buy/sell recommendations, ratings or price targets.
- **Bring your own keys.** Keys are read from environment variables or a local `.env` file.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs Python 3.12+ for
  you from `.python-version`; no system Python needed.
- Git. Both the clone and the Claude Code plugin install fetch this repository with git.

## Keys

| Variable | Required? | Where to get it | Free tier |
|---|---|---|---|
| `SEC_USER_AGENT` | Yes, for all SEC tools | No signup. Your name and email, e.g. `Jane Doe jane@example.com` ([SEC policy](https://www.sec.gov/os/accessing-edgar-data)) | Free. Max 10 requests/second |
| `FRED_API_KEY` | For `get_macro_series` | [fredaccount.stlouisfed.org/apikeys](https://fredaccount.stlouisfed.org/apikeys) | Free. The terms publish no numeric limit; 120 requests/minute is commonly cited (UNVERIFIED) |
| `TIINGO_API_KEY` | For price tools (default provider) | [tiingo.com/account/api/token](https://www.tiingo.com/account/api/token) | Starter: 50 requests/hour, 1,000/day, 500 unique symbols/month, internal (personal) use only ([pricing](https://www.tiingo.com/about/pricing)) |
| `FINNHUB_API_KEY` | Optional, for announced earnings dates | [finnhub.io/register](https://finnhub.io/register) | About 60 calls/minute, earnings calendar included, personal use only (UNVERIFIED). Without a key, dates are estimated from past SEC filings and marked as estimates |
| `PRICE_PROVIDER` | Optional | `tiingo` (default) or `yfinance` (unofficial; `uv sync --extra yfinance`) | n/a |
| `CACHE_PATH` | Optional | SQLite cache, default `~/.market-research/cache.db` | n/a |
| `LOG_LEVEL` | Optional | Default `INFO`; logs go to stderr | n/a |
| `JOURNAL_DIR` | Optional | Briefs go to `<JOURNAL_DIR>/analyses/`, otherwise `./analyses/` | n/a |
| `MRM_ENV_FILE` | Optional | Path of a `.env` file to use instead of the per-user one | n/a |

Missing keys do not stop the server: a tool that needs a key returns an error naming it.

## Where to put `.env`

Copy [`.env.example`](.env.example) and fill it in. Settings are read in this order, first
wins:

1. Real environment variables.
2. The file named by `MRM_ENV_FILE`, if set. Otherwise the per-user file:
   `%APPDATA%\market-research-mcp\.env` on Windows, `~/.config/market-research-mcp/.env`
   elsewhere (`$XDG_CONFIG_HOME` is honoured).
3. `.env` in the current working directory (for a clone, the repository root).

A `.env` inside the installed plugin folder is never read: plugin updates replace that folder.
`.env` is gitignored; never commit it.

## Install as a Claude Code plugin

1. Install uv and create the per-user `.env` above with at least `SEC_USER_AGENT`.
2. In Claude Code: `/plugin marketplace add romanmoravskyi/market-research-mcp`
3. `/plugin install market-research@market-research-mcp`, then restart Claude Code or run
   `/reload-plugins`.
4. Ask "analyze MSFT" or run `/market-research:analyze-ticker MSFT`.

The plugin starts the MCP server with `uv run` and adds the `analyze-ticker` skill.

## Use as a plain MCP server

Clone and install:

```sh
git clone https://github.com/romanmoravskyi/market-research-mcp.git
cd market-research-mcp
uv sync
```

Claude Desktop (`claude_desktop_config.json`; use absolute paths):

```json
{
  "mcpServers": {
    "market-research": {
      "command": "uv",
      "args": ["--directory", "/path/to/market-research-mcp", "run", "market-research-mcp"],
      "env": { "MRM_ENV_FILE": "/path/to/your/.env" }
    }
  }
}
```

Any stdio MCP client: launch `uv --directory /path/to/market-research-mcp run market-research-mcp`;
the server speaks MCP over stdin/stdout. In Claude Code without the plugin:

```sh
claude mcp add market-research -- uv --directory /path/to/market-research-mcp run market-research-mcp
```

## Tools

| Tool | What it returns |
|---|---|
| `ping` | Server status and version |
| `resolve_ticker` | Company name, CIK, exchange and SIC code from SEC |
| `list_filings` | SEC filings for a ticker, filtered by form and date |
| `get_filing_section` | Text of one 10-K/10-Q item (e.g. 1A risk factors), paged |
| `get_filing_exhibit` | Text of a filing exhibit (e.g. EX-99.1 earnings release) |
| `get_insider_transactions` | SEC Form 4 insider transactions with transaction codes |
| `get_fundamentals` | XBRL fundamentals, annual or quarterly; derived values are marked |
| `get_key_ratios` | Margins, ROE, ROA, debt/equity, FCF margin, P/E, each with formula and source |
| `get_price_history` | End-of-day prices from the configured provider |
| `get_price_stats` | Returns, max drawdown, 52-week range and volatility |
| `get_earnings_calendar` | Next earnings date per ticker (Finnhub, or an SEC-based estimate) |
| `get_macro_series` | FRED series observations |

## The `analyze_ticker` prompt

MCP clients without skill support can call the server's `analyze_ticker` prompt with a ticker.
It returns the same procedure as the `analyze-ticker` skill: a one-page brief where every number
has a source URL and date, unverifiable numbers are listed as UNVERIFIED, and there are no
recommendations.

## Running tests

```sh
uv run pytest              # offline, recorded fixtures; live tests are skipped by default
uv run pytest -m live      # opt-in: real network and your own keys
uv run ruff check . && uv run ruff format --check .
```

## Data sources and attribution

- **SEC EDGAR**: public-domain US government data. Follow the
  [fair access policy](https://www.sec.gov/os/accessing-edgar-data): a User-Agent with your
  contact details and at most 10 requests/second (the server rate-limits its own requests).
- **FRED**: This product uses the FRED® API but is not endorsed or certified by the Federal
  Reserve Bank of St. Louis. Use is subject to the
  [FRED API Terms of Use](https://fred.stlouisfed.org/docs/api/terms_of_use.html). Some series
  are copyrighted by third parties; get the owner's permission before uses beyond personal use.
- **Tiingo and Finnhub**: free tiers are for personal or internal use and their data may not be
  redistributed. Each user brings their own key; this repository's Tiingo and Finnhub test
  fixtures are synthetic.
- **yfinance** (opt-in): unofficial Yahoo Finance access, personal use only.

## License

[MIT](LICENSE). Not financial, tax or legal advice.
