# Canonical data schema and cleaning rules

This contract is `petrotwin-data-v3`. Raw files are immutable. Parse each source into a source-specific staging table, validate it, then publish canonical tables. Store rejected rows and a reason code in an interim quarantine output. Row-level exceptions may be quarantined while publishing the remaining validated rows only when all batch-level gates pass; a batch-level failure must not replace any part of the last valid processed snapshot.

## Canonical tables

Keep market series and domestic fuel prices in separate tables. Do not put USD/barrel, VND/USD, and VND/liter into one unqualified value column.

### `market_observation`

One row per market series and observation date (and source vintage, if the provider supplies revisions).

| Column | Type | Rule |
|---|---|---|
| `series_id` | string | Controlled ID: `brent_dated_usd_bbl` or `usd_vnd_sbv_reference`. |
| `observation_date` | DATE | Source observation date. It is a calendar date, not an invented midnight timestamp. |
| `value` | DECIMAL | Parsed numeric value; NULL only when `observation_status='missing'`. |
| `unit_code` | enum | Brent: `USD/bbl`; FX: `VND/USD`. Must match `series_id`. |
| `observation_status` | enum | `observed` or `missing`; a missing row must have a NULL value. |
| `source_id` | string | Stable source identifier, e.g. `eia_fred` or `sbv_reference_via_algolab`. |
| `snapshot_id` | string | Non-null ID of the immutable ingested file; joins to the manifest. |
| `source_value` | string, nullable | Original displayed source value, retained for audit. |
| `source_url` | string | URL recorded in the source snapshot. |
| `retrieved_at_utc` | TIMESTAMP WITH TIME ZONE | UTC extraction time, parsed from `extracted_at_utc`. |
| `schema_version` | string | `petrotwin-data-v3`. |

Primary key in ingestion history: (`series_id`, `observation_date`, `source_id`, `snapshot_id`). The current published view selects one validated snapshot per source. Do not combine snapshots unless revisions are intentionally being modeled.

### `fuel_price_observation`

One row per fuel product and effective instant. A price adjustment is an event; do not expand it into daily rows during cleaning.

| Column | Type | Rule |
|---|---|---|
| `product_id` | string | Controlled ID mapped from source label; unknown labels are quarantined. |
| `source_product_name` | string | Exact original product label. |
| `source_record_date` | DATE | Original `date` column, retained without assigning announcement semantics that the source has not verified. |
| `effective_at_local` | TIMESTAMP WITH TIME ZONE | Effective instant in `Asia/Ho_Chi_Minh`; serialized with the `+07:00` offset. Derive only from the source effective date/time using the rule below. |
| `effective_time_24_00_normalized` | boolean | True when source `24:00` was rolled to next-day `00:00`; source date/time remain in raw snapshot. |
| `price_value` | DECIMAL | Domestic price; must be positive for a publishable price observation. |
| `unit_code` | enum | `VND/liter` for the current products. Never combine with market units. |
| `price_change_value` | DECIMAL, nullable | Signed source change, if provided. NULL is allowed when not reported (including the first observation); do not infer it during ingestion. |
| `stabilization_fund_subsidy` | integer, nullable | `1` if the official record confirms fund spending for that product and period; `0` if it confirms no spending; NULL means not researched or not verified. |
| `stabilization_fund_subsidy_amount` | integer, nullable | Confirmed fund spending per unit for that product and period. NULL means not researched or not verified. |
| `stabilization_fund_subsidy_unit` | string | Unit for the fund spending amount: `VND/liter` or `VND/kg`. |
| `stabilization_fund_source_url` | string, nullable | Official MOIT source supporting the annotation. |
| `tax_included` | boolean, nullable | Parse source field; do not default when absent or malformed. |
| `source_id` | string | `pvoil` for the current snapshot. |
| `snapshot_id` | string | Non-null ID of the immutable ingested file; joins to the manifest. |
| `source_url` | string | URL recorded in the source snapshot. |
| `retrieved_at_utc` | TIMESTAMP WITH TIME ZONE | UTC extraction time. |
| `schema_version` | string | `petrotwin-data-v3`. |

Primary key in ingestion history: (`product_id`, `effective_at_local`, `source_id`, `snapshot_id`). The current published view selects one validated snapshot per source. Do not use `source_record_date` as the event key: it may differ from the effective date.

### `fuel_market_panel`

One row per validated fuel-price event, created by joining the two canonical market series as features. It carries the target event fields plus Brent and FX values, their source observation dates, and their age in calendar days. Keep separate feature columns and units; never sum or compare the two market values directly.

