# Team data interface

This is the stable handoff contract for `petrotwin-data-v3`. The authoritative field definitions and cleaning rules are in [DATA_DICTIONARY.md](DATA_DICTIONARY.md).

The latest snapshot's acceptance findings are recorded in [data-foundation-acceptance.md](../reports/data-foundation-acceptance.md).

## How consumers find a dataset

1. Open [`data/processed/current.json`](../data/processed/current.json) and obtain `run_id` and relative `path` for the latest published snapshot.
2. Read data only from `data/processed/<path>/`; never depend on a hard-coded run ID.
3. Pin the `run_id` in experiment outputs so results can be reproduced. A run directory is immutable after publication.

To load the integrated panel from a checkout:

```python
import json
from pathlib import Path

import pandas as pd

root = Path(".")
current = json.loads((root / "data/processed/current.json").read_text(encoding="utf-8"))
run_dir = root / "data/processed" / current["path"]
panel = pd.read_csv(run_dir / "fuel_market_panel.csv")
print(current["run_id"], panel.shape)
```

On GitHub, open the pointer above, follow its `path` under `data/processed/`, then select the needed CSV or report. For an event-level modeling table, use `fuel_market_panel.csv`.

The run directory contains:

| File | Grain/key | Intended use |
|---|---|---|
| `market_observation.csv` | (`series_id`, `observation_date`, `source_id`, `snapshot_id`) | Clean source-level Brent and FX observations; one row per series/date. |
| `fuel_price_observation.csv` | (`product_id`, `effective_at_local`, `source_id`, `snapshot_id`) | Clean positive domestic fuel-price events. |
| `fuel_market_panel.csv` | (`product_id`, `effective_at_local`, `target_snapshot_id`) | Fuel events joined to the latest strictly prior observed Brent and FX rows. Use for event-level modeling. |
| `quarantine.csv` | source file and row number | Rejected records with reason and original row JSON. Audit only; never use as valid targets/features. |
| `quality_report.json` | one report per run | Source coverage, frequencies, missingness, counts, quarantine reasons and join availability. |
| `manifest.json` | one entry per source file plus output checksums | Source attribution, retrieval time, schema fingerprint, and SHA-256/row count for each published table. |

## Serialization rules

- UTF-8 CSV with a header row. Dates use `YYYY-MM-DD`; timestamps are ISO 8601 with offsets. Fuel effective timestamps carry `+07:00`; extraction timestamps are UTC and end in `Z`.
- Decimal values are serialized without thousands separators. Blank CSV cells represent NULL. Do not interpret blanks as zero.
- Boolean cells are lowercase `true` or `false`.
- `unit_code` values are exactly `USD/bbl`, `VND/USD`, and `VND/liter`, matched to their series/table. The panel also has explicit per-feature unit columns.
- Do not rename, reorder, drop, or reinterpret v3 columns locally. If an interface change is needed, coordinate it, increment the schema version, update parsers/consumers, and publish a new run.
- Stabilization-fund fields are maintained in `data/manual/stabilization_fund_annotations.csv`, keyed by product and source record date. Amounts are VND/liter, except mazut which is VND/kg. `stabilization_fund_subsidy` is derived as `1` when the confirmed amount is positive and `0` when the official source confirms no spending. Blank values mean the period has not been verified. PVOIL does not provide this information.
- Join Brent/FX to fuel only using the selected observation date and snapshot IDs in `fuel_market_panel.csv`. The selected date must be strictly earlier than the event's local calendar date. NULL market features mean no earlier observed value was available in the source snapshot.

## Product IDs

| `product_id` | Source product label |
|---|---|
| `diesel_0005s_iv` | Dầu DO 0,005S-IV |
| `diesel_005s_ii` | Dầu DO 0,05S-II |
| `diesel_0001s_v` | Dầu DO 0,001S-V |
| `fuel_oil_35s` | Dầu FO 3,5%S |
| `fuel_oil_3s` | Dầu FO 3%S |
| `kerosene_ko` | Dầu KO |
| `gasoline_e5_ron92_ii` | Xăng E5 RON 92-II |
| `gasoline_e10_ron95_iii` | Xăng E10 RON 95-III |
| `gasoline_ron95_iii` | Xăng RON 95-III |
| `gasoline_ron95_iv` | Xăng RON 95-IV |

Product labels are not interchangeable. Do not fill a missing grade from another grade. `fuel_oil_3s` currently has no valid published target rows because all its source prices are zero and quarantined.
