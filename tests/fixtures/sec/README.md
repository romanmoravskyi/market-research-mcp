# SEC EDGAR test fixtures

These files are **real SEC EDGAR responses**, recorded on 2026-10-24 by
`scripts/record_sec_fixtures.py`. SEC EDGAR data is a US government work in the public domain.
Every file is gzip-compressed (`*.json.gz`, `*.xml.gz`) to keep the repo small.

- `company_tickers_exchange.json.gz`: `https://www.sec.gov/files/company_tickers_exchange.json`
- `CIK##########.json.gz`: `https://data.sec.gov/submissions/CIK##########.json`
  (MSFT, JPM, O, BRK-B)
- `CIK##########-submissions-001.json.gz`: the first older page listed in `filings.files`
- `atom-CIK0000019617-10-K.xml.gz`: the EDGAR company browse Atom feed of JPM 10-K filings,
  `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000019617&type=10-K&dateb=&owner=include&count=40&output=atom`
  (raw XML, recorded 2026-10-25; `--atom-only` re-records just this feed), and
  `atom-CIK0000019617-10-K-start40.xml.gz`, its second (last, short) page with `&start=40`
- `companyfacts/CIK##########.json.gz`: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`

Tests read them with `load_sec_fixture(name)` from `tests/conftest.py`, which accepts the name
with or without `.gz`. Tests that assert exact values (accession numbers, dates) refer to filings
in this recording. Re-recording adds newer filings and can move the boundary between the
`recent` block and the older pages, so check `tests/test_edgar.py` after re-recording.

## Re-recording

SEC requires a User-Agent with a name and email. The script reads it from the environment only
and never writes it to disk or prints it.

```powershell
$env:SEC_USER_AGENT = "Your Name your.email@example.com"
uv run python scripts/record_sec_fixtures.py
```

No personal data and no User-Agent values are stored here; `test_fixtures_have_no_user_agent`
checks the submissions and tickers files, and
`test_atom_fixtures_have_no_user_agent` checks the Atom feeds.