| Column | Type | Rule |
|---|---|---|
| `product_id` | string | Controlled fuel product ID. |
| `source_product_name` | string | Original fuel label. |
| `source_record_date` | DATE | Original source `date`; no announcement meaning is implied. |
| `effective_at_local` | TIMESTAMP WITH TIME ZONE | Fuel event time, `Asia/Ho_Chi_Minh`, serialized with `+07:00`. |
| `price_value` | DECIMAL | Target price, `VND/liter`; positive and non-NULL. |
| `price_change_value` | DECIMAL, nullable | Reported signed change; blank if source did not provide it. |
| `stabilization_fund_subsidy` | integer, nullable | Verified `0`/`1` label copied from the fuel price observation; blank means not researched or not verified. |
| `stabilization_fund_subsidy_amount` | integer, nullable | Confirmed fund spending per unit, copied from the fuel price observation. |
| `stabilization_fund_subsidy_unit` | string | Unit for the amount: `VND/liter` or `VND/kg`. |
| `stabilization_fund_source_url` | string, nullable | Official MOIT source supporting the annotation. |
| `unit_code` | enum | Always `VND/liter` for current fuel targets. |
| `target_source_id`, `target_snapshot_id` | string | Identifies the source event and raw snapshot. |
| `brent_source_id`, `brent_snapshot_id` | string | Provenance for the selected Brent feature; blank when no prior observation exists. |
| `brent_observation_date` | DATE, nullable | Selected Brent source date, strictly before event date. |
| `brent_usd_per_barrel` | DECIMAL, nullable | Selected Brent value, `USD/bbl`. |
| `brent_age_days` | integer, nullable | Calendar days from selected Brent date to fuel event date; at least 1. |
| `brent_usd_per_barrel_unit` | enum | Always `USD/bbl`. |
| `fx_source_id`, `fx_snapshot_id` | string | Provenance for the selected FX feature; blank when no prior observation exists. |
| `fx_observation_date` | DATE, nullable | Selected FX source date, strictly before event date. |
| `usd_vnd_rate` | DECIMAL, nullable | Selected FX value, `VND/USD`. |
| `fx_age_days` | integer, nullable | Calendar days from selected FX date to fuel event date; at least 1. |
| `usd_vnd_rate_unit` | enum | Always `VND/USD`. |
| `schema_version` | string | `petrotwin-data-v3`. |

Panel key: (`product_id`, `effective_at_local`, `target_snapshot_id`). Use this table for event-level model input; use the two canonical source tables for source-specific analysis.

Join rule: for a fuel event on local date `D`, select the latest `observation_date < D` with `observation_status='observed'` for each market series. This strict prior-day rule avoids look-ahead when publication times are unknown. Missing prior market history remains NULL and is counted in the quality report. The joined observation date, source/snapshot IDs, and age are mandatory audit columns. This is a point-in-time join in the derived panel; it does not write imputed values back to either canonical source table.

### Provenance and ingestion metadata

Store one manifest row per ingested file: `snapshot_id`, source ID, source URL, publisher/attribution, license/reuse terms (or `unknown`), retrieval timestamp UTC, file checksum, raw filename, row count, and detected source schema fingerprint. The current raw files do not establish license terms; record them as unknown until verified. Keep original fields in staging or the immutable raw file even when they are not promoted to canonical columns.

## Timestamp rules

- Parse market `date` and fuel source `date`/`effective_date` as strict ISO `YYYY-MM-DD` calendar dates. Reject invalid dates and ambiguous locale formats; never parse them as UTC instants.
- Interpret `extracted_at_utc` as a timezone-aware UTC instant. Reject missing offsets or invalid values; never convert it to local time for storage.
- Interpret fuel `effective_time` in `Asia/Ho_Chi_Minh`. Accept `HH:MM` from `00:00` through `23:59` and the explicit special value `24:00` only. Convert `effective_date + 24:00` to midnight at the start of the next local date. Preserve the original date/time in staging and record a normalization flag. Do not accept `24:01` or silently roll any other invalid time.
- Do not assume the source `date` is an announcement date. Promote it only as `source_record_date` unless the publisher documents its meaning.
- Comparisons and joins use event dates/instants, not extraction timestamps. Market observations remain on their source calendar dates; do not shift them to match fuel events.

## Keys, duplicates, and revisions

