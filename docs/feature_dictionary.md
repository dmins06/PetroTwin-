
# PETROTWIN – FEATURE DICTIONARY

## 1. Overview

Tài liệu mô tả các đặc trưng (features) được triển khai trong module Feature Engineering của dự án PetroTwin.

**Source code:** `src/petrotwin/features/pipeline.py`

**Mục tiêu:**
- Tạo đặc trưng từ lịch sử giá xăng dầu.
- Khai thác xu hướng và biến động giá trong các kỳ điều chỉnh trước.
- Bổ sung thông tin lịch và lịch sử Quỹ Bình ổn giá (BOG).
- Phát hiện các mức tăng giá bất thường dựa trên dữ liệu quá khứ.
- Hạn chế data leakage bằng cách chỉ sử dụng dữ liệu của các sự kiện trước đó trong các phép tính Lag và Rolling.

## 2. Feature Groups

### 2.1. Calendar Features

Nhóm đặc trưng mô tả thời gian của sự kiện điều chỉnh giá, được tạo từ `effective_at_local`.

| Feature | Description | Calculation |
|---|---|---|
| `calendar_day_of_week` | Thứ trong tuần | Day of week (0–6) |
| `calendar_day_of_month` | Ngày trong tháng | Day of month |
| `calendar_month` | Tháng trong năm | Month (1–12) |
| `calendar_day_of_year` | Ngày trong năm | Day of year |
| `calendar_day_of_week_sin` | Mã hóa chu kỳ thứ trong tuần | sin(2π × day_of_week / 7) |
| `calendar_day_of_week_cos` | Mã hóa chu kỳ thứ trong tuần | cos(2π × day_of_week / 7) |
| `calendar_month_sin` | Mã hóa chu kỳ tháng | sin(2π × (month − 1) / 12) |
| `calendar_month_cos` | Mã hóa chu kỳ tháng | cos(2π × (month − 1) / 12) |
| `days_since_previous_event` | Số ngày từ kỳ điều chỉnh trước | Current timestamp − previous timestamp |

**Lưu ý:** Các đặc trưng này được tính từ `effective_at_local`, không phải từ forecast origin. Chỉ được sử dụng khi lịch điều chỉnh tương ứng đã được biết tại thời điểm dự báo.

### 2.2. Lag Features

Lag Features lưu lại giá trị của các kỳ điều chỉnh trước đó.

**Lag periods:** 1, 2, 3, 5.

| Feature Pattern | Description |
|---|---|
| `price_value_lag_{n}` | Giá trị giá xăng dầu tại kỳ trước đó |
| `price_change_value_lag_{n}` | Mức thay đổi giá tại kỳ trước đó |
| `stabilization_fund_subsidy_lag_{n}` | Giá trị trích lập hoặc chi sử dụng Quỹ BOG tại kỳ trước đó |

Trong đó `n` thuộc `{1, 2, 3, 5}`.

**Calculation:**

`feature_lag_n(t) = value(t-n)`

Các giá trị được tính riêng theo từng `product_id`, tránh sử dụng lịch sử của sản phẩm khác.

### 2.3. Rolling Features

Rolling Features tổng hợp dữ liệu trong một số kỳ điều chỉnh gần nhất để mô tả xu hướng lịch sử.

**Rolling windows:** 3, 5, 7.

| Feature Pattern | Description |
|---|---|
| `price_value_rolling_mean_{n}` | Giá trung bình của n kỳ trước |
| `price_value_rolling_std_{n}` | Độ lệch chuẩn giá của n kỳ trước |
| `price_change_value_rolling_mean_{n}` | Mức thay đổi giá trung bình của n kỳ trước |
| `stabilization_fund_subsidy_rolling_mean_{n}` | Giá trị BOG trung bình của n kỳ trước |

Trong đó `n` thuộc `{3, 5, 7}`.

**Calculation:**

