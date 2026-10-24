# FRED fixtures

These files are **synthetic**: hand-written in the official FRED API JSON shape
(`fred/series` and `fred/series/observations`, `file_type=json`, `sort_order=desc`).
The values are made up and must not be used as data.

Files:
- `<ID>_series.json` and `<ID>_observations.json` for DGS10 and CPIAUCSL
  (each observations file contains one `"."` missing value).
- `error_unknown_series.json`: the HTTP 400 body FRED returns for an unknown series id.
- `error_bad_api_key.json`: the HTTP 400 body FRED returns for an unregistered `api_key`.

To replace them with real recordings (needs `FRED_API_KEY`):

    uv run python scripts/record_fred_fixtures.py

The script stores response bodies only and strips the API key from anything it writes.
Note: `tests/test_fred.py` asserts on the synthetic values (including the `"."` entries),
so update those assertions after re-recording.
