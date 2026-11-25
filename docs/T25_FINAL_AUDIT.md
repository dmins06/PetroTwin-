# T25 Final Audit – Benchmark Integrity & Full Regression Verification

> **Branch**: `main` (chưa commit, chưa push)  
> **Snapshot đối chiếu**: `run_id = 28b22922e761cb08a8d8` (`data/processed/current.json`, 1.390 dòng panel, 516 quan sát Validation `2024-01-01` → `2025-12-31`)  
> **Artifact đối chiếu**: `artifacts/benchmarks/28b22922e761cb08a8d8/` (11 tệp: 10 CSV + `metadata.json`, nằm ngoài Git theo `.gitignore`)

---

## 1. Tổng quan Tình trạng T25

T25 đã được kiểm tra toàn diện về tính toàn vẹn dữ liệu, chống rò rỉ dữ liệu (anti-leakage), tính chính xác của phép tính metric, độ khớp giữa CSV/JSON/Markdown, khả năng tái lập CLI, và toàn bộ regression suite (**250 tests**). Trong quá trình audit đã phát hiện và bổ sung ngay các trường metadata còn thiếu (`generated_at_utc`, `split_strategy`, `input_data_summary`) vào `metadata.json` của gói benchmark.

---

## 2. Nguyên nhân Chính xác của Chênh lệch 516 / 506

Truy vết trực tiếp trên `oof_predictions_expanding.csv` (`516` dòng cho mỗi baseline) xác nhận **không có dữ liệu bị thiếu, không có dòng `unclassified`, và không có overlap giữa các nhóm**:

- **10 quan sát còn lại** là nhóm **`9d+ (extended_cycle)`** — đã có sẵn trong CSV (`error_by_interval_horizon.csv`) và trong bảng mục 2.2 của [`docs/T25_Baseline_Benchmark_and_Error_Analysis.md`](T25_Baseline_Benchmark_and_Error_Analysis.md), chỉ bị bỏ sót khi tóm tắt ngắn trong tin nhắn chat trước đó.
- Danh sách chính xác **10 quan sát `9d+ (extended_cycle)`** (đều thuộc `expanding_fold_3`, phát sinh do dời lịch điều hành qua hai kỳ nghỉ lễ lớn):
  - **`2025-02-01`** (`days_since_previous_event = 9.0` ngày — sau kỳ nghỉ Tết Nguyên Đán 2025): `diesel_0001s_v`, `diesel_005s_ii`, `kerosene_ko`, `gasoline_e5_ron92_ii`, `gasoline_ron95_iii` (5 quan sát).
  - **`2025-05-05`** (`days_since_previous_event = 11.0` ngày — sau kỳ nghỉ lễ 30/4 – 1/5/2025): 5 sản phẩm tương tự (5 quan sát).

---

## 3. Bảng Đối chiếu Số lượng Quan sát (Mutual Exclusivity & Completeness)

| Chiều phân nhóm | Các nhóm loại trừ lẫn nhau | Số lượng | Tổng khớp | Ghi chú |
|---|---|---:|---:|---|
| **Theo Fold (T21)** | `expanding_fold_1` (105) + `fold_2` (118) + `fold_3` (130) + `fold_4` (163) | 105 + 118 + 130 + 163 | **516 / 516** | Khớp 100% `configs/validation.yaml` |
| **Theo Khoảng chu kỳ (`h=1`)** | `6-8d` (483) + `1-5d` (20) + `9d+` (10) + `unknown (cold_start)` (3) | 483 + 20 + 10 + 3 | **516 / 516** | 0 overlap, 0 `unclassified` |
| **Theo Quý (`year_quarter`)** | `24Q1`(52)+`24Q2`(53)+`24Q3`(53)+`24Q4`(65)+`25Q1`(65)+`25Q2`(65)+`25Q3`(79)+`25Q4`(84) | 8 quý | **516 / 516** | Khớp 100% |
| **Theo Cold-start & Dir. Acc** | Có lịch sử sản phẩm (`directional_valid_n = 513`) + Cold-start (`used_fallback = 3`) | 513 + 3 | **516 / 516** | 3 dòng cold-start: `fuel_oil_35s` (`2024-05-30`), `diesel_0001s_v` (`2024-09-26`), `gasoline_e10_ron95_iii` (`2025-07-31`) |
| **Nhóm COMECO Active 4** | Tập con 4 mặt hàng đang thu thập liên tục (`directional_valid_n = 301`, 2 cold-start) | 303 | **303 / 303** | Khớp 100% `comeco_active_summary.csv` |

