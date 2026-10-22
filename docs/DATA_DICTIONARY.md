# Data dictionary template

Create one entry per source table. Do not guess values that have not been verified.

| Field | Description |
|---|---|
| Dataset name | Stable name and version |
| Source and license | URL, publisher, and reuse terms |
| Retrieved at | Timestamp with timezone |
| Coverage | First and last observation dates |
| Grain/key | For example, one row per fuel grade and effective date |
| Columns and units | Meaning, type, and unit for each field |
| Known issues | Missing periods, revisions, duplicates, or policy changes |
| Processing | Cleaning rules and code/config version |

For fuel prices, distinguish fuel grade, announcement date, effective date, and price unit whenever the source provides them. Keep market observation dates separate from domestic adjustment dates.
