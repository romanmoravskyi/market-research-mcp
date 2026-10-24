# Finnhub fixtures (SYNTHETIC)

Finnhub's terms do not allow redistributing its data. These files copy only the JSON
*shape* of `GET /api/v1/calendar/earnings?from=&to=&symbol=` (checked live on 2026-10-24);
every date and number is made up.

- `msft_earnings.json`: two upcoming rows. The real response also carries `epsEstimate` and
  `revenueEstimate`; the tool ignores them because they are forecasts.
- `empty.json`: no earnings in the window.

There is no field saying whether a date is confirmed. Never replace these with real responses.
