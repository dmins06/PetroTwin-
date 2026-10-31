-- Read-only T13 reconciliation. Set @run_id to the run reported by current.json.
USE petrotwin;
SET @run_id = '28b22922e761cb08a8d8';

SELECT VERSION() AS mysql_version;
SELECT run_id, schema_version, pipeline_version, publication_status, created_at_utc
FROM dataset_run WHERE run_id = @run_id;

SELECT 'fuel_price_observation' AS table_name, COUNT(*) AS row_count
FROM fuel_price_observation WHERE run_id = @run_id
UNION ALL
SELECT 'market_observation', COUNT(*) FROM market_observation WHERE run_id = @run_id
UNION ALL
SELECT 'fuel_market_panel', COUNT(*) FROM fuel_market_panel WHERE run_id = @run_id
UNION ALL
SELECT 'fund_annotation', COUNT(*) FROM fund_annotation WHERE run_id = @run_id
UNION ALL
SELECT 'quarantine_record', COUNT(*) FROM quarantine_record WHERE run_id = @run_id;

SELECT observation_status, COUNT(*) AS row_count,
       SUM(value IS NULL) AS null_value_count
FROM market_observation WHERE run_id = @run_id
GROUP BY observation_status ORDER BY observation_status;

SELECT reason_code, COUNT(*) AS row_count
FROM quarantine_record WHERE run_id = @run_id
GROUP BY reason_code ORDER BY reason_code;

SELECT c.singleton_id, c.run_id AS current_run_id, c.updated_at_utc
FROM dataset_current_pointer c WHERE c.singleton_id = 1;

SELECT COUNT(*) AS panel_rows_with_future_or_same_day_features
FROM fuel_market_panel
WHERE run_id = @run_id AND (
  (brent_observation_date IS NOT NULL AND brent_observation_date >= DATE(effective_at_local))
  OR (fx_observation_date IS NOT NULL AND fx_observation_date >= DATE(effective_at_local))
);
