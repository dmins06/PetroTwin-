# Processed data

The data pipeline publishes validated, content-addressed run directories under `runs/<run_id>/` and atomically updates `current.json`. Each run includes canonical tables, quarantine, a quality report, a source/output manifest, and a persistent execution log is kept in `data/interim/logs/pipeline.log`.

Reviewed handoff snapshots can be committed to Git; new generated outputs are ignored by default and should be added explicitly when they are accepted for sharing. Build from the raw snapshots with `PYTHONPATH=src python -m petrotwin.data.pipeline`.
