# T25: Đánh giá Baseline, Phân tích Sai số & Đóng gói Benchmark

> **Snapshot chuẩn đã chạy**: `run_id = 28b22922e761cb08a8d8` (đọc động từ `data/processed/current.json`)  
> **Cấu hình chia tập (T21)**: [`configs/validation.yaml`](../configs/validation.yaml) — 4 folds Validation (`2024-01-01` → `2025-12-31`, **516 quan sát**; **Final Test 2026 giữ khóa**)  
> **Cấu hình baseline (T23)**: [`configs/baseline.yaml`](../configs/baseline.yaml) (`horizon = 1`, `random_seed = 42`)  
> **Mã nguồn benchmark**: [`src/petrotwin/evaluation/benchmark.py`](../src/petrotwin/evaluation/benchmark.py)

---

## 1. Bảng Benchmark Chuẩn (So sánh Công bằng với SARIMAX & XGBoost)

### 1.1. Tổng hợp Expanding Window (Chính) vs Rolling Window (`730d`, Bổ sung)

| Mô hình | Chế độ Window | Mean MAE ± Std (`VND/L`) | Mean RMSE ± Std (`VND/L`) | Pooled MAE (`516 obs`) | Pooled RMSE (`516 obs`) | Pooled MAPE (`%`) | Dir. Acc (`513 obs`) | Fallback TB (`%`) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **`naive_persistence`** | `expanding` | **379,91 ± 38,52** | **504,26 ± 52,91** | **374,60** | **499,69** | **1,90%** | 0,97% | **0,60%** |
| `seasonal_naive (7D)` | `expanding` | 382,73 ± 32,97 | 512,07 ± 37,30 | 378,17 | 508,31 | 1,92% | 0,97% | 7,09% |
| `naive_persistence` | `rolling (730d)` | 390,45 ± 46,12 | 564,90 ± 112,93 | 383,83 | 557,73 | 1,95% | 0,97% | 0,60% |
| `seasonal_naive (7D)` | `rolling (730d)` | 393,27 ± 40,89 | 572,65 ± 100,83 | 387,39 | 565,47 | 1,97% | 0,97% | 7,09% |

### 1.2. Chi tiết theo từng Fold (`expanding_fold_1..4`)

| Mô hình | Fold ID | `valid_n` | MAE (`VND/L`) | RMSE (`VND/L`) | MAPE (`%`) | Fallback (`%`) |
|---|---|---:|---:|---:|---:|---:|
| `naive_persistence` | `expanding_fold_1` (`2024 H1`) | 105 | 408,14 | 531,42 | 1,89% | 0,95% |
| `seasonal_naive (7D)` | `expanding_fold_1` (`2024 H1`) | 105 | 408,14 | 531,42 | 1,89% | 4,76% |
| `naive_persistence` | `expanding_fold_2` (`2024 H2`) | 118 | 392,80 | 530,50 | 2,02% | 0,85% |
| `seasonal_naive (7D)` | `expanding_fold_2` (`2024 H2`) | 118 | 392,80 | 530,50 | 2,02% | 0,85% |
| `naive_persistence` | `expanding_fold_3` (`2025 H1`) | 130 | 395,69 | 530,24 | 2,08% | 0,00% |
| `seasonal_naive (7D)` | `expanding_fold_3` (`2025 H1`) | 130 | 395,69 | 530,24 | 2,08% | 15,38% |
| `naive_persistence` | `expanding_fold_4` (`2025 H2`) | 163 | **323,01** | **424,89** | **1,68%** | 0,61% |
| `seasonal_naive (7D)` | `expanding_fold_4` (`2025 H2`) | 163 | 334,29 | 456,12 | 1,74% | 7,36% |

### 1.3. Tổng hợp Nhóm 4 Sản phẩm COMECO Hiện hành (`comeco_active_4`, 303 obs)

| Mô hình | `valid_n` | MAE (`VND/L`) | RMSE (`VND/L`) | MAPE (`%`) | `directional_valid_n` | Dir. Acc (`%`) |
|---|---:|---:|---:|---:|---:|---:|
| **`naive_persistence`** | 303 | **372,31** | **494,07** | **1,91%** | 301 | 1,33% |
| `seasonal_naive (7D)` | 303 | 375,94 | 502,72 | 1,93% | 301 | 1,33% |

> [!IMPORTANT]
> **Phát hiện thực nghiệm về tỷ lệ trùng lặp (`Overlap Rate`)**:
> Trên cả Expanding và Rolling Window, `SeasonalNaiveBaseline(7D)` cho dự báo trùng hoàn toàn với `NaivePersistenceBaseline` tới **99,03%** (511/516 quan sát) do Việt Nam vận hành chu kỳ điều hành 7 ngày (Nghị định 80/2023/NĐ-CP) khiến `origin_time - 7D` rơi đúng vào kỳ điều hành liền trước `t - 1`. Trên 5 quan sát khác biệt (các kỳ điều chỉnh sớm `1-5d` ở `expanding_fold_4`), `SeasonalNaiveBaseline(7D)` kém hơn Naive (`595,50` vs `503,50 VND/L`). Do đó, **`naive_persistence` là mốc chuẩn chính (Pooled MAE = 374,60 VND/L, RMSE = 499,69 VND/L)**.

---

## 2. Phân tích Sai số theo Thời gian & Horizon / Chu kỳ

### 2.1. Diễn biến Sai số theo Quý (`year_quarter` — `naive_persistence`, Expanding)

