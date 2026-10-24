# Tiingo fixtures (SYNTHETIC)

Every price, volume and date pattern in this folder is **made up**. Tiingo's terms do not
allow redistributing its data, so these files copy only the JSON *shape* of Tiingo's
responses:

- `msft_prices.json.gz`: `/tiingo/daily/msft/prices` (list of daily bars, gzipped), about six
  years of weekdays with one synthetic 4:1 split on 2023-03-15.
- `msft_meta.json`: `/tiingo/daily/msft` metadata.

Regenerate deterministically with `uv run python tests/fixtures/tiingo/make_synthetic.py`.
Never replace these with real Tiingo responses.
