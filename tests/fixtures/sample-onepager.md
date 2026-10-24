# Example Corp (EXMPL) - research brief

> TEST FIXTURE: fictional company, all numbers fake. Used only by tests/test_check_sources.py.

CIK: 0000999999 | Exchange: NASDAQ | SIC: 7372 | Generated: 2026-10-24

## Snapshot

| Field | Value | Source |
|---|---|---|
| Name | Example Corp | [1] |
| SIC | 7372 Prepackaged software | [1] |
| Market price (close) | $123.45 as of 2026-10-23 | [4] |

## Fundamentals (annual, 5y)

| Metric | Value | Period | Source | Filed |
|---|---|---|---|---|
| Revenue | $12.3B | FY2025 (2024-07-01 to 2025-06-30) | [2] | 2025-08-01 |
| Net income | $2,345M | FY2025 | [2] | 2025-08-01 |
| Diluted EPS | $1.23 | FY2025 | [2] | 2025-08-01 |
| Revenue | $10.9B | FY2024 | [2] | 2024-08-02 |

10-K cross-check: revenue, net income and diluted EPS match the latest 10-K (accession 0000999999-25-000123) [2].

## Ratios

- Gross margin 68.2%, revenue growth YoY 12.8%, 3y CAGR 10.1% [3]
- Net debt: -$1.2B (net cash) [3]

## Price stats

Provider: ExampleFeed, as of 2026-10-23 [4]. 1m return 2.1%, 1y return 15.4%, max drawdown -38.5% (5y) [4].

## Risks (latest 10-K, Item 1A)

1. Customer concentration: top customers drive a large share of sales [2]
2. Supply chain dependence on a single foundry [2]

## Earnings releases (8-K, Item 2.02, EX-99.1)

- Q4 FY2025 release: revenue $3.4B, up 11% YoY ([8-K](https://www.sec.gov/Archives/edgar/data/999999/000099999925000200/ex99-1.htm))

## Insiders (12 months)

Open-market buy/sell (codes P/S): 0 purchases, 3 sales totalling $4.5M [5].
Other Form 4 codes (A, M, F) are listed in the footnote and are not open-market trades [5].

## Next earnings

Expected 2026-11-05 (UNVERIFIED date confirmation from company IR) [6]

## Sources

1. SEC company facts, https://data.sec.gov/submissions/CIK0000999999.json, retrieved 2026-10-24
2. 10-K accession 0000999999-25-000123, https://www.sec.gov/Archives/edgar/data/999999/000099999925000123/, filed 2025-08-01
3. Ratios computed from get_fundamentals values (SEC XBRL), https://data.sec.gov/api/xbrl/companyfacts/CIK0000999999.json, as of 2025-08-01
4. ExampleFeed price stats, https://example.com/prices/EXMPL, as of 2026-10-23
5. Form 4 filings, https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000999999&type=4, retrieved 2026-10-24
6. Earnings calendar, https://example.com/calendar/EXMPL, retrieved 2026-10-24

## Could not verify

- UNVERIFIED: employee count 12,000 (no XBRL value returned)

---
Research tooling output, not financial advice.