---

## 4. Kết quả Xác minh Metric, Anti-Leakage & Hiện tượng Trùng lặp 99,03%

- **Cùng tập Validation & Split T21**: Cả `NaivePersistenceBaseline` và `SeasonalNaiveBaseline(7D)` chạy trên cùng 4 folds Expanding và 4 folds Rolling từ `configs/validation.yaml`; **Final Test 2026 giữ khóa (`final_test_locked: true`)**.
- **Không rò rỉ dữ liệu vào `fit()` / `_global_median`**: `evaluate_baselines_on_t21()` fit mô hình thuần trên `split.train_df` và chỉ truyền `split.eval_df` qua `additional_history` khi `predict()` với lọc `effective_at_local <= target_effective_at_local - 1s`.
- **Tách biệt Directional Accuracy & MAE/RMSE/MAPE**:
  - `y_prev` gắn `NaN` cho 3 dòng cold-start (`used_fallback == True`) nên `directional_valid_n = 513` (loại đúng 3 dòng cold-start).
  - `MAE` (`374.6027`), `RMSE` (`499.6872`), `MAPE` (`1.9030%`) giữ nguyên `valid_n = 516` và đúng đơn vị `VND/liter`, `mape_excluded_zero_count = 0`.
  - Phân biệt rõ **Mean Fold MAE** (`379.9095 ± 38.5153`) và **Pooled OOF MAE** (`374.6027`).
- **Xác minh tỷ lệ trùng lặp `99,03%` (511 / 516 quan sát) giữa `SeasonalNaive(7D)` và `NaivePersistence`**:
  - **Kết luận**: **ĐÂY LÀ KẾT QUẢ THỰC TẾ CỦA CHU KỲ ĐIỀU HÀNH 7 NGÀY, KHÔNG PHẢI LỖI TRIỂN KHAI**.
  - **Bằng chứng**: Trong 516 quan sát, `SeasonalNaive(7D)` khớp trực tiếp ứng viên mùa vụ trong `±1D` trên **478** dòng (`used_fallback = False`) và fallback trên **38** dòng (`used_fallback = True`). Do chu kỳ điều hành Việt Nam 2024–2025 là đúng 7 ngày, 473/478 dòng khớp mùa vụ rơi trúng kỳ điều hành liền trước `t - 1`. Đúng **5 dòng duy nhất** khác biệt xảy ra ngày **`2025-07-03`** (`expanding_fold_4`, cách đợt điều chỉnh giảm thuế VAT đặc biệt `2025-07-01` đúng `2,625` ngày): `Naive` lấy giá `2025-07-01`, còn `SeasonalNaive(7D)` lấy đúng giá `2025-06-26` (7 ngày trước, `used_fallback = False`).

---

## 5. Tình trạng Đối chiếu CSV / JSON / Markdown

Đã đối chiếu 100% số liệu trong [`docs/T25_Baseline_Benchmark_and_Error_Analysis.md`](T25_Baseline_Benchmark_and_Error_Analysis.md) với toàn bộ 11 tệp tại `artifacts/benchmarks/28b22922e761cb08a8d8/`:
- `expanding_summary.csv`, `rolling_summary.csv`, `expanding_per_fold.csv`, `comeco_active_summary.csv`, `error_by_quarter.csv`, `error_by_interval_horizon.csv`, `top_errors_naive.csv`, `metadata.json`: **khớp hoàn toàn 100%** (tới mức làm tròn 2 chữ số thập phân trong báo cáo Markdown).

---

## 6. Kết quả Chạy lại CLI Tái lập

