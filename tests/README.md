# Tests

- `unit/` covers cleaning rules, schema validation, features, models, metrics, and forecast-log behavior in isolation.
- `integration/` covers end-to-end pipeline stages and API routes using small synthetic fixtures.
- Keep tests deterministic, small, and independent of private or large datasets. Include time-order and data-leakage edge cases for forecasting logic.