- Market natural key within one snapshot: (`series_id`, `observation_date`, `source_id`, `snapshot_id`). Fuel natural key within one snapshot: (`product_id`, `effective_at_local`, `source_id`, `snapshot_id`).
- An exact repeated row within the same snapshot may be collapsed to one row and counted in the batch report. Same key with differing values or attributes is a conflict: fail that source batch and quarantine the conflicting rows. Never use arbitrary `first`, `last`, or mean aggregation.
- Across snapshots, retain both versions in ingestion history. A later value replaces the published current value only when the source identifies a revision or the refresh policy explicitly designates a complete replacement snapshot; record the prior and new checksums.
- Reject missing key components and unmapped product/series IDs. Do not manufacture keys from row order.
- Missing keys, conflicting duplicates, or absent required schema columns are batch-level failures. Invalid individual values (for example, the known FO zero prices) are row-level rejects unless they indicate a broader source or parser failure.

## Units and value parsing

- Parse numeric source strings using source-specific rules, then retain the original text. Decimal point is `.` in machine numeric fields. For displayed Vietnamese values, remove a thousands separator only when the source format is known (for example `17.730 đ` means 17730 VND/liter); reject ambiguous strings.
- Required mappings: Brent `price_usd_per_barrel` → `USD/bbl`; FX `rate_vnd_per_usd` → `VND/USD`; domestic `price_vnd_per_liter` → `VND/liter`.
- Check the source `unit` field against the mapping. A mismatch fails validation; do not convert or relabel silently. Any future conversion must be explicit, versioned, and preserve the source value and unit.
- FX weekend values are retained as source observations. Do not forward-fill or classify them as duplicates solely because their numeric value repeats.

## Missing values and exceptions

- Missing market values are allowed only for Brent rows explicitly marked `observation_status='missing'`; preserve the date/status and NULL value. A null marked `observed`, or a missing value without a status, fails validation.
- Missing domestic price, product, effective date/time, unit, or provenance key is a batch error. Missing `price_change_value` is allowed and remains NULL; it is not the target price.
- The 64 current zero-price FO rows are exceptions, not valid prices. Quarantine them pending confirmation from PVOIL; do not change zero to NULL in place, impute, or include them in modeling. A zero price for any other domestic product also fails validation pending source review.
- Record gaps rather than filling them. A market calendar audit flags absent weekdays but treats weekends/holidays according to the source calendar. Domestic prices are event-based and irregular; do not require a daily row or infer a missing adjustment from cadence.
- Keep product history sparse where grades were introduced or discontinued. In particular, do not infer E10 from RON95 or carry a discontinued grade forward as a new event.

## Validation gates

Run these checks before publishing processed data:

1. Required columns exist; no unexpected type coercion; source schema fingerprint is recorded. Source headers must match the pinned v1 schema, including names and order. Any added, removed, renamed, or reordered header fails the batch until the parser and schema version are explicitly updated.
2. Dates, timestamps, numbers, booleans, and status enums parse under the source-specific parser. Report invalid and null counts by column.
3. Keys are complete and unique after exact duplicate handling. Conflicting duplicates fail the batch.
4. Units match the series/product mapping. Market values must be positive when observed; domestic publishable prices must be positive. Keep signed `price_change_value` unconstrained except for numeric parsing.
5. Missingness follows the rules above. No silent imputation, forward fill, or zero substitution in raw-to-processed cleaning.
6. Effective timestamps are strictly chronological within each product after sorting. Do not require every product to appear on every adjustment date.
7. Report coverage, first/last observation, distinct dates, rows per date/product, interval distribution, weekday gaps, missing values, duplicate counts, out-of-range/zero values, and excluded/quarantined row counts. Frequency and gaps are diagnostics, not automatic failure, unless they violate a documented source calendar.
8. Batch publication is atomic: row-level rejects are written to quarantine and excluded from the new validated snapshot; publish that snapshot only if every batch-level gate passes. If a batch-level gate fails, write diagnostics/quarantine output and leave the last valid processed snapshot unchanged.
9. Fund annotations must have unique (`product_id`, `source_record_date`) keys, a known product, a valid ISO date, a non-negative integer amount, a product-compatible unit, and an HTTPS evidence URL. Each annotation must match exactly one valid fuel event; orphan or ambiguous annotations fail the batch. Amount `0` means verified no spending, a positive amount means verified spending, and no annotation leaves the output NULL/unverified.

## Current source assessment and unresolved decisions

The current files report Brent as EIA data distributed by FRED, FX as SBV central reference rates attributed through Algolab, and domestic prices from PVOIL. The PVOIL page was not independently retrievable during the initial audit, and the FX file uses a secondary source. Record these source confidence limits in the manifest; verify source licenses and confirm zero-price FO semantics before declaring the processed snapshot production-ready.
