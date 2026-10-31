-- PetroTwin M2 T12 reference rows; apply after 001_initial_schema.sql.
USE petrotwin;

INSERT IGNORE INTO fuel_product (product_id, canonical_name, default_unit) VALUES
  ('diesel_0005s_iv', 'Dầu DO 0,005S-IV', 'VND/liter'),
  ('diesel_005s_ii', 'Dầu DO 0,05S-II', 'VND/liter'),
  ('diesel_0001s_v', 'Dầu DO 0,001S-V', 'VND/liter'),
  ('fuel_oil_35s', 'Dầu FO 3,5%S', 'VND/liter'),
  ('fuel_oil_3s', 'Dầu FO 3%S', 'VND/liter'),
  ('kerosene_ko', 'Dầu KO', 'VND/liter'),
  ('gasoline_e5_ron92_ii', 'Xăng E5 RON 92-II', 'VND/liter'),
  ('gasoline_e10_ron95_iii', 'Xăng E10 RON 95-III', 'VND/liter'),
  ('gasoline_ron95_iii', 'Xăng RON 95-III', 'VND/liter'),
  ('gasoline_ron95_iv', 'Xăng RON 95-IV', 'VND/liter');

INSERT IGNORE INTO market_series (series_id, canonical_name, unit_code) VALUES
  ('brent_dated_usd_bbl', 'Brent dated crude oil spot price', 'USD/bbl'),
  ('usd_vnd_sbv_reference', 'SBV central USD/VND reference rate', 'VND/USD');
