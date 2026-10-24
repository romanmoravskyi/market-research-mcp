---
name: analyze-ticker
description: Produce a sourced one-page research brief for a US-listed ticker (fundamentals, ratios, price stats, 10-K risks, earnings releases, insider activity, next earnings) using the market-research MCP server. Use when the user asks to analyze, research, profile or "look at" a ticker. Facts only; every number cited; no recommendations.
---

# analyze-ticker

Read-only research. The user makes every investment decision.

## Hard rules
- No buy/sell/hold language, no price targets, no ratings, no "undervalued/overvalued" or
  "cheap/expensive" verdicts, no "we recommend". Report facts, changes and risks only.
- Every number carries a source URL and a date (as_of, period end or filing date), inline or
  via a footnote ref `[n]` pointing to the numbered Sources list.
- A number you cannot tie to a tool response with `source_url` goes in "Could not verify",
  marked UNVERIFIED. Never estimate, interpolate or fill from memory.
- If a tool returns null with a reason (e.g. ratios for banks/REITs), show "n/a" plus the reason.
  If a tool is not available on the server, show "n/a - tool not available".
- The brief is research tooling output, not financial advice, and says so in its footer.

## Steps (in this order, all via MCP server `market-research`)
1. `resolve_ticker(query=<ticker>)` -> name, CIK, exchange, SIC. Stop and report if not found.
2. `get_fundamentals(ticker, metrics=[revenue, net_income, diluted_eps, operating_cash_flow,
   capex, total_debt, cash, shares_diluted], periods="annual", n=5)`, then the same with
   `periods="quarterly", n=8`. Keep XBRL tag, period start/end and filed date per value.
3. Ratios: margins, revenue growth YoY and 3y CAGR, FCF, net debt, share-count change. Use
   `get_key_ratios(ticker)` if the server lists it for margins, ROE, ROA, debt/equity, current
   ratio and FCF margin. It does not return revenue growth, CAGR, net debt or share-count
   change, so always compute those from the step 2 `get_fundamentals` values. Without
   `get_key_ratios`, compute all ratios from the step 2 `get_fundamentals` values. Show the
   formula and inputs for each computed ratio (e.g. "FCF = operating_cash_flow - capex = $X -
   $Y"), citing the same sources. If an input is null, the ratio is "n/a" with that reason.
4. `get_price_stats(ticker)` -> 1m/3m/1y/5y returns, max drawdown, 52w range, volatility;
   record `provider` and `as_of`.
5. Latest 10-K: `list_filings(ticker, forms=["10-K"], limit=1)`, then
   `get_filing_section(ticker, accession, "1A")` (pass the returned `next_cursor` as
   `cursor` until there is none). Summarize the top 5
   risks in one line each, in neutral words; cite the filing URL with accession number.
6. Last 2 earnings releases: `list_filings(ticker, forms=["8-K"], since=<18 months ago>, limit=100)`,
   keep 8-Ks with Item 2.02, take the latest 2, `get_filing_exhibit(ticker, accession, "EX-99.1")`.
   Extract reported headline figures only (revenue, EPS, guidance if stated), each cited.
7. Insiders, last 12 months: `get_insider_transactions(ticker, since=<12 months ago>)`.
   Headline: only codes P (open-market purchase) and S (open-market sale): counts, shares,
   value. All other codes (A, M, F, G, ...) go in a footnote as counts, labelled "not
   open-market trades". If `filings_capped` is true, the totals cover only filings from
   `covered_since`: state that window instead of "12 months".
8. `get_earnings_calendar([ticker], days_ahead=120)` -> next earnings date, or "none found".
9. Choose the output folder. Get `JOURNAL_DIR` from the same settings the server uses
   (environment, `MRM_ENV_FILE` or the per-user `.env`, then `.env` in the current folder).
   In the Claude Code plugin, run
   `uv run --quiet --no-dev --frozen --project "${CLAUDE_PLUGIN_ROOT}" python -m market_research_mcp.config journal-dir`;
   elsewhere, read the `JOURNAL_DIR` environment variable. If it gives a path, use
   `<JOURNAL_DIR>/analyses/`; otherwise use `./analyses/` under the current working directory.
   Create the folder if needed. Write the one-pager to `<folder>/<TICKER>-<YYYY-MM-DD>.md`
   (today's date) using the template below. Footnote refs `[n]` must match the numbered
   Sources list. If you cannot write files, return the one-pager as your answer instead.
10. Check sources. In the Claude Code plugin, run
    `uv run --quiet --no-dev --frozen --project "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/skills/analyze-ticker/scripts/check_sources.py" "<path to the one-pager>"`.
    Elsewhere, run `check_sources.py` from the `skills/analyze-ticker/scripts/` folder of the
    market-research-mcp repository the same way, or, if you cannot run it, check every line
    by hand against the hard rules. Fix every flagged line (add the source, move it to
    "Could not verify", or reword) and re-run until it exits 0. Then tell the user the file
    path and the two or three most notable facts.

## Template

```markdown
# <Company name> (<TICKER>) - research brief

CIK: <cik> | Exchange: <exchange> | SIC: <sic> | Generated: <YYYY-MM-DD>

## Snapshot
| Field | Value | Source |
|---|---|---|
(name, exchange, SIC, last close + as_of, 52w range, next earnings)

## Fundamentals
| Metric | Value | Period | Source | Filed |
|---|---|---|---|---|
(5 annual rows per key metric, then last 8 quarters for revenue, net income, diluted EPS)

10-K cross-check: annual revenue, net income and diluted EPS above <match | differ: detail>
the latest 10-K values from get_fundamentals (accession <accession>) [n].

## Ratios
(bullets: metric, value, period, [n]; "n/a - <reason>" when null)

## Price stats
Provider: <provider>, as of <as_of> [n]. (returns, max drawdown, 52w range, volatility)

## Risks (latest 10-K, Item 1A)
1. <risk in one neutral line> [n]
(top 5)

## Earnings releases
- <date> (8-K Item 2.02, EX-99.1): <reported figures> [n]
(last 2)

## Insiders (12 months)
Open-market buy/sell (codes P/S): <counts, shares, value> [n]
Footnote: other Form 4 codes <code: count, ...>, not open-market trades [n].

## Next earnings
<date or "none found within 120 days"> [n]

## Sources
1. <description>, <source_url>, <as_of / filed / retrieved date>
(one entry per distinct source_url; numbering matches inline refs)

## Could not verify
- UNVERIFIED: <item and why> (or "Nothing.")

---
Research tooling output, not financial advice.
```

## Style
- Neutral verbs: "reported", "increased", "decreased", "disclosed". No adjectives that judge.
- Units explicit ($B, $M, %, shares). State fiscal-year end when it is not December.
- Keep it to one page: tables over prose.
