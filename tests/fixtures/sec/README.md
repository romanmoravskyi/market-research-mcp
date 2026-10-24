# SEC EDGAR test fixtures

These files are **hand-trimmed** samples in the exact shape of the official SEC responses:

- `company_tickers_exchange.json`: `https://www.sec.gov/files/company_tickers_exchange.json`
- `CIK##########.json`: `https://data.sec.gov/submissions/CIK##########.json`
- `CIK##########-submissions-NNN.json`: older pages listed in `filings.files`

Values (accession numbers, dates, document names) are realistic but not guaranteed to match
EDGAR exactly. They will be replaced by recorded data via `scripts/record_sec_fixtures.py`,
which also writes gzip-compressed company facts to `companyfacts/`.

No personal data and no User-Agent values are stored here.