- Rolling Mean: trung bình các giá trị trong cửa sổ lịch sử.
- Rolling Standard Deviation: độ lệch chuẩn của các giá trị trong cửa sổ lịch sử.

**Leakage Prevention:**

Dữ liệu được dịch lùi một kỳ (`shift(1)`) trước khi tính Rolling. Vì vậy, giá trị của sự kiện hiện tại không tham gia vào cửa sổ tính toán.

Lưu ý: Code cũng tạo Rolling Mean cho Brent và USD/VND nếu dữ liệu đầu vào có các cột tương ứng, nhưng chúng chưa nằm trong danh sách predictor được phê duyệt.

### 2.4. BOG History Features

Nhóm đặc trưng khai thác lịch sử Quỹ Bình ổn giá.

**BOG class encoding:**

| Class | Meaning | Condition |
|---|---|---|
| 0 | NO_ACTION | amount = 0 |
| 1 | SPEND | amount > 0 |
| 2 | CONTRIBUTE | amount < 0 |

**Features:**

| Feature | Description |
|---|---|
| `bog_amount_lag_1` | Giá trị BOG của kỳ trước |
| `bog_class_lag_1` | Phân loại BOG của kỳ trước |
| `bog_no_action_count_3` | Số lần NO_ACTION trong 3 kỳ trước |
| `bog_spend_count_3` | Số lần SPEND trong 3 kỳ trước |
| `bog_contribute_count_3` | Số lần CONTRIBUTE trong 3 kỳ trước |
| `bog_known_count_3` | Số kỳ có thông tin BOG trong 3 kỳ trước |
| `bog_last_known_amount` | Giá trị BOG gần nhất đã biết |

**Important:**
- Các feature lịch sử BOG được tính riêng theo từng sản phẩm.
- Sử dụng `shift(1)` để không đưa thông tin BOG của sự kiện hiện tại vào predictor.
- Giá trị BOG bị thiếu được giữ là NaN, không tự động chuyển thành NO_ACTION.

**Data source:** Bảng annotation BOG được truyền vào `build_features()` thông qua tham số `bog_annotations`.

**Limitation:** Dữ liệu annotation hiện chưa có timestamp thể hiện thời điểm thông tin BOG được công bố. Do đó, cần xác minh tính sẵn có tại forecast origin trước khi sử dụng trong dự báo thực tế.

### 2.5. Unusual Price Increase Features

Nhóm đặc trưng nhận diện mức tăng giá bất thường dựa trên lịch sử thay đổi giá.

| Feature | Description |
|---|---|
| `price_change_mean_3` | Trung bình mức thay đổi giá trong 3 kỳ trước |
| `price_change_std_3` | Độ lệch chuẩn mức thay đổi giá trong 3 kỳ trước |
| `price_increase_zscore_lag_1` | Z-score của mức thay đổi giá kỳ trước so với baseline lịch sử |
| `price_increase_positive_lag_1` | Cho biết mức thay đổi giá kỳ trước có dương hay không |

**Z-score calculation:**

`Z = (Previous Price Change − Historical Mean) / Historical Standard Deviation`

Trong code, baseline được xây dựng từ các sự kiện trước sự kiện đang xét. Nếu độ lệch chuẩn bằng 0 hoặc không đủ dữ liệu, Z-score có thể là NaN.

**Interpretation:**
- Z-score lớn: mức thay đổi giá trước đó cao hơn đáng kể so với baseline lịch sử.
- Z-score âm: mức thay đổi giá thấp hơn baseline.
- `price_increase_positive_lag_1 = 1`: mức thay đổi giá kỳ trước lớn hơn 0.
- `price_increase_positive_lag_1 = 0`: mức thay đổi giá kỳ trước không lớn hơn 0.

Đây là các đặc trưng liên tục và nhị phân, chưa có ngưỡng cố định để kết luận một đợt tăng giá là bất thường.

## 3. Exogenous & Intra-cycle Volatility Features