| Quý | `valid_n` | MAE (`VND/L`) | RMSE (`VND/L`) | MAPE (`%`) | Nhận xét |
|---|---:|---:|---:|---:|---|
| `2024Q1` | 52 | 418,85 | 489,74 | 1,90% | Giai đoạn đầu ổn định chu kỳ 7 ngày |
| `2024Q2` | 53 | 397,64 | 569,37 | 1,89% | Chứa cold-start `fuel_oil_35s` (lỗi 2.375) |
| `2024Q3` | 53 | 455,09 | 611,85 | 2,32% | Chứa cold-start `diesel_0001s_v` (lỗi 2.630) + biến động giá Q3 |
| `2024Q4` | 65 | 342,00 | 453,49 | 1,78% | Biên độ điều chỉnh dịu lại |
| `2025Q1` | 65 | 318,92 | 396,42 | 1,63% | Biến động thấp |
| `2025Q2` | 65 | **472,46** | **636,51** | **2,53%** | **Đỉnh sai số**: cú sốc giảm mạnh tháng 4 & tăng mạnh tháng 6 |
| `2025Q3` | 79 | **267,97** | **364,20** | **1,40%** | Quý ổn định nhất |
| `2025Q4` | 84 | 374,76 | 474,95 | 1,95% | Biến động trung bình cuối năm |

### 2.2. Sai số theo Horizon & Khoảng cách Chu kỳ (`h = 1`, `interval_horizon_bucket`)

| Mô hình | Khoảng cách ngày kể từ kỳ trước | `valid_n` | MAE (`VND/L`) | RMSE (`VND/L`) | MAPE (`%`) |
|---|---|---:|---:|---:|---:|
| `naive_persistence` | `1-5d (early/irregular)` | 20 | 503,50 | 609,90 | 2,68% |
| `naive_persistence` | `6-8d (standard_7d)` | 483 | **360,99** | **469,00** | **1,82%** |
| `naive_persistence` | `9d+ (extended_cycle)` | 10 | 360,00 | 503,37 | 1,87% |
| `naive_persistence` | `unknown (cold_start)` | 3 | 1.755,00 | 2.051,43 | 9,87% |
| `seasonal_naive (7D)` | `1-5d (early/irregular)` | 20 | 595,50 | 772,12 | 3,16% |
| `seasonal_naive (7D)` | `6-8d (standard_7d)` | 483 | 360,99 | 469,00 | 1,82% |
| `seasonal_naive (7D)` | `9d+ (extended_cycle)` | 10 | 360,00 | 503,37 | 1,87% |
| `seasonal_naive (7D)` | `unknown (cold_start)` | 3 | 1.755,00 | 2.051,43 | 9,87% |

### 2.3. Top 5 Sai số Lớn nhất (`naive_persistence`)

| Fold | Ngày điều hành | `product_id` | Giá thực | Dự báo | `\|err\|` (`VND/L`) | `used_fallback` | Nguyên nhân |
|---|---|---|---:|---:|---:|---|---|
| `expanding_fold_2` | `2024-09-26` | `diesel_0001s_v` | 17.850 | 20.480 | **2.630** | `True` | Cold-start sản phẩm mới (fallback median Train) |
| `expanding_fold_1` | `2024-05-30` | `fuel_oil_35s` | 17.530 | 19.905 | **2.375** | `True` | Cold-start sản phẩm đơn lẻ (1 dòng hợp lệ) |
| `expanding_fold_3` | `2025-04-10` | `gasoline_ron95_iii` | 19.200 | 20.910 | **1.710** | `False` | Cú sốc giảm giá dầu thế giới đầu tháng 4/2025 |
| `expanding_fold_3` | `2025-04-10` | `gasoline_e5_ron92_ii` | 18.880 | 20.370 | **1.490** | `False` | Cú sốc giảm giá dầu thế giới đầu tháng 4/2025 |
| `expanding_fold_3` | `2025-06-19` | `diesel_0001s_v` | 19.510 | 18.060 | **1.450** | `False` | Cú sốc tăng giá dầu thế giới giữa tháng 6/2025 |

---

## 3. Đóng gói Benchmark & Hướng dẫn Chạy Tái lập

### 3.1. Điều kiện so sánh công bằng cho SARIMAX / XGBoost
Mọi mô hình dự báo tiếp theo phải sử dụng:
1. Cùng snapshot đọc động từ `data/processed/current.json` (`run_id = 28b22922e761cb08a8d8`).
2. Cùng 4 folds Expanding & Rolling từ [`configs/validation.yaml`](../configs/validation.yaml) (`iter_validation_splits`), **giữ khóa Final Test 2026**.
3. Cùng hàm tính metric [`compute_regression_metrics()`](../src/petrotwin/evaluation/metrics.py) và cùng quy tắc loại 3 dòng cold-start khỏi mẫu số Directional Accuracy (`directional_valid_n = 513`).
4. **Ngưỡng cần vượt qua**: Pooled Expanding MAE **< 374,60 VND/L** và RMSE **< 499,69 VND/L** (nhóm COMECO Active 4: MAE **< 372,31 VND/L**).

### 3.2. Hướng dẫn chạy từ dòng lệnh (CLI) & Python

Chạy và in toàn bộ bảng benchmark + phân tích sai số:
```bash
PYTHONPATH=src python3 -m petrotwin.evaluation.benchmark
```

Chạy và đóng gói đầy đủ 10 bảng CSV + `metadata.json` (gồm `oof_predictions_expanding.csv` cho kiểm định thống kê theo từng quan sát) ra thư mục artifact ngoài Git:
```bash
PYTHONPATH=src python3 -m petrotwin.evaluation.benchmark --output-dir artifacts/benchmarks/latest
```

Gọi trực tiếp từ Python:
```python
from petrotwin.evaluation import run_baseline_benchmark

res = run_baseline_benchmark(
    processed_dir="data/processed",
    validation_config_path="configs/validation.yaml",
    baseline_config_path="configs/baseline.yaml",
    output_dir="artifacts/benchmarks/latest",
)
print(res.expanding_summary)
```
