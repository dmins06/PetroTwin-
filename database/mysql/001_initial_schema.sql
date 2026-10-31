-- PetroTwin M2 T12 initial schema; target MySQL 26.7 / InnoDB.
CREATE DATABASE IF NOT EXISTS petrotwin CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE petrotwin;

CREATE TABLE dataset_run (
  run_id VARCHAR(64) PRIMARY KEY,
  schema_version VARCHAR(64) NOT NULL,
  pipeline_version VARCHAR(32) NOT NULL,
  pipeline_sha256 CHAR(64) NOT NULL,
  publication_status VARCHAR(40) NOT NULL,
  started_at_utc DATETIME(6) NULL,
  completed_at_utc DATETIME(6) NULL,
  manifest_json JSON NOT NULL,
  created_at_utc DATETIME(6) NOT NULL,
  CHECK (publication_status IN ('building','validated','validated_with_quarantine','failed'))
) ENGINE=InnoDB;

CREATE TABLE source_snapshot (
  snapshot_id CHAR(64) PRIMARY KEY,
  source_id VARCHAR(100) NOT NULL,
  raw_filename VARCHAR(255) NOT NULL,
  source_url TEXT NOT NULL,
  publisher_or_attribution TEXT NULL,
  license_reuse_terms TEXT NULL,
  sha256 CHAR(64) NOT NULL,
  schema_fingerprint_sha256 CHAR(64) NOT NULL,
  source_row_count INT UNSIGNED NOT NULL,
  retrieval_times_utc JSON NOT NULL,
  retrieved_at_utc_min DATETIME(6) NOT NULL,
  retrieved_at_utc_max DATETIME(6) NOT NULL,
  UNIQUE KEY uq_source_snapshot_hash (source_id, sha256),
  UNIQUE KEY uq_snapshot_source (snapshot_id, source_id),
  KEY ix_source_retrieved (source_id, retrieved_at_utc_max),
  CHECK (retrieved_at_utc_max >= retrieved_at_utc_min)
) ENGINE=InnoDB;

CREATE TABLE dataset_run_source (
  run_id VARCHAR(64) NOT NULL,
  snapshot_id CHAR(64) NOT NULL,
  PRIMARY KEY (run_id, snapshot_id),
  FOREIGN KEY (run_id) REFERENCES dataset_run(run_id),
  FOREIGN KEY (snapshot_id) REFERENCES source_snapshot(snapshot_id)
) ENGINE=InnoDB;