Các biến ngoại sinh được hỗ trợ trong `src/petrotwin/features/pipeline.py` (nối điểm thời gian với `market_observation.csv` theo điều kiện nghiêm ngặt `observation_date < origin_date`, `allow_exact_matches=False`, và `fx_max_age_days=7`):

| Feature Pattern | Đơn vị | Mô tả |
|---|---|---|
| `brent_usd_per_barrel_lag_{n}`, `usd_vnd_rate_lag_{n}` | `USD/bbl`, `VND/USD` | Giá trị trễ `n ∈ {1, 2, 3, 5}` kỳ điều hành trước đó |
| `brent_usd_per_barrel_rolling_mean_{n}`, `brent_usd_per_barrel_rolling_std_{n}` | `USD/bbl` | Trung bình và độ lệch chuẩn trượt `n ∈ {3, 5, 7}` kỳ trước |
| `usd_vnd_rate_rolling_mean_{n}`, `usd_vnd_rate_rolling_std_{n}` | `VND/USD` | Trung bình và độ lệch chuẩn trượt `n ∈ {3, 5, 7}` kỳ trước |
| `brent_vnd_implied`, `brent_vnd_per_liter_proxy` | `VND/bbl`, `VND/liter` | Giá Brent quy đổi sang VND và xấp xỉ theo lít (`÷ 158.9873`) |
| `brent_change_since_previous_event`, `brent_pct_change_since_previous_event` | `USD/bbl`, `ratio` | Biến động Brent giữa hai kỳ điều hành |
| `fx_pct_change_since_previous_event` | `ratio` | Tỷ lệ thay đổi tỷ giá USD/VND giữa hai kỳ điều hành |

## 4. Intra-cycle Volatility Features

Được tính khi truyền `market_observations` vào `build_features()` hoặc `FoldFeaturePipeline` (chỉ sử dụng dữ liệu ngày giao dịch có `observation_date < origin_date`):

| Feature | Đơn vị | Mô tả |
|---|---|---|
| `brent_ret_1d`, `brent_ret_7d` | `ratio` | Biến động Brent 1 phiên và 7 phiên trước `origin_date` |
| `brent_rolling_mean_7d`, `brent_rolling_std_7d` | `USD/bbl` | Trung bình và độ lệch chuẩn 7 phiên của Brent |
| `brent_intra_cycle_volatility`, `brent_intra_cycle_range` | `ratio`, `USD/bbl` | Độ lệch chuẩn lợi suất ngày và biên độ `max - min` 7 phiên nội kỳ của Brent |
| `fx_ret_1d`, `fx_ret_7d`, `fx_rolling_mean_7d`, `fx_rolling_std_7d` | `ratio`, `VND/USD` | Biến động và thống kê trượt 7 ngày của USD/VND (`NaN` nếu quá `fx_max_age_days=7`) |
| `fx_intra_cycle_volatility`, `fx_intra_cycle_range` | `ratio`, `VND/USD` | Độ biến động lợi suất ngày và biên độ 7 ngày nội kỳ của USD/VND |

Tất cả các biến trên được kiểm soát qua `get_feature_columns(features, include_exogenous=True)`. Nếu truyền trực tiếp `panel` không có `market_observations`, pipeline vẫn kiểm tra bắt buộc `brent_observation_date < effective_date` và `fx_observation_date < effective_date`.

## 5. Fold-specific Feature Pipeline

Class `FoldFeaturePipeline` hỗ trợ xử lý đặc trưng theo từng fold.

| Method | Description |
|---|---|
| `fit(train)` | Lưu lịch sử train và tính toán thống kê preprocessing trên tập train |
| `transform(data)` | Tạo feature cho dữ liệu mới dựa trên lịch sử train đã lưu |
| `fit_transform(train)` | Fit pipeline và trả về dữ liệu train đã được tạo feature |