Lệnh đã chạy:
```bash
PYTHONPATH=src python3 -m petrotwin.evaluation.benchmark --output-dir artifacts/benchmarks/28b22922e761cb08a8d8
```
- Trạng thái: thành công, tạo đủ **11 tệp** (`10` CSV + `metadata.json`).
- `metadata.json` ghi đầy đủ: `schema_version`, `generated_at_utc`, `dataset_run_id` (`28b22922e761cb08a8d8`), `validation_config`, `baseline_config`, `split_strategy`, `input_data_summary` (`total_panel_rows=1390`, `validation_observations=516`, `directional_valid_observations=513`, `cold_start_observations=3`), `final_test_locked: true`, `expanding_naive_seasonal_overlap_pct: 99.03`, `rolling_naive_seasonal_overlap_pct: 99.03`.
- Không chạy MySQL loader, không ghi DB.

---

## 7. Kết quả Full Test Suite & Chất lượng Code

- **Full test suite** (`PYTHONPATH=src python3 -m pytest -q`, Python `3.13.15`, `pytest 9.2.0`):
  - **250 passed, 0 failed, 0 skipped, 1 warning** (`24.45s`).
  - *1 warning duy nhất từ plugin bên thứ ba `anyio/pytest_plugin.py` (`PytestRemovedIn10Warning`); 0 cảnh báo Pandas.*
- **Ruff (`.venv/bin/ruff check` trên phạm vi T22–T25)**: **`All checks passed!`** (0 lỗi sau khi bỏ 2 cast `int(len(...))` thừa `RUF046`).
- **`git diff --check`**: sạch (exit code `0`).

---

## 8. Danh sách File đã Sửa trong Đợt Audit T25

- [`src/petrotwin/evaluation/benchmark.py`](../src/petrotwin/evaluation/benchmark.py): bổ sung `generated_at_utc`, `split_strategy`, `input_data_summary` vào `metadata.json`; sửa 2 lỗi Ruff `RUF046`.
- [`tests/unit/test_benchmark_t25.py`](../tests/unit/test_benchmark_t25.py): bổ sung assertion kiểm tra `generated_at_utc`, `split_strategy`, `validation_observations == 516`, `directional_valid_observations == 513`.
- [`docs/T25_FINAL_AUDIT.md`](T25_FINAL_AUDIT.md): báo cáo audit này.

---

## 9. Vấn đề Còn Tồn tại & Giới hạn Đánh giá

1. **Độ trùng lặp cao của `SeasonalNaiveBaseline(7D)` (`99,03%`)**: Do chu kỳ điều hành 7 ngày, `SeasonalNaive(7D)` hầu như đồng nhất với `NaivePersistence`; trong giai đoạn mô hình chính (SARIMAX/XGBoost), **`naive_persistence` (`Pooled MAE = 374,60 VND/L`)** là mốc so sánh quyết định.
2. **Môi trường `.venv/bin/python`**: Phải chạy `PYTHONPATH=src python3 -m pytest` (Python 3.13 hệ thống) vì `.venv` chưa đồng bộ phiên bản Python; không ảnh hưởng tính tái lập của code hay kết quả số học.
3. **BOG Baseline**: Hai baseline giá không dự báo xác suất BOG; đánh giá phân loại BOG tiếp tục ở trạng thái `provisional` theo T21/T24 cho đến khi có mô hình phân loại BOG.

---

## 10. Kết luận Audit

**`T25 VERIFIED WITH LIMITATIONS`**

Toàn bộ dữ liệu benchmark (516/516 quan sát), phép tính metric, kiểm soát anti-leakage, khóa Final Test 2026, độ khớp CSV/JSON/Markdown, khả năng tái lập CLI, 250/250 test và Ruff T22–T25 đều đạt hoàn toàn. Các giới hạn còn lại (trùng lặp 99,03% của Seasonal Naive 7D do bản chất chu kỳ 7 ngày, trạng thái provisional của BOG, và cách gọi `PYTHONPATH=src python3`) đã được ghi nhận rõ và không ảnh hưởng tới tính đúng đắn cốt lõi của benchmark.