CREATE TABLE fuel_product (
  product_id VARCHAR(64) PRIMARY KEY,
  canonical_name VARCHAR(150) NOT NULL,
  default_unit VARCHAR(32) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE market_series (
  series_id VARCHAR(100) PRIMARY KEY,
  canonical_name VARCHAR(150) NOT NULL,
  unit_code VARCHAR(32) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE market_observation (
  run_id VARCHAR(64) NOT NULL,
  series_id VARCHAR(100) NOT NULL,
  observation_date DATE NOT NULL,
  snapshot_id CHAR(64) NOT NULL,
  value DECIMAL(20,8) NULL,
  unit_code VARCHAR(32) NOT NULL,
  observation_status VARCHAR(24) NOT NULL,
  source_value DECIMAL(20,8) NULL,
  source_url TEXT NULL,
  retrieved_at_utc DATETIME(6) NULL,
  PRIMARY KEY (run_id, series_id, observation_date, snapshot_id),
  FOREIGN KEY (series_id) REFERENCES market_series(series_id),
  CONSTRAINT fk_market_observation_run_source
    FOREIGN KEY (run_id, snapshot_id) REFERENCES dataset_run_source(run_id, snapshot_id),
  CHECK (observation_status IN ('observed','missing'))
) ENGINE=InnoDB;

CREATE TABLE fuel_price_observation (
  run_id VARCHAR(64) NOT NULL,
  product_id VARCHAR(64) NOT NULL,
  source_record_date DATE NOT NULL,
  effective_at_local DATETIME NOT NULL,
  local_timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
  effective_time_24_00_normalized BOOLEAN NULL,
  snapshot_id CHAR(64) NOT NULL,
  source_product_name VARCHAR(255) NOT NULL,
  price_value DECIMAL(18,4) NULL,
  unit_code VARCHAR(32) NOT NULL,
  price_change_value DECIMAL(18,4) NULL,
  stabilization_fund_subsidy TINYINT UNSIGNED NULL,
  stabilization_fund_subsidy_amount DECIMAL(18,4) NULL,
  stabilization_fund_subsidy_unit VARCHAR(32) NULL,
  stabilization_fund_source_url TEXT NULL,
  tax_included BOOLEAN NULL,
  source_url TEXT NULL,
  retrieved_at_utc DATETIME(6) NULL,
  PRIMARY KEY (run_id, product_id, effective_at_local, snapshot_id),
  FOREIGN KEY (product_id) REFERENCES fuel_product(product_id),
  CONSTRAINT fk_fuel_price_run_source
    FOREIGN KEY (run_id, snapshot_id) REFERENCES dataset_run_source(run_id, snapshot_id),
  CHECK (stabilization_fund_subsidy IS NULL OR stabilization_fund_subsidy IN (0, 1))
) ENGINE=InnoDB;

CREATE TABLE fund_annotation (
  run_id VARCHAR(64) NOT NULL,
  product_id VARCHAR(64) NOT NULL,
  source_record_date DATE NOT NULL,
  snapshot_id CHAR(64) NOT NULL,
  fund_amount DECIMAL(18,4) NOT NULL,
  unit_code VARCHAR(32) NOT NULL,
  evidence_url TEXT NOT NULL,
  PRIMARY KEY (run_id, product_id, source_record_date),
  FOREIGN KEY (product_id) REFERENCES fuel_product(product_id),
  CONSTRAINT fk_fund_annotation_run_source
    FOREIGN KEY (run_id, snapshot_id) REFERENCES dataset_run_source(run_id, snapshot_id),
  CHECK (fund_amount >= 0)
) ENGINE=InnoDB;

CREATE TABLE fuel_market_panel (
  run_id VARCHAR(64) NOT NULL,
  product_id VARCHAR(64) NOT NULL,
  effective_at_local DATETIME NOT NULL,
  local_timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
  target_source_id VARCHAR(100) NOT NULL,
  target_snapshot_id CHAR(64) NOT NULL,
  source_record_date DATE NOT NULL,
  price_value DECIMAL(18,4) NULL,
  price_change_value DECIMAL(18,4) NULL,
  unit_code VARCHAR(32) NOT NULL,
  stabilization_fund_subsidy TINYINT UNSIGNED NULL,
  stabilization_fund_subsidy_amount DECIMAL(18,4) NULL,
  stabilization_fund_subsidy_unit VARCHAR(32) NULL,
  stabilization_fund_source_url TEXT NULL,
  brent_source_id VARCHAR(100) NOT NULL,
  brent_snapshot_id CHAR(64) NULL,
  brent_observation_date DATE NULL,
  brent_usd_per_barrel DECIMAL(20,8) NULL,
  brent_age_days INT NULL,
  brent_unit VARCHAR(32) NOT NULL,
  fx_source_id VARCHAR(100) NOT NULL,
  fx_snapshot_id CHAR(64) NULL,
  fx_observation_date DATE NULL,
  usd_vnd_rate DECIMAL(20,8) NULL,
  fx_age_days INT NULL,
  fx_unit VARCHAR(32) NOT NULL,
  PRIMARY KEY (run_id, product_id, effective_at_local, target_snapshot_id),
  FOREIGN KEY (product_id) REFERENCES fuel_product(product_id),
  CONSTRAINT fk_panel_target_run_source
    FOREIGN KEY (run_id, target_snapshot_id) REFERENCES dataset_run_source(run_id, snapshot_id),
  CONSTRAINT fk_panel_brent_run_source
    FOREIGN KEY (run_id, brent_snapshot_id) REFERENCES dataset_run_source(run_id, snapshot_id),
  CONSTRAINT fk_panel_fx_run_source
    FOREIGN KEY (run_id, fx_snapshot_id) REFERENCES dataset_run_source(run_id, snapshot_id),
  CHECK (stabilization_fund_subsidy IS NULL OR stabilization_fund_subsidy IN (0, 1)),
  CHECK (brent_age_days IS NULL OR brent_age_days >= 0),
  CHECK (fx_age_days IS NULL OR fx_age_days >= 0)
) ENGINE=InnoDB;

CREATE TABLE quarantine_record (
  run_id VARCHAR(64) NOT NULL,
  record_number INT UNSIGNED NOT NULL,
  source_id VARCHAR(100) NOT NULL,
  snapshot_id CHAR(64) NOT NULL,
  reason_code VARCHAR(80) NOT NULL,
  reason_detail TEXT NULL,
  raw_row_json JSON NOT NULL,
  PRIMARY KEY (run_id, source_id, record_number),
  CONSTRAINT fk_quarantine_run_source
    FOREIGN KEY (run_id, snapshot_id) REFERENCES dataset_run_source(run_id, snapshot_id),
  CONSTRAINT fk_quarantine_source_id
    FOREIGN KEY (snapshot_id, source_id) REFERENCES source_snapshot(snapshot_id, source_id)
) ENGINE=InnoDB;

CREATE TABLE quality_report (
  run_id VARCHAR(64) PRIMARY KEY,
  report_json JSON NOT NULL,
  FOREIGN KEY (run_id) REFERENCES dataset_run(run_id)
) ENGINE=InnoDB;

CREATE TABLE dataset_current_pointer (
  singleton_id TINYINT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL,
  updated_at_utc DATETIME(6) NOT NULL,
  CHECK (singleton_id = 1),
  FOREIGN KEY (run_id) REFERENCES dataset_run(run_id)
) ENGINE=InnoDB;

CREATE VIEW v_current_fuel_market_panel AS
SELECT p.* FROM fuel_market_panel p
JOIN dataset_current_pointer c ON c.singleton_id = 1 AND c.run_id = p.run_id;

CREATE VIEW v_current_market_observation AS
SELECT o.* FROM market_observation o
JOIN dataset_current_pointer c ON c.singleton_id = 1 AND c.run_id = o.run_id;

CREATE VIEW v_current_fuel_price_observation AS
SELECT o.* FROM fuel_price_observation o
JOIN dataset_current_pointer c ON c.singleton_id = 1 AND c.run_id = o.run_id;