**Leakage Prevention:**
- Thống kê imputation được tính trên tập train.
- Dữ liệu transform phải nằm sau lịch sử đã fit của từng sản phẩm.
- Feature Lag và Rolling chỉ sử dụng lịch sử trước sự kiện hiện tại.
- Tạo một pipeline riêng cho mỗi fold.

**Limitation:** Pipeline hiện chưa tự xây dựng forecast origin và chưa xác minh timestamp công bố của dữ liệu thị trường. Do đó, chưa thể thay thế toàn bộ quy trình kiểm soát point-in-time theo T21.

## 6. Target Columns and Excluded Predictors

Các cột sau được xác định là target hoặc nhãn của sự kiện hiện tại:

| Column | Description |
|---|---|
| `price_value` | Giá xăng dầu hiện tại |
| `price_change_value` | Mức thay đổi giá hiện tại |
| `stabilization_fund_subsidy` | Giá trị BOG hiện tại |
| `bog_amount` | Số tiền BOG của sự kiện hiện tại |
| `bog_class` | Nhãn phân loại BOG của sự kiện hiện tại |

Các cột này được giữ lại trong DataFrame để kiểm tra và đối chiếu, nhưng không được đưa trực tiếp vào danh sách predictor được phê duyệt.

## 7. Feature Selection

Hàm `get_feature_columns(features)` sử dụng danh sách prefix cho phép để lựa chọn predictor.

Các nhóm được phê duyệt trong code hiện tại:

1. Calendar Features.
2. Price Lag Features.
3. Price Rolling Features.
4. Stabilization Fund Lag and Rolling Features.
5. BOG History Features.
6. Unusual Price Increase Features.

Các biến Brent và USD/VND chưa được phê duyệt trong danh sách này.

## 8. Point-in-Time and Data Leakage Considerations

Các nguyên tắc cần tuân thủ:

- Không sử dụng dữ liệu của sự kiện hiện tại để tạo Lag hoặc Rolling predictor.
- Không trộn lịch sử giữa các sản phẩm.
- Không sử dụng thông tin được công bố sau forecast origin.
- Chỉ fit preprocessing trên tập train của từng fold.
- Không tự động biến dữ liệu BOG thiếu thành NO_ACTION.
- Chỉ sử dụng biến ngoại sinh khi xác minh được thời điểm dữ liệu có sẵn.

**Important limitations requiring further work:**

1. Calendar Features hiện dựa trên `effective_at_local`; cần xác minh lịch sự kiện đã biết tại forecast origin.
2. BOG annotations chưa có `available_at`.
3. Brent và USD/VND chưa có timestamp availability phù hợp để xác minh point-in-time.
4. Intra-cycle Volatility chưa được triển khai.

## 9. Implementation and Testing

**Implementation file:**

`src/petrotwin/features/pipeline.py`

**Unit test file:**

`tests/unit/test_feature_engineering.py`

Các nội dung đã được kiểm thử:

| Test case | Expected result |
|---|---|
| Lag uses previous event only | PASS |
| Products have separate histories | PASS |
| Rolling excludes current event | PASS |
| Current target is not a feature | PASS |
| Fold pipeline uses training history | PASS |
| Transform requires fit | PASS |
| Duplicate event keys are rejected | PASS |
| BOG features use previous event only | PASS |
| Missing BOG is not converted to NO_ACTION | PASS |
| BOG histories are separated by product | PASS |
| Price anomaly features use past events only | PASS |
| Market as-of join enforces strict prior date & intra-cycle volatility | PASS |
| Panel same-day market date raises leakage error | PASS |
| `price_increase_positive_lag_1` preserves NaN | PASS |
| Fold pipeline imputation & live inference without target | PASS |

**Latest test result:** 15 passed (`tests/unit/test_feature_engineering.py`).

---

**Document status:** Feature Engineering documentation based on the current implementation. Features marked pending or not approved must not be considered production-ready predictors until their point-in-time availability requirements are satisfied.
