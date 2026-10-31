# Data preparation and quality gate

Run `01_preprocessing_quality_audit.ipynb` before the T10/T11 EDA notebook.

The existing `petrotwin.data.pipeline` owns raw-to-canonical cleaning. The audit notebook reads the raw source CSVs, current accepted run, manifest, quality report, and quarantine; it profiles and visualizes them without modifying those files. Review missingness, keys, distributions, outliers, calendar coverage, row reconciliation, checksums, and known warnings before starting EDA.

Run from the repository checkout with the `eda` optional dependencies installed. A FAIL blocks the EDA handoff. A WARN is a known limitation to document. Statistical outlier flags are review prompts only; do not remove or cap values from them automatically.
